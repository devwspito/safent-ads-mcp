"""`SingleOwnerCallerScopeResolver` (aclaracion del dueno, 004 tasks.md
A6/A10): modo de primera clase para el Safent local (motor Hermes), no un
flag de desarrollo. `caller_id="owner"`, permiso `aprobar`, alcance
explicito sobre los negocios activos -- nunca `None`."""

from __future__ import annotations

import pytest

from safent_ads.mcp.application.caller_scope import Permission
from safent_ads.mcp.application.seat_authority import SeatAuthorityDeniedError
from safent_ads.mcp.infrastructure.single_owner_caller_scope_resolver import (
    SingleOwnerCallerScopeResolver,
)

_TOKEN = "owner-mcp-token"  # noqa: S105 - synthetic fixture, never deployed


class _FakeRow:
    def __init__(self, id_: str) -> None:
        self.id = id_


class _FakeResult:
    def __init__(self, rows: list[_FakeRow]) -> None:
        self._rows = rows

    def all(self) -> list[_FakeRow]:
        return self._rows


class _FakeSession:
    def __init__(self, business_ids: list[str]) -> None:
        self._business_ids = business_ids

    async def execute(self, _statement: object) -> _FakeResult:
        return _FakeResult([_FakeRow(bid) for bid in self._business_ids])

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


def _session_factory(business_ids: list[str]):
    def factory() -> _FakeSession:
        return _FakeSession(business_ids)

    return factory


async def test_valid_token_resolves_to_owner_with_approve_over_all_active_businesses() -> None:
    resolver = SingleOwnerCallerScopeResolver(
        _session_factory(["biz-1", "biz-2"]), expected_token=_TOKEN
    )

    scope = await resolver.resolve(_TOKEN)

    assert scope.caller_id == "owner"
    assert scope.person_label == "Dueño"
    assert scope.permission is Permission.APPROVE
    assert scope.allowed_business_ids == frozenset({"biz-1", "biz-2"})


async def test_no_active_businesses_produces_an_empty_scope_never_total() -> None:
    resolver = SingleOwnerCallerScopeResolver(_session_factory([]), expected_token=_TOKEN)

    scope = await resolver.resolve(_TOKEN)

    assert scope.allowed_business_ids == frozenset()
    assert scope.can_access("anything") is False


async def test_wrong_token_is_denied() -> None:
    resolver = SingleOwnerCallerScopeResolver(_session_factory(["biz-1"]), expected_token=_TOKEN)

    with pytest.raises(SeatAuthorityDeniedError):
        await resolver.resolve("wrong-token")


@pytest.mark.parametrize("expected_token", [None, ""])
@pytest.mark.parametrize("presented", [_TOKEN, ""])
async def test_without_a_configured_bearer_every_token_is_denied(
    expected_token: str | None, presented: str
) -> None:
    """Sin bearer configurado no hay puerta que abrir: se deniega, nunca se
    compara contra vacio. `None` es `ADS_MCP_TOKEN` sin definir; la cadena
    VACIA es `ADS_MCP_TOKEN=` a secas en el fichero de entorno, que llega
    como `SecretStr("")` -- si se tratara como un valor mas, un bearer
    vacio casaria en tiempo constante y entraria como dueno (spec 008 fase
    E, seguimiento de T022)."""
    resolver = SingleOwnerCallerScopeResolver(
        _session_factory(["biz-1"]), expected_token=expected_token
    )

    with pytest.raises(SeatAuthorityDeniedError):
        await resolver.resolve(presented)
