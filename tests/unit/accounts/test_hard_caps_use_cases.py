"""Capa de aplicacion de los topes duros (spec 008 T031): puerto, casos de
uso, auditoria doble y la invariante i11 -- `set`/`delete` NUNCA se piden
con reintento, porque son mutaciones, igual que `execute_write`."""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import struct
from pathlib import Path

import pytest

from safent_ads.accounts.application.errors import BrokerRequestDeniedError
from safent_ads.accounts.application.hard_caps import (
    DeleteAccountHardCaps,
    GetAccountHardCaps,
    SetAccountHardCaps,
)
from safent_ads.accounts.application.hard_caps_ports import (
    AccountHardCapsView,
    EffectiveAmountsView,
    HardCapsBrokerPort,
    PanelCapsInput,
)
from safent_ads.accounts.infrastructure import broker_hard_caps_client
from safent_ads.accounts.infrastructure.broker_hard_caps_client import BrokerHardCapsSocketClient
from safent_ads.accounts.infrastructure.errors import BrokerConnectionError
from safent_ads.audit.application.record_decision import RecordDecision
from safent_ads.audit.domain.entry import DecisionKind, DecisionLogEntry, PendingDecision
from safent_ads.shared.ids import BusinessId

_BUSINESS = BusinessId.parse("00000000-0000-0000-0000-000000000000")
_ACCOUNT = "1234567890"
_OWNER = "owner-1"

_CAPS = PanelCapsInput(
    daily_cap_minor=1000, monthly_cap_minor=20000, ceiling_minor=9000, currency="EUR"
)


class _RecordingDecisionLog:
    def __init__(self) -> None:
        self.appended: list[PendingDecision] = []

    async def append(self, pending: PendingDecision) -> DecisionLogEntry:
        self.appended.append(pending)
        raise _StopAfterAppend


class _StopAfterAppend(Exception):
    """El caso de uso no usa la entrada devuelta; este doble evita tener que
    fabricar una fila encadenada completa solo para el retorno."""


class _SpyBroker(HardCapsBrokerPort):
    def __init__(self, *, view: AccountHardCapsView | None = None) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self._view = view or _view()

    async def resolve_account_caps(self, platform_account_id: str) -> AccountHardCapsView:
        self.calls.append(("resolve", {"platform_account_id": platform_account_id}))
        return self._view

    async def set_account_caps(
        self,
        platform_account_id: str,
        caps: PanelCapsInput,
        *,
        requested_by: str,
        request_id: str | None = None,
    ) -> AccountHardCapsView:
        self.calls.append(
            (
                "set",
                {
                    "platform_account_id": platform_account_id,
                    "requested_by": requested_by,
                    "request_id": request_id,
                    "daily_cap_minor": caps.daily_cap_minor,
                },
            )
        )
        return self._view

    async def delete_account_caps(
        self, platform_account_id: str, *, requested_by: str, request_id: str | None = None
    ) -> AccountHardCapsView:
        self.calls.append(
            (
                "delete",
                {
                    "platform_account_id": platform_account_id,
                    "requested_by": requested_by,
                    "request_id": request_id,
                },
            )
        )
        return self._view


class _DeniedBroker(_SpyBroker):
    async def set_account_caps(
        self,
        platform_account_id: str,
        caps: PanelCapsInput,
        *,
        requested_by: str,
        request_id: str | None = None,
    ) -> AccountHardCapsView:
        await super().set_account_caps(
            platform_account_id, caps, requested_by=requested_by, request_id=request_id
        )
        raise BrokerRequestDeniedError("ENVELOPE_EXCEEDED", "denied")


def _view(**overrides: object) -> AccountHardCapsView:
    base: dict[str, object] = {
        "platform_account_id": _ACCOUNT,
        "source": "panel",
        "writable": True,
        "effective": EffectiveAmountsView(
            daily_cap_minor=1000, monthly_cap_minor=20000, floor_minor=500, ceiling_minor=9000
        ),
        "clamped_by": (),
        "panel_state_available": True,
        "envelope": None,
    }
    return AccountHardCapsView(**(base | overrides))  # type: ignore[arg-type]


def _record(log: _RecordingDecisionLog) -> RecordDecision:
    return RecordDecision(log)  # type: ignore[arg-type]


# --- Auditoria doble -----------------------------------------------------


async def test_setting_a_cap_appends_a_decision_after_the_broker_accepts() -> None:
    log = _RecordingDecisionLog()
    use_case = SetAccountHardCaps(_SpyBroker(), _record(log), business_id=_BUSINESS)

    with pytest.raises(_StopAfterAppend):
        await use_case.execute(_ACCOUNT, _CAPS, owner_id=_OWNER)

    assert len(log.appended) == 1
    decision = log.appended[0]
    assert decision.kind is DecisionKind.ACCOUNT_HARD_CAPS_SET
    assert decision.actor_id == _OWNER
    assert decision.payload["platform_account_id"] == _ACCOUNT
    assert decision.payload["requested"]["daily_cap_minor"] == 1000  # type: ignore[index]


async def test_deleting_a_cap_appends_its_own_decision_kind() -> None:
    log = _RecordingDecisionLog()
    use_case = DeleteAccountHardCaps(_SpyBroker(), _record(log), business_id=_BUSINESS)

    with pytest.raises(_StopAfterAppend):
        await use_case.execute(_ACCOUNT, owner_id=_OWNER)

    assert log.appended[0].kind is DecisionKind.ACCOUNT_HARD_CAPS_DELETED
    assert "requested" not in log.appended[0].payload


async def test_a_change_the_broker_refused_is_never_recorded_as_applied() -> None:
    log = _RecordingDecisionLog()
    use_case = SetAccountHardCaps(_DeniedBroker(), _record(log), business_id=_BUSINESS)

    with pytest.raises(BrokerRequestDeniedError):
        await use_case.execute(_ACCOUNT, _CAPS, owner_id=_OWNER)

    assert log.appended == []


async def test_reading_the_caps_is_not_a_decision() -> None:
    log = _RecordingDecisionLog()
    broker = _SpyBroker()

    await GetAccountHardCaps(broker).execute(_ACCOUNT)

    assert log.appended == []
    assert broker.calls == [("resolve", {"platform_account_id": _ACCOUNT})]


async def test_the_audited_payload_carries_no_personal_data() -> None:
    """`PendingDecision` rechaza por su cuenta las claves prohibidas; esto
    comprueba que el payload que construimos no las necesita."""
    log = _RecordingDecisionLog()
    use_case = SetAccountHardCaps(_SpyBroker(), _record(log), business_id=_BUSINESS)

    with pytest.raises(_StopAfterAppend):
        await use_case.execute(_ACCOUNT, _CAPS, owner_id=_OWNER)

    serialized = json.dumps(dict(log.appended[0].payload))
    assert "@" not in serialized


# --- i11: mutaciones, nunca con reintento --------------------------------


def test_i11_the_hard_caps_client_has_no_retry_switch_at_all() -> None:
    """`set_account_caps`/`delete_account_caps` son mutaciones, como
    `execute_write`. Aqui no hay un `retryable=True` que se pueda pasar por
    error: el nombre no existe en el codigo, ni como parametro ni como
    argumento. Se mira el AST, no el texto: un comentario que lo mencione
    no debe hacer fallar el test, y una llamada escondida dentro de una
    funcion tampoco debe escaparse."""
    tree = ast.parse(Path(broker_hard_caps_client.__file__).read_text())
    names = {
        argument.arg
        for node in ast.walk(tree)
        if isinstance(node, ast.arguments)
        for argument in (*node.args, *node.kwonlyargs, *node.posonlyargs)
    } | {
        keyword.arg
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for keyword in node.keywords
    }

    assert "retryable" not in names
    assert set(inspect.signature(BrokerHardCapsSocketClient._request).parameters) == {
        "self",
        "payload",
    }


async def test_i11_a_connection_failure_is_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`monkeypatch` y no un `try/finally` a mano: si el cuerpo fallara
    antes del `finally`, un `asyncio` parcheado se llevaria por delante al
    resto de la sesion de tests."""
    attempts = 0

    async def failing(*args: object, **kwargs: object) -> tuple[object, object]:
        nonlocal attempts
        attempts += 1
        raise OSError("sin socket")

    monkeypatch.setattr(asyncio, "open_unix_connection", failing)
    client = BrokerHardCapsSocketClient(tmp_path / "broker.sock")

    with pytest.raises(BrokerConnectionError):
        await client.set_account_caps(_ACCOUNT, _CAPS, requested_by=_OWNER)

    assert attempts == 1


# --- El cliente contra un socket real -------------------------------------


async def test_the_client_speaks_the_contract_over_a_real_socket(tmp_path: Path) -> None:
    socket_path = tmp_path / "broker.sock"
    received: list[dict[str, object]] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        header = await reader.readexactly(4)
        (length,) = struct.unpack(">I", header)
        received.append(json.loads(await reader.readexactly(length)))
        body = json.dumps(
            {
                "ok": True,
                "result": {
                    "platform_account_id": _ACCOUNT,
                    "source": "file_and_panel",
                    "writable": True,
                    "effective": {
                        "daily_cap_minor": 1000,
                        "monthly_cap_minor": 20000,
                        "floor_minor": 200,
                        "ceiling_minor": 9000,
                    },
                    "clamped_by": ["daily_cap_minor"],
                    "panel_state_available": True,
                    "envelope": {
                        "max_daily_cap_minor": 5000,
                        "max_monthly_cap_minor": 100000,
                        "max_ceiling_minor": 20000,
                        "min_floor_minor": 500,
                        "max_accounts": 2,
                        "accounts_used": 1,
                        "max_cap_changes_per_day": 3,
                        "cap_changes_today": 1,
                        "currency": "EUR",
                    },
                },
            }
        ).encode()
        writer.write(struct.pack(">I", len(body)) + body)
        await writer.drain()
        writer.close()

    server = await asyncio.start_unix_server(handle, path=str(socket_path))
    async with server:
        view = await BrokerHardCapsSocketClient(socket_path).set_account_caps(
            _ACCOUNT, _CAPS, requested_by=_OWNER, request_id="nonce-1"
        )

    assert received[0] == {
        "op": "set_account_caps",
        "platform_account_id": _ACCOUNT,
        "caps": {
            "daily_cap_minor": 1000,
            "monthly_cap_minor": 20000,
            "ceiling_minor": 9000,
            "currency": "EUR",
        },
        "requested_by": _OWNER,
        "request_id": "nonce-1",
    }
    assert view.source == "file_and_panel"
    assert view.clamped_by == ("daily_cap_minor",)
    assert view.envelope is not None
    assert view.envelope.accounts_used == 1


async def test_a_denial_from_the_broker_keeps_its_error_code(tmp_path: Path) -> None:
    socket_path = tmp_path / "broker.sock"

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        header = await reader.readexactly(4)
        (length,) = struct.unpack(">I", header)
        await reader.readexactly(length)
        body = json.dumps(
            {"ok": False, "error_code": "ENVELOPE_NOT_DECLARED", "reason": "denied"}
        ).encode()
        writer.write(struct.pack(">I", len(body)) + body)
        await writer.drain()
        writer.close()

    server = await asyncio.start_unix_server(handle, path=str(socket_path))
    async with server:
        with pytest.raises(BrokerRequestDeniedError) as error:
            await BrokerHardCapsSocketClient(socket_path).resolve_account_caps(_ACCOUNT)

    assert error.value.error_code == "ENVELOPE_NOT_DECLARED"
