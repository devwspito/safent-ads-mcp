"""Isolated central ingress. No local-owner login, static bearer or global catalog."""

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.context import Context
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.requests import ClientDisconnect
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from safent_ads import __version__
from safent_ads.composition.container import Container
from safent_ads.composition.managed_service import TOOL_MODELS, ManagedAdsService
from safent_ads.iam.application.managed_ads_authority import ManagedAdsDenied, ManagedAdsUnavailable
from safent_ads.mcp.application.caller_scope import CallerScope, Permission
from safent_ads.mcp.application.errors import ToolDispatchError
from safent_ads.mcp.infrastructure.rate_limiter import InMemoryQuota
from safent_ads.mcp.presentation.args import ToolArgs
from safent_ads.mcp.presentation.dispatcher import ToolDispatcher
from safent_ads.mcp.presentation.http import _transport_security_for
from safent_ads.mcp.presentation.registry import ToolClass, ToolDefinition, ToolRegistry
from safent_ads.proposals.application.submit_approval import ProposalApprovalDeniedError

_MAX_BODY_BYTES = 32768
_MAX_TOKEN_CHARS = 8192


class BoundedManagedBody:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return
        body = bytearray()
        while True:
            event = await receive()
            if event["type"] == "http.disconnect":
                return
            body.extend(event.get("body", b""))
            if len(body) > _MAX_BODY_BYTES:
                await JSONResponse(
                    {"error": {"code": "MANAGED_REQUEST_INVALID"}},
                    status_code=413,
                    headers={"Cache-Control": "no-store"},
                )(scope, receive, send)
                return
            if not event.get("more_body", False):
                break
        delivered = False

        async def replay() -> Any:
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, replay, send)


class HumanSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    assertion: str = Field(min_length=1, max_length=_MAX_TOKEN_CHARS, repr=False)


def _bearer(request: Request) -> str:
    value = request.headers.get("authorization", "")
    if not value.startswith("Bearer ") or len(value) > _MAX_TOKEN_CHARS + 7:
        raise ManagedAdsDenied("managed_credentials_required")
    return value[7:]


async def _body(request: Request) -> dict[str, Any]:
    chunks = bytearray()
    async for chunk in request.stream():
        chunks.extend(chunk)
        if len(chunks) > _MAX_BODY_BYTES:
            raise ManagedAdsDenied("managed_request_invalid")
    result = json.loads(chunks)
    if not isinstance(result, dict):
        raise ManagedAdsDenied("managed_request_invalid")
    return result


class ManagedDispatcher:
    """Reuse the existing MCP allowlist, validation, quota and tracing pipeline."""

    def __init__(self, service: ManagedAdsService) -> None:
        self.service = service
        self.quota = InMemoryQuota(clock=service.container.clock)

    async def dispatch(self, token: str, name: str, raw: dict[str, Any]) -> dict[str, Any]:
        if name not in TOOL_MODELS:
            raise ManagedAdsDenied("managed_tool_not_allowed")
        admission = await self.service.admit(token, "read")
        binding = admission.binding
        # `permission`/`person_label` son decorativos aqui: este camino filtra
        # por tool_class explicito en `definition()` mas abajo, no por
        # `registries_by_permission` (004 no toca instancias gestionadas).
        scope = CallerScope(
            caller_id=str(binding.grant_id),
            allowed_business_ids=frozenset({str(binding.account.business_id)}),
            permission=Permission.PROPOSE,
            person_label="Instancia gestionada",
        )

        def definition(tool: str, model: type[ToolArgs]) -> ToolDefinition[ToolArgs]:
            async def handle(args: ToolArgs, _scope: CallerScope) -> object:
                # Fresh operation-specific admission, not the routing claim or read ceiling.
                return await self.service.call(token, tool, args.model_dump(mode="json"))

            return ToolDefinition(
                tool,
                "Cuenta asignada por Enterprise; sin aprobación automática.",
                model,
                ToolClass.PROPOSAL if tool.startswith("propose_") else ToolClass.READ,
                handle,
                None,
            )

        registry = ToolRegistry(definition(tool, model) for tool, model in TOOL_MODELS.items())
        return await ToolDispatcher(registry=registry, quota=self.quota).dispatch(
            name, raw, caller_scope=scope
        )


def _tool(
    dispatcher: ManagedDispatcher, name: str, model: type[ToolArgs]
) -> Callable[..., Awaitable[dict[str, Any]]]:
    async def wrapper(args: ToolArgs, ctx: Context) -> dict[str, Any]:
        request = ctx.request_context.request
        if request is None:
            return {"error": {"code": "MANAGED_DENIED"}}
        try:
            return await dispatcher.dispatch(_bearer(request), name, args.model_dump(mode="json"))
        except (ManagedAdsDenied, ManagedAdsUnavailable, ToolDispatchError, ValidationError):
            return {"error": {"code": "MANAGED_DENIED"}}

    wrapper.__name__ = name
    wrapper.__annotations__ = {"args": model, "ctx": Context, "return": dict[str, Any]}
    return wrapper


def create_managed_app(container: Container) -> FastAPI:
    service = ManagedAdsService(container)
    dispatcher = ManagedDispatcher(service)
    server = MCPServer(name="ads-managed", version=__version__)
    for name, model in TOOL_MODELS.items():
        server.add_tool(_tool(dispatcher, name, model), name=name)
    mcp = server.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        max_request_body_size=_MAX_BODY_BYTES,
        transport_security=_transport_security_for(container.settings.public_base_url),
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            async with mcp.router.lifespan_context(mcp):
                yield
        finally:
            await container.aclose()

    app = FastAPI(
        title="safent-ads-managed",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.container = container

    @app.middleware("http")
    async def guard(request: Request, call_next: Callable[[Request], Awaitable[Any]]) -> Any:
        try:
            if request.url.path == "/mcp":
                await service.admit(_bearer(request), "read")
            response = await call_next(request)
        except (ManagedAdsDenied, ProposalApprovalDeniedError):
            response = JSONResponse({"error": {"code": "MANAGED_DENIED"}}, status_code=403)
        except ManagedAdsUnavailable:
            response = JSONResponse({"error": {"code": "MANAGED_UNAVAILABLE"}}, status_code=503)
        except (ValidationError, ValueError, ClientDisconnect, ToolDispatchError):
            response = JSONResponse({"error": {"code": "MANAGED_REQUEST_INVALID"}}, status_code=400)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/api/v1/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "profile": "managed-central"}

    @app.post("/api/v1/managed/tools/{name}")
    async def tool(name: str, request: Request) -> JSONResponse:
        result = await dispatcher.dispatch(_bearer(request), name, await _body(request))
        return JSONResponse(jsonable_encoder(result))

    @app.post("/api/v1/managed/human-approval")
    async def approve(request: Request) -> JSONResponse:
        # Only the Enterprise server submits this one-use assertion. No owner cookies.
        body = HumanSubmission.model_validate(await _body(request))
        return JSONResponse(jsonable_encoder(await service.approve(body.assertion)))

    app.router.routes.append(Route("/mcp", endpoint=mcp))
    app.add_middleware(BoundedManagedBody)
    return app
