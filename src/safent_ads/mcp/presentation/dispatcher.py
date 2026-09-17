"""`ToolDispatcher` (T044): unico camino de ejecucion de una herramienta.
Lista blanca -> validacion estricta -> cuota -> alcance -> autorizacion por
negocio -> traza -> handler -> auditoria (contracts/mcp.md §4,
contracts/mcp-tools.md reglas 1 y 4, tasks.md A7). El paso de auditoria va
aqui, no en `mount.py` ni en `composition/`, para que no haya camino de
llamada que lo esquive: exito o fallo, SIEMPRE una fila.

Spec 002 (mcp_oauth) tasks.md T012: `ToolClass.PROPOSAL` exige
`ads:propose` en `caller_scope.granted_scopes` -- `ads:read` (o un llamador
del modo de un solo propietario, sin alcances concedidos, donde la puerta
es el `permission` del puesto) no basta para crear una propuesta. Va ANTES de la
autorizacion por negocio: un token sin ese alcance no debe aprender si el
`business_id` pedido existe o no."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any, Protocol

import structlog
from pydantic import ValidationError

from safent_ads.mcp.application.caller_scope import CallerScope, QuotaPort
from safent_ads.mcp.application.errors import (
    BusinessForbiddenError,
    ForbiddenScopeError,
    RateLimitedError,
    ToolDispatchError,
    ToolNotAllowedError,
    ToolValidationError,
)
from safent_ads.mcp.presentation.registry import ToolClass, ToolDefinition, ToolRegistry
from safent_ads.observability.metrics import record_mcp_tool_call
from safent_ads.shared.read_models.serialization import to_json_value

logger = structlog.get_logger(__name__)


_UNREGISTERED_TOOL_LABEL = "_unregistered"
_PROPOSAL_SCOPE = "ads:propose"
# B-4: UUID nulo, nunca un negocio real (no hay FK en `decision_log`, solo
# `NOT NULL`) -- marcador explicito para cuando ni el argumento de la
# llamada ni el alcance de un unico negocio resuelven un `business_id`:
# la fila se escribe igual, nunca se omite (contracts/mcp.md §4).
_UNRESOLVED_BUSINESS_ID = "00000000-0000-0000-0000-000000000000"


class DecisionAuditPort(Protocol):
    """Puerto local a `presentation` (mismo criterio que `ProposalWritePort`
    en `mcp/application`): `composition/app.py` cablea la implementacion
    real sobre `audit.application.record_decision.RecordDecision`, sin que
    `mcp` importe `Container`."""

    async def record_tool_call(
        self,
        *,
        caller_id: str,
        person_label: str,
        business_id: str,
        tool_name: str,
        permission: str,
        outcome: str,
        error_code: str | None,
        args_digest: str,
        duration_ms: int,
    ) -> None: ...


def _args_digest(raw_arguments: dict[str, Any]) -> str:
    canonical = json.dumps(raw_arguments, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _single_business_id(caller_scope: CallerScope) -> str | None:
    """B-4: respaldo SOLO cuando `_authorize` no pudo (o no tuvo que)
    resolver un `business_id` del argumento de la llamada -- p.ej. una
    herramienta sin `business_id_of`, o un fallo antes de llegar a
    autorizar. Un alcance vacio o con mas de un negocio no tiene un unico
    destino claro; `_audit_call` cae entonces a `_UNRESOLVED_BUSINESS_ID`,
    nunca omite la fila."""
    if len(caller_scope.allowed_business_ids) == 1:
        return next(iter(caller_scope.allowed_business_ids))
    return None


class ToolDispatcher:
    def __init__(
        self, *, registry: ToolRegistry, quota: QuotaPort, audit: DecisionAuditPort | None = None
    ) -> None:
        self._registry = registry
        self._quota = quota
        self._audit = audit

    async def dispatch(
        self,
        tool_name: str,
        raw_arguments: dict[str, Any],
        *,
        caller_scope: CallerScope,
        cycle_id: str | None = None,
    ) -> dict[str, Any]:
        trace_id = str(uuid.uuid4())
        log = logger.bind(trace_id=trace_id, cycle_id=cycle_id, tool=tool_name)
        started_at = time.perf_counter()
        # Etiqueta de metrica acotada a la lista blanca del registro: un
        # `tool_name` que el llamante inventa (rechazado como NOT_ALLOWED
        # mas abajo) NUNCA se usa como valor de etiqueta -- degradaria la
        # cardinalidad acotada de `ads_mcp_tool_calls_total` a "cualquier
        # cadena que un llamante autenticado quiera mandar".
        metric_tool = tool_name if self._registry.get(tool_name) else _UNREGISTERED_TOOL_LABEL
        business_id = None

        try:
            definition = self._require_registered(tool_name, log)
            args = self._validate_args(definition, raw_arguments, log)
            await self._enforce_quota(caller_scope, tool_name, definition.tool_class, log)
            self._enforce_scope(definition, caller_scope, log)
            business_id = self._authorize(definition, args, caller_scope, log)

            log.info("mcp_tool_dispatch_start", business_id=business_id)
            result = await definition.handler(args, caller_scope)
        except ToolNotAllowedError as exc:
            self._record(metric_tool, "not_allowed", started_at)
            await self._audit_call(
                caller_scope=caller_scope,
                tool_name=tool_name,
                raw_arguments=raw_arguments,
                outcome="denied",
                error_code=exc.code,
                started_at=started_at,
                resolved_business_id=business_id,
            )
            raise
        except ToolValidationError as exc:
            self._record(metric_tool, "validation_error", started_at)
            await self._audit_call(
                caller_scope=caller_scope,
                tool_name=tool_name,
                raw_arguments=raw_arguments,
                outcome="validation_error",
                error_code=exc.code,
                started_at=started_at,
                resolved_business_id=business_id,
            )
            raise
        except RateLimitedError as exc:
            self._record(metric_tool, "rate_limited", started_at)
            await self._audit_call(
                caller_scope=caller_scope,
                tool_name=tool_name,
                raw_arguments=raw_arguments,
                outcome="rate_limited",
                error_code=exc.code,
                started_at=started_at,
                resolved_business_id=business_id,
            )
            raise
        except ForbiddenScopeError as exc:
            # Fusion lane/003 + spec 002: el alcance denegado se audita
            # como cualquier otro desenlace (contracts/mcp.md §4, "exito o
            # fallo, SIEMPRE una fila") -- antes de esta fusion T012 solo
            # contaba la metrica.
            self._record(metric_tool, "forbidden_scope", started_at)
            await self._audit_call(
                caller_scope=caller_scope,
                tool_name=tool_name,
                raw_arguments=raw_arguments,
                outcome="denied",
                error_code=exc.code,
                started_at=started_at,
                resolved_business_id=business_id,
            )
            raise
        except BusinessForbiddenError as exc:
            self._record(metric_tool, "business_forbidden", started_at)
            await self._audit_call(
                caller_scope=caller_scope,
                tool_name=tool_name,
                raw_arguments=raw_arguments,
                outcome="denied",
                error_code=exc.code,
                started_at=started_at,
                resolved_business_id=business_id,
            )
            raise
        except Exception as exc:  # noqa: BLE001 - se relanza tal cual, solo se cuenta el desenlace
            self._record(metric_tool, "error", started_at)
            error_code = exc.code if isinstance(exc, ToolDispatchError) else None
            await self._audit_call(
                caller_scope=caller_scope,
                tool_name=tool_name,
                raw_arguments=raw_arguments,
                outcome="error",
                error_code=error_code,
                started_at=started_at,
                resolved_business_id=business_id,
            )
            raise
        log.info("mcp_tool_dispatch_ok", business_id=business_id)
        self._record(metric_tool, "ok", started_at)
        await self._audit_call(
            caller_scope=caller_scope,
            tool_name=tool_name,
            raw_arguments=raw_arguments,
            outcome="ok",
            error_code=None,
            started_at=started_at,
            resolved_business_id=business_id,
        )
        return {"result": to_json_value(result)}

    async def deny(
        self, tool_name: str, raw_arguments: dict[str, Any], *, caller_scope: CallerScope
    ) -> dict[str, Any]:
        """Denegacion por permiso (bug 1, hotfix 0.2.20): `mount.py` llama
        aqui cuando `tool_name` esta en el catalogo completo pero el
        `MCPServer` de este permiso no lo monta -- nunca el handler se
        ejecuta, pero la llamada se audita igual que cualquier otro
        desenlace de `dispatch` (contracts/mcp.md §4: "denegado y
        auditado"), en vez del "unknown tool" generico y silencioso del SDK."""
        started_at = time.perf_counter()
        log = logger.bind(trace_id=str(uuid.uuid4()), tool=tool_name)
        log.warning("mcp_tool_permission_denied", permission=caller_scope.permission.value)
        self._record(tool_name, "permission_denied", started_at)
        await self._audit_call(
            caller_scope=caller_scope,
            tool_name=tool_name,
            raw_arguments=raw_arguments,
            outcome="denied",
            error_code="PERMISSION_DENIED",
            started_at=started_at,
        )
        permission = caller_scope.permission.value
        return {
            "error": {
                "code": "PERMISSION_DENIED",
                "message": f"tu permiso ({permission}) no alcanza a {tool_name}.",
            }
        }

    async def _audit_call(
        self,
        *,
        caller_scope: CallerScope,
        tool_name: str,
        raw_arguments: dict[str, Any],
        outcome: str,
        error_code: str | None,
        started_at: float,
        resolved_business_id: str | None = None,
    ) -> None:
        """B-4: SIEMPRE una fila -- `resolved_business_id` (el que
        `_authorize` ya calculo del argumento de la llamada) manda; el
        alcance de un unico negocio es solo respaldo; sin ninguno de los
        dos, `_UNRESOLVED_BUSINESS_ID` deja constancia igual, nunca se
        omite la fila."""
        if self._audit is None:
            return
        business_id = (
            resolved_business_id or _single_business_id(caller_scope) or _UNRESOLVED_BUSINESS_ID
        )
        duration_ms = round((time.perf_counter() - started_at) * 1000)
        await self._audit.record_tool_call(
            caller_id=caller_scope.caller_id,
            person_label=caller_scope.person_label,
            business_id=business_id,
            tool_name=tool_name,
            permission=caller_scope.permission.value,
            outcome=outcome,
            error_code=error_code,
            args_digest=_args_digest(raw_arguments),
            duration_ms=duration_ms,
        )

    def _record(self, tool: str, outcome: str, started_at: float) -> None:
        elapsed = time.perf_counter() - started_at
        record_mcp_tool_call(tool, outcome=outcome, duration_seconds=elapsed)

    def _require_registered(
        self, tool_name: str, log: structlog.BoundLogger
    ) -> ToolDefinition[Any]:
        definition = self._registry.get(tool_name)
        if definition is None:
            log.warning("mcp_tool_not_allowed")
            raise ToolNotAllowedError(f"herramienta no registrada: {tool_name}")
        return definition

    def _validate_args(
        self,
        definition: ToolDefinition[Any],
        raw_arguments: dict[str, Any],
        log: structlog.BoundLogger,
    ) -> Any:  # noqa: ANN401 - el tipo concreto depende de `definition.args_model`
        try:
            return definition.args_model.model_validate(raw_arguments)
        except ValidationError as exc:
            log.warning("mcp_tool_validation_error", errors=exc.error_count())
            raise ToolValidationError(str(exc)) from exc

    async def _enforce_quota(
        self,
        caller_scope: CallerScope,
        tool_name: str,
        tool_class: ToolClass,
        log: structlog.BoundLogger,
    ) -> None:
        within_quota = await self._quota.check_and_consume(
            caller_id=caller_scope.caller_id, tool_name=tool_name, tool_class=tool_class.value
        )
        if not within_quota:
            log.warning("mcp_tool_rate_limited", caller_id=caller_scope.caller_id)
            raise RateLimitedError(f"cuota agotada para {caller_scope.caller_id}:{tool_name}")

    def _enforce_scope(
        self, definition: ToolDefinition[Any], caller_scope: CallerScope, log: structlog.BoundLogger
    ) -> None:
        if definition.tool_class is not ToolClass.PROPOSAL:
            return
        if not caller_scope.has_scope(_PROPOSAL_SCOPE):
            log.warning("mcp_tool_forbidden_scope", caller_id=caller_scope.caller_id)
            raise ForbiddenScopeError(
                f"{caller_scope.caller_id} sin alcance {_PROPOSAL_SCOPE}"
            )

    def _authorize(
        self,
        definition: ToolDefinition[Any],
        args: Any,  # noqa: ANN401
        caller_scope: CallerScope,
        log: structlog.BoundLogger,
    ) -> str | None:
        if definition.business_id_of is None:
            return None
        business_id = definition.business_id_of(args)
        if not caller_scope.can_access(business_id):
            log.warning("mcp_tool_business_forbidden", business_id=business_id)
            raise BusinessForbiddenError(f"{caller_scope.caller_id} sin acceso a {business_id}")
        return business_id
