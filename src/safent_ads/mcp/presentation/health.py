"""`GET /mcp/health` (contrato del companion, definido en el runtime:
"Lo que debe exponer el servicio de ads"): mismo verificador que `/mcp`
(`mcp_oauth.presentation.token_verifier.CompositeTokenVerifier`, un unico
camino de verificacion -- threat-model.md C-48) y el mismo cuerpo/cabecera
`WWW-Authenticate` de 401 (`mcp.presentation.http.unauthorized_response`).

M1 de la revision de seguridad (16-sep): `verify_token` solo comprueba
firma/caducidad -- `/mcp` ademas exige `ads:read` en `AuthCredentials.scopes`
y el `resource` canonico via `RequireAuthMiddleware`/`BearerAuthBackend`
del SDK (`mcp/presentation/http.py`). Esta ruta vive FUERA de esa
sub-app (Starlette la resuelve por orden de registro, ver mas abajo), asi
que replica ambas comprobaciones a mano -- si no, un token con solo
`ads:propose` o emitido para otro recurso pasaria aqui y no en `/mcp`.

`composition/app.py` registra este router ANTES de `app.mount("/mcp",
mcp_app)`: Starlette resuelve por orden de registro (mismo patron que
`_mount_panel_spa`), asi que esta ruta exacta gana sobre el catch-all del
transporte streamable-http montado despues bajo el mismo prefijo."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from mcp.server.auth.provider import AccessToken, TokenVerifier

from safent_ads.mcp.application.health import AccountsLinked, GetHealthStatus, HealthReport
from safent_ads.mcp.presentation.http import unauthorized_response
from safent_ads.shared.bearer import extract_bearer_token


def build_mcp_health_router(
    *,
    get_health_status: GetHealthStatus,
    token_verifier: TokenVerifier,
    resource_metadata_url: str,
    required_scope: str,
    canonical_resource: str,
) -> APIRouter:
    router = APIRouter()

    @router.get("/mcp/health", include_in_schema=True)
    async def mcp_health(request: Request) -> JSONResponse:
        token = extract_bearer_token(request.headers.get("authorization", ""))
        access_token = None if token is None else await token_verifier.verify_token(token)
        if not _authorized(
            access_token, required_scope=required_scope, canonical_resource=canonical_resource
        ):
            return unauthorized_response(resource_metadata_url=resource_metadata_url)
        report = await get_health_status.execute()
        return JSONResponse(_serialize(report))

    return router


def _authorized(
    access_token: AccessToken | None, *, required_scope: str, canonical_resource: str
) -> bool:
    if access_token is None:
        return False
    return (
        required_scope in access_token.scopes and access_token.resource == canonical_resource
    )


def _serialize(report: HealthReport) -> dict[str, object]:
    return {
        "status": report.status,
        "contract_version": report.contract_version,
        "accounts_linked": _serialize_accounts(report.accounts_linked),
        "db": report.db,
    }


def _serialize_accounts(accounts: AccountsLinked) -> dict[str, bool]:
    return {"google": accounts.google, "meta": accounts.meta}
