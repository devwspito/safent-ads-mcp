"""Revision de codigo (17-sep, nit de f36fb2a6): dos rutas distintas tratan
una fila de `oauth_clients` que ya no cumple una regla del dominio (destino
remoto de D-11, `client_name` con caracteres bidireccionales de C-70 pieza
3) SIN dejar escapar el `DomainError` -- `_hydrate_for_listing` (lectura en
lote, `get_many`/`ListGrants`) y `get_client_tolerating_domain_violations`
(lectura individual, `get_consent`/`StartAuthorization`/`ApproveConsent`).
Cada una emite su PROPIO evento de WARNING, y los dos llevan exactamente
`client_id` + `error_type` -- nunca el mensaje de la excepcion, que lleva
el valor rechazado (entrada de un tercero)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import structlog.testing

from safent_ads.mcp_oauth.application.client_lookup import (
    get_client_tolerating_domain_violations,
)
from safent_ads.mcp_oauth.domain.errors import InvalidRedirectUriError
from safent_ads.mcp_oauth.infrastructure.sql_client_repository import _hydrate_for_listing
from safent_ads.shared.errors import DomainError

_MALICIOUS_REDIRECT_URI = "https://evil.example/callback"


def _legacy_client_row(client_id: str) -> SimpleNamespace:
    """Misma forma que la fila que devuelve `SqlClientRepository` (atributos
    de un `Row` de SQLAlchemy) con un `redirect_uri` remoto -- invalido
    desde D-11 -- para que `_row_to_client` levante `InvalidRedirectUriError`
    al construir el value object."""
    return SimpleNamespace(
        client_id=client_id,
        client_name="Claude Code",
        redirect_uris=[_MALICIOUS_REDIRECT_URI],
        token_endpoint_auth_method="none",
        client_secret_hash=None,
        grant_types=["authorization_code", "refresh_token"],
        requested_scopes="ads:read ads:propose",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        last_seen_at=None,
    )


class _RaisingClientRepository:
    def __init__(self, error: DomainError) -> None:
        self._error = error

    async def get_by_id(self, client_id: str) -> None:
        del client_id
        raise self._error


def _only_fields(entry: dict[str, object]) -> set[str]:
    return set(entry) - {"event", "log_level"}


async def test_hydrate_for_listing_logs_client_id_and_error_type_only() -> None:
    client_id = f"client-{uuid.uuid4().hex[:8]}"

    with structlog.testing.capture_logs() as logs:
        result = _hydrate_for_listing(_legacy_client_row(client_id))

    assert result is None
    (entry,) = [log for log in logs if log["event"] == "mcp_oauth_client_omitted_from_listing"]
    assert entry["log_level"] == "warning"
    assert entry["client_id"] == client_id
    assert entry["error_type"] == "InvalidRedirectUriError"
    assert _only_fields(entry) == {"client_id", "error_type"}
    assert _MALICIOUS_REDIRECT_URI not in repr(entry)


async def test_get_client_tolerating_domain_violations_logs_client_id_and_error_type_only() -> None:
    client_id = f"client-{uuid.uuid4().hex[:8]}"
    error = InvalidRedirectUriError(f"redirect_uri no permitida: {_MALICIOUS_REDIRECT_URI}")
    repository = _RaisingClientRepository(error)

    with structlog.testing.capture_logs() as logs:
        result = await get_client_tolerating_domain_violations(repository, client_id)  # type: ignore[arg-type]

    assert result is None
    (entry,) = [log for log in logs if log["event"] == "mcp_oauth_client_rejected_by_the_domain"]
    assert entry["log_level"] == "warning"
    assert entry["client_id"] == client_id
    assert entry["error_type"] == "InvalidRedirectUriError"
    assert _only_fields(entry) == {"client_id", "error_type"}
    assert _MALICIOUS_REDIRECT_URI not in repr(entry)


async def test_get_client_tolerating_domain_violations_truncates_an_oversized_client_id() -> None:
    """Nit (code review 17-sep): el `client_id` que llega aqui puede venir
    SIN validar todavia -- un `client_id` fabricado kilometrico no debe
    inflar el registro."""
    oversized_client_id = "a" * 500
    error = InvalidRedirectUriError("redirect_uri no permitida")
    repository = _RaisingClientRepository(error)

    with structlog.testing.capture_logs() as logs:
        await get_client_tolerating_domain_violations(repository, oversized_client_id)  # type: ignore[arg-type]

    (entry,) = [log for log in logs if log["event"] == "mcp_oauth_client_rejected_by_the_domain"]
    assert len(str(entry["client_id"])) <= 101
    assert str(entry["client_id"]).endswith("…")


async def test_get_client_tolerating_domain_violations_escapes_control_characters() -> None:
    """Nit (code review 17-sep): un caracter de control (salto de linea,
    escape ANSI) en el `client_id` no debe colarse tal cual en un log
    estructurado -- podria inyectar lineas falsas o romper el parseo
    aguas abajo."""
    hostile_client_id = "client\n\x1b[31mFAKE ERROR\x1b[0m\r"
    error = InvalidRedirectUriError("redirect_uri no permitida")
    repository = _RaisingClientRepository(error)

    with structlog.testing.capture_logs() as logs:
        await get_client_tolerating_domain_violations(repository, hostile_client_id)  # type: ignore[arg-type]

    (entry,) = [log for log in logs if log["event"] == "mcp_oauth_client_rejected_by_the_domain"]
    logged_client_id = str(entry["client_id"])
    assert "\n" not in logged_client_id
    assert "\r" not in logged_client_id
    assert "\x1b" not in logged_client_id
