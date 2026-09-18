"""El transporte streamable-http responde en la URL que publica el contrato:
`https://<host>/mcp`, sin barra final (contracts/mcp.md), y enruta por
permiso (004 tasks.md A6): `tools/list` con `ver` no ve ninguna
`propose_*`; con `proponer` ve 97; sin credencial, `401`; con Enterprise
caido, `503`; `Host` ajeno, `403`. Prueba de extremo a extremo contra el
transporte real (`create_app`), no contra el dispatcher pelado.

Recuento pinned (H-follow-up, revision de codigo 2026-09-15, mismo criterio
que `tests/e2e/journeys/conftest.py`): 69->74 READ (kit_services +3,
Cloudflare +2), 90->97 PROPOSAL (+19->+20 pinned en
`test_catalog_registries_by_permission.py`, pero `build_api_settings()`
aqui deja `ADS_CAMPAIGN_PACKAGES_ENABLED` en su `False` por defecto, asi
que `propose_campaign_package` no cuenta, +3 CATALOG_WRITE de Cloudflare),
92->99 APPROVE (97 + 2 CONNECTION_WRITE, sin cambios en esa cuenta).

Se conduce la app ASGI real con el cliente del propio SDK -- `initialize` +
`tools/list` de verdad, no una peticion HTTP a mano -- porque el fallo que
estas pruebas fijan no se ve en una peticion suelta: con `app.mount("/mcp")`
el `/mcp` pelado se quedaba en el `307` de `redirect_slashes` y solo `/mcp/`
llegaba al transporte. `stateless_http=True, json_response=True`
(contracts/mcp.md): la respuesta es JSON llano, no SSE, y no hay
`Mcp-Session-Id` que mantener entre peticiones.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from tests.unit.composition.factories import build_api_settings

from safent_ads.composition.app import create_app
from safent_ads.mcp.application.caller_scope import CallerScope, Permission
from safent_ads.mcp.application.seat_authority import (
    SeatAuthorityDeniedError,
    SeatAuthorityUnavailableError,
)

pytestmark = pytest.mark.integration

_BASE_URL = "https://ads.test.ts.net"
_ENDPOINT = f"{_BASE_URL}/mcp"
_CREDENTIAL = "sfa_" + "a" * 64  # noqa: S105 - synthetic local test fixture, never deployed


class _FixedScopeResolver:
    """Doble de `CallerScopeResolverPort`: nunca toca la red, resuelve una
    credencial fija por permiso o levanta la denegacion/indisponibilidad
    que `SeatCredentialRouter` traduce a 401/503."""

    def __init__(self, *, scope: CallerScope | None = None, unavailable: bool = False) -> None:
        self._scope = scope
        self._unavailable = unavailable

    async def resolve(self, bearer_token: str) -> CallerScope:
        if self._unavailable:
            raise SeatAuthorityUnavailableError("seat_authority_unavailable")
        if self._scope is None or bearer_token != _CREDENTIAL:
            raise SeatAuthorityDeniedError("ads_seat_invalid")
        return self._scope


def _scope(permission: Permission) -> CallerScope:
    return CallerScope("person:ana", frozenset({"biz-1"}), permission, "Ana")


@asynccontextmanager
async def _mcp_http_client(
    resolver: _FixedScopeResolver,
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(build_api_settings(), caller_scope_resolver=resolver)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=_BASE_URL
        ) as client:
            yield client


def _rpc(
    method: str, *, id_: int = 1, params: dict[str, object] | None = None
) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}


async def _initialize_and_list_tools(
    client: httpx.AsyncClient, *, credential: str | None
) -> httpx.Response:
    headers = {"Accept": "application/json, text/event-stream"}
    if credential is not None:
        headers["Authorization"] = f"Bearer {credential}"
    init = await client.post(
        "/mcp",
        headers=headers,
        json=_rpc(
            "initialize",
            params={
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "seat-probe", "version": "0"},
            },
        ),
    )
    if init.status_code != 200:
        return init
    return await client.post("/mcp", headers=headers, json=_rpc("tools/list", id_=2))


async def test_view_permission_sees_only_read_tools_never_propose() -> None:
    resolver = _FixedScopeResolver(scope=_scope(Permission.VIEW))
    async with _mcp_http_client(resolver) as client:
        response = await _initialize_and_list_tools(client, credential=_CREDENTIAL)

    assert response.status_code == 200, response.text
    assert "mcp-session-id" not in response.headers
    names = {tool["name"] for tool in response.json()["result"]["tools"]}
    assert len(names) == 78
    assert not any(name.startswith("propose_") for name in names)


async def test_propose_permission_sees_read_and_proposal_tools() -> None:
    resolver = _FixedScopeResolver(scope=_scope(Permission.PROPOSE))
    async with _mcp_http_client(resolver) as client:
        response = await _initialize_and_list_tools(client, credential=_CREDENTIAL)

    assert response.status_code == 200, response.text
    names = {tool["name"] for tool in response.json()["result"]["tools"]}
    assert len(names) == 102
    assert "propose_budget_change" in names


async def test_missing_credential_is_unauthorized() -> None:
    """Fusion con spec 002 (mcp_oauth), contracts/oauth.md SS2: el 401 de
    `/mcp` es el de RFC 9728 -- mismo cuerpo y misma cabecera
    `WWW-Authenticate` que `GET /mcp/health` (threat-model.md C-48), para
    que un cliente MCP descubra solo el servidor de autorizacion. Antes de
    la fusion era el sobre `{"error": {"code": "UNAUTHORIZED"}}` propio."""
    resolver = _FixedScopeResolver(scope=_scope(Permission.VIEW))
    async with _mcp_http_client(resolver) as client:
        response = await _initialize_and_list_tools(client, credential=None)

    assert response.status_code == 401
    assert response.json() == {
        "error": "invalid_token",
        "error_description": "Authentication required",
    }
    assert "resource_metadata=" in response.headers["www-authenticate"]


async def _chunked_body(total_bytes: int, *, chunk_size: int = 65_536) -> AsyncIterator[bytes]:
    sent = 0
    while sent < total_bytes:
        chunk = min(chunk_size, total_bytes - sent)
        yield b"x" * chunk
        sent += chunk


async def test_r3_a_chunked_body_over_the_cap_is_rejected_mid_stream() -> None:
    """R-3: sin `Content-Length` fiable (equivalente a `Transfer-Encoding:
    chunked`) el tope de M-4 no contaba nada -- con una credencial de
    puesto valida (la revision: "explotarlo exige una credencial de puesto
    valida") un cuerpo de 20 MB se habria leido entero sin cota propia de
    este servicio. `ContentLengthLimitMiddleware` envuelve el
    `SeatCredentialRouter` entero (`composition/app.py::
    _route_mcp_transport`) contando bytes en marcha: el `413` llega mucho
    antes de los 20 MB. (El SDK MCP interno tiene ademas su propio tope de
    4 MiB sobre el mismo `receive` envuelto -- ambas capas fallan cerrado
    con `Transfer-Encoding: chunked`; la prueba que aisla el diff propio
    de `ContentLengthLimitMiddleware`, sin el SDK de por medio, vive en
    `tests/unit/mcp/presentation/test_http.py`.)"""
    resolver = _FixedScopeResolver(scope=_scope(Permission.VIEW))
    app = create_app(build_api_settings(), caller_scope_resolver=resolver)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=_BASE_URL
        ) as client:
            response = await client.post(
                "/mcp",
                headers={
                    "Accept": "application/json, text/event-stream",
                    "Authorization": f"Bearer {_CREDENTIAL}",
                },
                content=_chunked_body(20_000_000),
            )

    assert response.status_code == 413


async def test_enterprise_unavailable_never_falls_back_to_a_default_scope() -> None:
    resolver = _FixedScopeResolver(unavailable=True)
    async with _mcp_http_client(resolver) as client:
        response = await _initialize_and_list_tools(client, credential=_CREDENTIAL)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "SEAT_AUTHORITY_UNAVAILABLE"


async def test_foreign_host_is_forbidden() -> None:
    resolver = _FixedScopeResolver(scope=_scope(Permission.VIEW))
    app = create_app(build_api_settings(), caller_scope_resolver=resolver)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://attacker.invalid"
        ) as client:
            response = await _initialize_and_list_tools(client, credential=_CREDENTIAL)

    assert response.status_code == 403


async def test_the_published_endpoint_never_redirects() -> None:
    resolver = _FixedScopeResolver(scope=_scope(Permission.VIEW))
    exchanges: list[tuple[str, str, int]] = []

    async def record(response: httpx.Response) -> None:
        exchanges.append((response.request.method, response.request.url.path, response.status_code))

    app = create_app(build_api_settings(), caller_scope_resolver=resolver)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=_BASE_URL,
            event_hooks={"response": [record]},
        ) as client:
            await _initialize_and_list_tools(client, credential=_CREDENTIAL)

    assert exchanges, "el cliente no llego a hacer ninguna peticion"
    assert not [ex for ex in exchanges if 300 <= ex[2] < 400], exchanges
    assert {path for _, path, _ in exchanges} == {"/mcp"}, exchanges


async def test_single_owner_mode_lists_exactly_the_approve_registry(
    isolated_database_url: str,
) -> None:
    """Aclaracion del dueno: el Safent local (motor Hermes) sigue usando el
    mismo bearer estatico de siempre y ve el catalogo COMPLETO (`aprobar`),
    nunca uno recortado -- mismo contrato de `/mcp` que ya conoce."""
    settings = build_api_settings(
        database_url=isolated_database_url,
        seat_authority_enabled=False,
        single_owner_mode=True,
        # T045 (spec 008, CWE-288): el bearer estatico solo abre la puerta
        # con el interruptor encendido a proposito (companion mode lo hace
        # por si solo) -- nunca por tener `ADS_MCP_TOKEN` en el entorno.
        mcp_static_token_enabled=True,
    )
    assert settings.mcp_token is not None
    async with container_business(isolated_database_url):
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=_BASE_URL
            ) as client:
                response = await _initialize_and_list_tools(
                    client, credential=settings.mcp_token.get_secret_value()
                )

    assert response.status_code == 200, response.text
    names = {tool["name"] for tool in response.json()["result"]["tools"]}
    # `aprobar` (100 de `proponer` + las 2 `CONNECTION_WRITE`, A5/Anadido del
    # dueno): el modo de un solo propietario ve el catalogo COMPLETO.
    assert len(names) == 104
    assert "propose_budget_change" in names
    assert "connect_platform_account" in names


async def test_single_owner_mode_with_static_token_disabled_rejects_the_configured_bearer(
    isolated_database_url: str,
) -> None:
    """T045 (spec 008, CWE-288): `ADS_MCP_TOKEN` sigue en el entorno pero
    `ADS_MCP_STATIC_TOKEN_ENABLED` esta en su valor por defecto (`false`) --
    el mismo bearer que la prueba de arriba acepta con el interruptor
    encendido tiene que rebotar en `/mcp`, extremo a extremo, a traves del
    unico verificador real (`CompositeTokenVerifier` + `SingleOwnerCaller
    ScopeResolver`), nunca solo a nivel de unidad."""
    settings = build_api_settings(
        database_url=isolated_database_url,
        seat_authority_enabled=False,
        single_owner_mode=True,
    )
    assert settings.mcp_token is not None
    async with container_business(isolated_database_url):
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=_BASE_URL
            ) as client:
                response = await _initialize_and_list_tools(
                    client, credential=settings.mcp_token.get_secret_value()
                )

    assert response.status_code == 401, response.text


@asynccontextmanager
async def container_business(database_url: str) -> AsyncIterator[None]:
    """Al menos un negocio activo: `SingleOwnerCallerScopeResolver` deniega
    todo sobre un alcance vacio, igual que cualquier otro `CallerScope`.
    `isolated_database_url` es de ambito de sesion (`tests/conftest.py`):
    limpia su propia fila para no ensuciar otros tests."""
    business_id = uuid.uuid4()
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO businesses (id, slug, name, timezone, reference_currency) "
                    "VALUES (:id, 'single-owner-test', 'Negocio de contrato', "
                    "'Europe/Madrid', 'EUR')"
                ),
                {"id": business_id},
            )
        yield
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM businesses WHERE id = :id"), {"id": business_id}
            )
        await engine.dispose()
