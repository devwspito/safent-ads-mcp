"""`SeatCredentialRouter` (004 tasks.md A6): resuelve la credencial UNA vez
por peticion y delega en el sub-app del permiso resuelto. `Host` ajeno se
rechaza antes de tocar la autoridad; Enterprise caido nunca abre un alcance
por defecto."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.testclient import TestClient

from safent_ads.mcp.application.caller_scope import Permission
from safent_ads.mcp.application.seat_authority import SeatAdmission, SeatAuthorityPort
from safent_ads.mcp.infrastructure.enterprise_seat_caller_scope_resolver import (
    EnterpriseSeatCallerScopeResolver,
)
from safent_ads.mcp.presentation.http import (
    CALLER_SCOPE_STATE_ATTR,
    SeatCredentialRouter,
    _allowed_hosts_for,
)
from safent_ads.mcp.testing.fakes import FakeSeatAuthority
from safent_ads.shared.clock import Clock, FixedClock

_PUBLIC_BASE_URL = "https://ads.test"
_CREDENTIAL = "sfa_" + "a" * 64
# `EnterpriseSeatCallerScopeResolver._DEFAULT_INTROSPECTION_LIMIT_PER_MINUTE`
# (contracts/mcp.md §6): repetido aqui solo para dar un valor por defecto
# legible a `_router`, no para redefinir la politica.
_INTROSPECTION_DEFAULT_LIMIT = 120


class _CountingSeatAuthority:
    """Envuelve `FakeSeatAuthority` para contar llamadas reales a
    Enterprise -- Q2 exige que el cupo de introspecciones se compruebe
    ANTES, sin gastar ni una cuando esta agotado."""

    def __init__(self, authority: FakeSeatAuthority) -> None:
        self._authority = authority
        self.call_count = 0

    async def resolve(self, credential: str) -> SeatAdmission:
        self.call_count += 1
        return await self._authority.resolve(credential)


def _echo_app(label: str) -> Starlette:
    async def endpoint(request: Request) -> JSONResponse:
        scope = getattr(request.state, CALLER_SCOPE_STATE_ATTR, None)
        return JSONResponse({"app": label, "caller_id": scope.caller_id if scope else None})

    app = Starlette()
    app.add_route("/mcp", endpoint, methods=["GET"])
    return app


def _router(
    authority: SeatAuthorityPort,
    *,
    clock: Clock | None = None,
    introspection_limit_per_minute: int = _INTROSPECTION_DEFAULT_LIMIT,
) -> SeatCredentialRouter:
    resolver = EnterpriseSeatCallerScopeResolver(
        authority, clock=clock, introspection_limit_per_minute=introspection_limit_per_minute
    )
    apps = {
        Permission.VIEW: _echo_app("view"),
        Permission.PROPOSE: _echo_app("propose"),
        Permission.APPROVE: _echo_app("approve"),
    }
    return SeatCredentialRouter(
        apps, caller_scope_resolver=resolver, allowed_hosts=frozenset({"ads.test"})
    )


def _admission(permission: Permission) -> SeatAdmission:
    return SeatAdmission(
        org_id="org-1",
        user_id="user-1",
        person_label="Ana",
        seat_id="seat-1",
        business_id="biz-1",
        permission=permission,
        expires_at=9_999_999_999,
    )


def test_missing_credential_is_unauthorized() -> None:
    router = _router(FakeSeatAuthority())
    client = TestClient(router, base_url=_PUBLIC_BASE_URL)

    response = client.get("/mcp")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


def test_denied_credential_is_unauthorized() -> None:
    router = _router(FakeSeatAuthority())
    client = TestClient(router, base_url=_PUBLIC_BASE_URL)

    response = client.get("/mcp", headers={"Authorization": f"Bearer {_CREDENTIAL}"})

    assert response.status_code == 401


def test_unavailable_authority_returns_503_never_a_default_scope() -> None:
    authority = FakeSeatAuthority()
    authority.mark_unavailable(_CREDENTIAL)
    router = _router(authority)
    client = TestClient(router, base_url=_PUBLIC_BASE_URL)

    response = client.get("/mcp", headers={"Authorization": f"Bearer {_CREDENTIAL}"})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "SEAT_AUTHORITY_UNAVAILABLE"


def test_foreign_host_is_forbidden_before_touching_the_authority() -> None:
    authority = FakeSeatAuthority()
    authority.admit(_CREDENTIAL, _admission(Permission.VIEW))
    router = _router(authority)
    client = TestClient(router, base_url="https://attacker.invalid")

    response = client.get("/mcp", headers={"Authorization": f"Bearer {_CREDENTIAL}"})

    assert response.status_code == 403


def test_extra_allowed_host_from_ads_mcp_extra_allowed_hosts_is_accepted() -> None:
    """Defecto real en la instancia de produccion (0.2.21): un hostname
    provisional (`ADS_MCP_EXTRA_ALLOWED_HOSTS`, p.ej. un `sslip.io` servido
    por el mismo Caddy) debe pasar el `Host` allow-list igual que el
    dominio publico real."""
    authority = FakeSeatAuthority()
    authority.admit(_CREDENTIAL, _admission(Permission.VIEW))
    resolver = EnterpriseSeatCallerScopeResolver(authority)
    apps = {
        Permission.VIEW: _echo_app("view"),
        Permission.PROPOSE: _echo_app("propose"),
        Permission.APPROVE: _echo_app("approve"),
    }
    extra_host = "ads.example.test"
    router = SeatCredentialRouter(
        apps,
        caller_scope_resolver=resolver,
        allowed_hosts=_allowed_hosts_for(
            _PUBLIC_BASE_URL, extra_allowed_hosts=frozenset({extra_host})
        ),
    )
    client = TestClient(router, base_url=f"https://{extra_host}")

    response = client.get("/mcp", headers={"Authorization": f"Bearer {_CREDENTIAL}"})

    assert response.status_code == 200


def test_a_host_outside_the_extra_allowed_hosts_stays_forbidden() -> None:
    authority = FakeSeatAuthority()
    authority.admit(_CREDENTIAL, _admission(Permission.VIEW))
    resolver = EnterpriseSeatCallerScopeResolver(authority)
    apps = {
        Permission.VIEW: _echo_app("view"),
        Permission.PROPOSE: _echo_app("propose"),
        Permission.APPROVE: _echo_app("approve"),
    }
    router = SeatCredentialRouter(
        apps,
        caller_scope_resolver=resolver,
        allowed_hosts=_allowed_hosts_for(
            _PUBLIC_BASE_URL, extra_allowed_hosts=frozenset({"ads.example.test"})
        ),
    )
    client = TestClient(router, base_url="https://attacker.invalid")

    response = client.get("/mcp", headers={"Authorization": f"Bearer {_CREDENTIAL}"})

    assert response.status_code == 403


@pytest.mark.parametrize(
    "permission,expected_app",
    [(Permission.VIEW, "view"), (Permission.PROPOSE, "propose"), (Permission.APPROVE, "approve")],
)
def test_routes_to_the_sub_app_matching_the_resolved_permission(permission, expected_app) -> None:
    authority = FakeSeatAuthority()
    authority.admit(_CREDENTIAL, _admission(permission))
    router = _router(authority)
    client = TestClient(router, base_url=_PUBLIC_BASE_URL)

    response = client.get("/mcp", headers={"Authorization": f"Bearer {_CREDENTIAL}"})

    assert response.status_code == 200
    body = response.json()
    assert body["app"] == expected_app
    assert body["caller_id"] == "person:user-1"


def test_ciento_veintiuna_introspecciones_en_un_minuto_devuelven_429_sin_llamar_a_enterprise() -> (
    None
):
    fake_authority = FakeSeatAuthority()
    fake_authority.admit(_CREDENTIAL, _admission(Permission.VIEW))
    authority = _CountingSeatAuthority(fake_authority)
    clock = FixedClock(datetime(2026, 9, 14, 12, 0, tzinfo=UTC))
    router = _router(authority, clock=clock, introspection_limit_per_minute=120)
    client = TestClient(router, base_url=_PUBLIC_BASE_URL)
    headers = {"Authorization": f"Bearer {_CREDENTIAL}"}

    for _ in range(120):
        response = client.get("/mcp", headers=headers)
        assert response.status_code == 200

    response = client.get("/mcp", headers=headers)

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "RATE_LIMITED"
    assert int(response.headers["retry-after"]) > 0
    assert authority.call_count == 120
