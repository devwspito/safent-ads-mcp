"""`audit_client_redirect_uris` (D-11, threat-model.md C-70 pieza 4): la
linea de arranque que delata las registraciones antiguas que la regla de
bucle local exclusivo deja sin poder autorizar. No borra, no arregla, no
tumba el arranque si la BD no responde -- ni si una fila esta malformada,
ni si la tabla todavia no existe (instalacion recien hecha)."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Self

import pytest
import structlog
from asyncpg.exceptions import InvalidPasswordError
from sqlalchemy.exc import OperationalError, ProgrammingError

from safent_ads.mcp_oauth.infrastructure import audit_redirect_uris as audit_module
from safent_ads.mcp_oauth.infrastructure.audit_redirect_uris import (
    audit_client_redirect_uris,
    has_a_rejected_redirect_uri,
)

_EVENT = "mcp_oauth_non_loopback_client_registrations"


class _MissingTable(Exception):
    """El error del driver cuando `oauth_clients` todavia no existe; la
    auditoria lo distingue por el SQLSTATE, sin importar `asyncpg`."""

    sqlstate = "42P01"


@dataclass(frozen=True, slots=True)
class _Row:
    client_id: str
    redirect_uris: list[str]


class _StubResult:
    def __init__(self, rows: list[_Row]) -> None:
        self._rows = rows

    def all(self) -> list[_Row]:
        return self._rows


class _StubSession:
    def __init__(self, rows: list[_Row], *, failure: Exception | None = None) -> None:
        self._rows = rows
        self._failure = failure

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def execute(self, *_: object, **__: object) -> _StubResult:
        if self._failure is not None:
            raise self._failure
        return _StubResult(self._rows)


def _session_factory(rows: list[_Row], *, failure: Exception | None = None) -> Any:  # noqa: ANN401
    return lambda: _StubSession(rows, failure=failure)


@pytest.fixture
def captured_logs(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict[str, Any]]]:
    """`capture_logs()` intercambia los procesadores GLOBALES, pero el
    `logger` del modulo es un proxy que ya se ato a la configuracion que
    dejo el ultimo `create_app()` de la sesion (ver
    `tests/unit/composition/test_release_version.py::_structlog_config_
    restored`). Rebindearlo DENTRO del contexto es lo que hace que la
    captura funcione tambien en una ejecucion completa de la suite, no
    solo con el fichero suelto -- mismo truco que
    `test_mcp_static_token_warning.py`.

    Y el nivel: `configure_logging()` deja un `wrapper_class` que FILTRA
    en INFO, asi que sin bajarlo el evento de DEBUG (`..._skipped`) se
    perderia. `capture_logs()` no restaura el `wrapper_class`, de ahi la
    instantanea de la configuracion alrededor."""
    before = structlog.get_config()
    try:
        with structlog.testing.capture_logs() as logs:
            structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG))
            monkeypatch.setattr(audit_module, "logger", structlog.get_logger())
            yield logs
    finally:
        structlog.configure(**before)


@pytest.mark.parametrize(
    ("redirect_uris", "expected"),
    (
        (["http://127.0.0.1:54321/callback"], False),
        (["https://localhost:8443/callback"], False),
        (["https://agent.example/callback"], True),
        (["http://127.0.0.1:54321/callback", "https://agent.example/callback"], True),
        (["http://127.0.0.1.evil.com/callback"], True),
        # Autoridad rota: `urlsplit` levanta `ValueError` y el dominio lo
        # traduce -- la auditoria lo cuenta como ofensor, no revienta.
        (["http://[::1"], True),
    ),
)
def test_has_a_rejected_redirect_uri(redirect_uris: list[str], expected: bool) -> None:
    assert has_a_rejected_redirect_uri(redirect_uris) is expected


async def test_a_clean_registry_logs_nothing(captured_logs: list[dict[str, Any]]) -> None:
    rows = [_Row("client-1", ["http://127.0.0.1:54321/callback"])]

    offenders = await audit_client_redirect_uris(_session_factory(rows))

    assert offenders == ()
    assert not [entry for entry in captured_logs if entry["event"] == _EVENT]


async def test_a_legacy_remote_registration_is_listed_by_client_id(
    captured_logs: list[dict[str, Any]],
) -> None:
    rows = [
        _Row("client-remoto", ["https://agent.example/callback"]),
        _Row("client-local", ["http://127.0.0.1:54321/callback"]),
    ]

    offenders = await audit_client_redirect_uris(_session_factory(rows))

    assert offenders == ("client-remoto",)
    [entry] = [entry for entry in captured_logs if entry["event"] == _EVENT]
    assert entry["client_ids"] == ["client-remoto"]
    assert entry["count"] == 1


async def test_the_offending_uri_never_reaches_the_log(
    captured_logs: list[dict[str, Any]],
) -> None:
    """DCR esta abierta a Internet: el `redirect_uri` es entrada de un
    tercero y no entra en el registro estructurado. El `client_id` basta
    para encontrar la fila."""
    rows = [_Row("client-remoto", ["https://agent.example/callback?ruido=<script>"])]

    await audit_client_redirect_uris(_session_factory(rows))

    assert all("agent.example" not in repr(entry) for entry in captured_logs)


@pytest.mark.parametrize(
    "failure",
    (
        OperationalError("SELECT ...", {}, Exception("connection refused")),
        # Un fallo de CONEXION del driver no pasa por `sqlalchemy.exc`
        # (`asyncpg.exceptions.InvalidPasswordError` sale crudo): es
        # justo el caso de "arrancar sin BD detras" y tambien se traga.
        InvalidPasswordError("password authentication failed for user"),
    ),
)
async def test_a_database_that_does_not_answer_never_stops_the_boot(
    failure: Exception, captured_logs: list[dict[str, Any]]
) -> None:
    offenders = await audit_client_redirect_uris(_session_factory([], failure=failure))

    assert offenders == ()
    assert any(entry["event"] == "mcp_oauth_redirect_uri_audit_failed" for entry in captured_logs)


async def test_a_malformed_row_is_reported_instead_of_stopping_the_boot(
    captured_logs: list[dict[str, Any]],
) -> None:
    """Revision de seguridad (17-sep): el recuento vive DENTRO del `try`,
    asi que ni siquiera una fila cuya URI no parsea puede escaparse hacia
    el arranque de `ads-api`."""
    rows = [_Row("client-roto", ["http://[::1"])]

    offenders = await audit_client_redirect_uris(_session_factory(rows))

    assert offenders == ("client-roto",)
    assert any(entry["event"] == _EVENT for entry in captured_logs)


async def test_at_most_twenty_client_ids_reach_the_log_with_the_total_count(
    captured_logs: list[dict[str, Any]],
) -> None:
    rows = [_Row(f"client-{index:03d}", ["https://agent.example/callback"]) for index in range(25)]

    offenders = await audit_client_redirect_uris(_session_factory(rows))

    assert len(offenders) == 25
    [entry] = [entry for entry in captured_logs if entry["event"] == _EVENT]
    assert entry["count"] == 25
    assert entry["client_ids"] == [f"client-{index:03d}" for index in range(20)]


async def test_a_table_that_does_not_exist_yet_is_not_an_alarm(
    captured_logs: list[dict[str, Any]],
) -> None:
    """Instalacion recien hecha: `ads-api` puede arrancar antes de que las
    migraciones creen `oauth_clients`. Eso se anota en DEBUG, no como un
    fallo que revisar."""
    failure = ProgrammingError("SELECT ...", {}, _MissingTable())

    offenders = await audit_client_redirect_uris(_session_factory([], failure=failure))

    assert offenders == ()
    assert not [
        entry for entry in captured_logs if entry["event"] == "mcp_oauth_redirect_uri_audit_failed"
    ]
    assert any(
        entry["event"] == "mcp_oauth_redirect_uri_audit_skipped" for entry in captured_logs
    )


async def test_the_failure_log_names_the_exception_type(
    captured_logs: list[dict[str, Any]],
) -> None:
    offenders = await audit_client_redirect_uris(
        _session_factory([], failure=InvalidPasswordError("password authentication failed"))
    )

    assert offenders == ()
    [entry] = [
        entry for entry in captured_logs if entry["event"] == "mcp_oauth_redirect_uri_audit_failed"
    ]
    assert entry["error_type"] == "InvalidPasswordError"
