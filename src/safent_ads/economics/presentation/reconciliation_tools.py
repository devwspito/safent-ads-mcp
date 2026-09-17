"""Las 5 herramientas nuevas de medicion (T159/T160, tool-surface.md §2.4/
§2.5, profitability-engine.md §9: "bloqueantes: sin medicion integra no se
sube gasto"): `build_tracking_template`, `validate_utm_consistency`,
`get_crm_reconciliation`, `compare_attribution_windows`,
`get_conversion_bridge_health`.

Mismo patron que `economics.presentation.mcp_tools` (T161):
`build_reconciliation_tool_specs(...)` es el unico punto de montaje, la
lane que cablea `mcp/` adapta cada `ToolSpec` a un `ToolDefinition` en una
linea (ver `tool_spec.py`). Modulo nuevo, no una extension del de T161, a
proposito: agrupa las herramientas que profitability-engine.md §9 promueve
a bloqueantes, sin tocar el fichero de las 5 de P1 ya cableadas."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from safent_ads.economics.application.compare_attribution_windows import (
    CompareAttributionWindows,
)
from safent_ads.economics.application.get_conversion_bridge_health import (
    GetConversionBridgeHealth,
)
from safent_ads.economics.application.get_crm_reconciliation import GetCrmReconciliation
from safent_ads.economics.domain.tracking import build_tracking_template, find_utm_inconsistency
from safent_ads.economics.presentation import args as a
from safent_ads.economics.presentation.serialization import to_json_dict
from safent_ads.economics.presentation.tool_spec import ToolSpec
from safent_ads.shared.ids import BusinessId, EntityRef


@dataclass(frozen=True, kw_only=True, slots=True)
class TrackedEntitySnapshot:
    entity_ref: str
    observed_final_url_suffix: str | None


class AdTrackingObservationPort(Protocol):
    """Puerto de `validate_utm_consistency`: URL final observada por
    entidad de la cuenta. Vive en este modulo, no en `economics.
    application.ports`, porque `economics` no modela hoy ninguna entidad
    de plataforma -- es un puerto propio de esta herramienta, no del
    bounded context (mismo criterio que `EconomicsQueryService` frente a
    `ToolSpec`: la lane de superficie decide como se cablea)."""

    async def list_for_account(
        self, *, business_id: BusinessId, account_ref: str
    ) -> Sequence[TrackedEntitySnapshot]: ...


class NotYetWiredAdTrackingObservationPort:
    """Ninguna fuente real de `final_url_suffix` por anuncio existe todavia
    en el esquema (`ad_entities` no lo modela -- gap preexistente, T159 no
    lo resuelve). Devolver `[]` es honesto: 'sin datos', nunca un veredicto
    inventado que aparente ser una medicion (profitability-engine.md §7)."""

    async def list_for_account(
        self,
        business_id: BusinessId,  # noqa: ARG002 - forma exacta del puerto
        account_ref: str,  # noqa: ARG002
    ) -> Sequence[TrackedEntitySnapshot]:
        return []


def build_reconciliation_tool_specs(
    *,
    crm_reconciliation: GetCrmReconciliation,
    compare_windows: CompareAttributionWindows,
    bridge_health: GetConversionBridgeHealth,
    ad_tracking: AdTrackingObservationPort | None = None,
) -> list[ToolSpec]:
    tracking_port = ad_tracking or NotYetWiredAdTrackingObservationPort()
    return [
        ToolSpec(
            name="build_tracking_template",
            description="Plantilla UTM y sufijo de URL final para una campana.",
            args_model=a.BuildTrackingTemplateArgs,
            handler=_build_tracking_template(),
        ),
        ToolSpec(
            name="validate_utm_consistency",
            description="Entidades de una cuenta con UTM roto o divergente.",
            args_model=a.ValidateUtmConsistencyArgs,
            handler=_validate_utm_consistency(tracking_port),
        ),
        ToolSpec(
            name="get_crm_reconciliation",
            description="Plataforma vs CRM en una ventana, con el rezago declarado.",
            args_model=a.GetCrmReconciliationArgs,
            handler=_get_crm_reconciliation(crm_reconciliation),
        ),
        ToolSpec(
            name="compare_attribution_windows",
            description="Misma entidad con ventanas de 1/7/28 dias y el CRM al lado.",
            args_model=a.CompareAttributionWindowsArgs,
            handler=_compare_attribution_windows(compare_windows),
        ),
        ToolSpec(
            name="get_conversion_bridge_health",
            description="WhatsApp y llamada como conversion: ¿siguen llegando?",
            args_model=a.GetConversionBridgeHealthArgs,
            handler=_get_conversion_bridge_health(bridge_health),
        ),
    ]


def _build_tracking_template() -> Any:
    async def handler(args: a.BuildTrackingTemplateArgs) -> dict[str, Any]:
        template = build_tracking_template(
            platform=args.platform,
            campaign_ref=args.campaign_ref,
            offering_code=args.offering_code,
            calendar_event_code=args.calendar_event_code,
        )
        return {"final_url_suffix": template.final_url_suffix, "utm": dict(template.utm)}

    return handler


def _validate_utm_consistency(tracking_port: AdTrackingObservationPort) -> Any:
    async def handler(args: a.ValidateUtmConsistencyArgs) -> dict[str, Any]:
        snapshots = await tracking_port.list_for_account(
            business_id=BusinessId.parse(args.business_id), account_ref=args.account_ref
        )
        findings = []
        for snapshot in snapshots:
            template = build_tracking_template(
                platform=EntityRef.parse(snapshot.entity_ref).platform.value,
                campaign_ref=snapshot.entity_ref,
            )
            issue = find_utm_inconsistency(
                template=template, observed_final_url_suffix=snapshot.observed_final_url_suffix
            )
            if issue is not None:
                findings.append({"entity_ref": snapshot.entity_ref, "issue": issue.value})
        return {"findings": findings}

    return handler


def _get_crm_reconciliation(service: GetCrmReconciliation) -> Any:
    async def handler(args: a.GetCrmReconciliationArgs) -> dict[str, Any]:
        view = await service.execute(
            business_id=BusinessId.parse(args.business_id),
            window_start=args.window_start,
            window_end=args.window_end,
            lag_days=args.lag_days,
        )
        return to_json_dict(view)

    return handler


def _compare_attribution_windows(service: CompareAttributionWindows) -> Any:
    async def handler(args: a.CompareAttributionWindowsArgs) -> dict[str, Any]:
        rows = await service.execute(
            business_id=BusinessId.parse(args.business_id),
            entity_ref=EntityRef.parse(args.entity_ref),
            as_of=args.as_of,
        )
        return {"windows": [to_json_dict(row) for row in rows]}

    return handler


def _get_conversion_bridge_health(service: GetConversionBridgeHealth) -> Any:
    async def handler(args: a.GetConversionBridgeHealthArgs) -> dict[str, Any]:
        rows = await service.execute(
            business_id=BusinessId.parse(args.business_id), as_of=args.as_of
        )
        return {"bridges": [to_json_dict(row) for row in rows]}

    return handler
