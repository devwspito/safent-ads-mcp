"""Traduce los DTOs de `AdsPlatformPort` (dataclasses de dominio) a JSON
para la respuesta del socket. Frontera de presentacion: aqui, y solo aqui,
los tipos de `accounts.domain` se convierten a `dict`/`str` planos."""

from __future__ import annotations

import base64
from collections.abc import Mapping, Sequence
from typing import Any

from safent_ads.accounts.application.ports import (
    AdEntitySnapshot,
    EntityStateSnapshot,
    MetricFactSnapshot,
    PlatformAssetHandle,
    WriteOutcome,
)
from safent_ads.accounts.domain.budget import Budget
from safent_ads.accounts.domain.money import Money
from safent_ads.broker.application.app_credentials_service import AppCredentialsStatus
from safent_ads.broker.application.oauth_connect_flow import (
    BeginResult,
    CompleteResult,
    DiscoveredAccount,
    StatusResult,
)
from safent_ads.broker.application.render_image import RenderImageResult
from safent_ads.broker.domain.hard_caps_policy import EffectiveAmounts, PanelAmounts
from safent_ads.broker.infrastructure.hard_caps_service import CapsView, EnvelopeUsage
from safent_ads.shared.ids import EntityRef


def _money(money: Money | None) -> dict[str, Any] | None:
    if money is None:
        return None
    return {"minor_units": money.minor_units, "currency": money.currency}


def _budget(budget: Budget | None) -> dict[str, Any] | None:
    if budget is None:
        return None
    return {"amount": _money(budget.amount), "kind": budget.kind.value}


def _ref(ref: EntityRef | None) -> str | None:
    return None if ref is None else str(ref)


def serialize_ad_entity_snapshot(snapshot: AdEntitySnapshot) -> dict[str, Any]:
    return {
        "entity_ref": str(snapshot.entity_ref),
        "parent_ref": str(snapshot.parent_ref),
        "name": snapshot.name,
        "status": snapshot.status.value,
        "is_controllable": snapshot.is_controllable,
        "learning_state": snapshot.learning_state.value,
        "budget": _budget(snapshot.budget),
        "bid_target": _money(snapshot.bid_target),
        "shared_budget_ref": _ref(snapshot.shared_budget_ref),
        "canonical_state": dict(snapshot.canonical_state),
        "fetched_at": snapshot.fetched_at.isoformat(),
    }


def serialize_entity_state_snapshot(snapshot: EntityStateSnapshot) -> dict[str, Any]:
    return {
        "entity_ref": str(snapshot.entity_ref),
        "status": snapshot.status.value,
        "is_controllable": snapshot.is_controllable,
        "canonical_state": dict(snapshot.canonical_state),
        "fetched_at": snapshot.fetched_at.isoformat(),
    }


def serialize_metric_fact_snapshot(fact: MetricFactSnapshot) -> dict[str, Any]:
    return {
        "entity_ref": str(fact.entity_ref),
        "stat_date": fact.stat_date.isoformat(),
        "stat_hour": fact.stat_hour,
        "currency": fact.currency,
        "spend": _money(fact.spend),
        "impressions": fact.impressions,
        "clicks": fact.clicks,
        "reach": fact.reach,
        "frequency": fact.frequency,
        "conversions_by_kind": dict(fact.conversions_by_kind),
        "conversion_value": _money(fact.conversion_value),
        "video_views_3s": fact.video_views_3s,
        "video_views_75pct": fact.video_views_75pct,
        "search_lost_is_budget": fact.search_lost_is_budget,
        "search_lost_is_rank": fact.search_lost_is_rank,
    }


def _optional_iso(value: Any) -> str | None:  # noqa: ANN401 - datetime | None sin importar aqui
    return None if value is None else value.isoformat()


def serialize_write_outcome(outcome: WriteOutcome) -> dict[str, Any]:
    return {
        "outcome": outcome.outcome,
        "applied_value": outcome.applied_value,
        "state_hash_after": outcome.state_hash_after,
        "error_code": outcome.error_code,
        "platform_request_id": outcome.platform_request_id,
    }


def serialize_platform_asset_handle(handle: PlatformAssetHandle) -> dict[str, Any]:
    """`op: upload_asset` (003-paquete-de-campana BL-6): `platform_asset_id`
    es el manejador opaco que `{creative_of:X}` resuelve (Meta `image_hash`,
    Google `asset resource name`); `preview_url` es solo informativo, nunca
    lo que se firma ni lo que publica."""
    return {"platform_asset_id": handle.platform_asset_id, "preview_url": handle.preview_url}


def serialize_oauth_begin(result: BeginResult) -> dict[str, Any]:
    return {
        "connection_id": result.connection_id,
        "authorization_url": result.authorization_url,
        "state": result.state,
        "expires_at": result.expires_at.isoformat(),
    }


def _serialize_discovered_account(account: DiscoveredAccount) -> dict[str, Any]:
    return {
        "connection_id": account.connection_id,
        "business_id": account.business_id,
        "owner_id": account.owner_id,
        "platform": account.platform.value,
        "external_account_id": account.external_account_id,
        "label": account.label,
        "currency": account.currency,
        "timezone": account.timezone,
        "api_tier": account.api_tier.value,
        "credential_ref_id": str(account.credential_ref_id),
        "scopes": list(account.scopes),
        "obtained_at": account.obtained_at.isoformat(),
        "expires_at": _optional_iso(account.expires_at),
    }


def serialize_oauth_complete(result: CompleteResult) -> dict[str, Any]:
    return {"accounts": [_serialize_discovered_account(account) for account in result.accounts]}


def serialize_credential_status(result: StatusResult) -> dict[str, Any]:
    return {
        "status": result.status,
        "scopes": list(result.scopes),
        "expires_at": _optional_iso(result.expires_at),
        "last_validated_at": _optional_iso(result.last_validated_at),
    }


def serialize_app_credentials_status(status: AppCredentialsStatus) -> dict[str, Any]:
    return {
        "platform": status.platform.value,
        "configured": status.configured,
        "client_id_masked": status.client_id_masked,
        "login_customer_id_masked": status.login_customer_id_masked,
        "updated_at": _optional_iso(status.updated_at),
        "client_type": status.client_type,
    }


def serialize_gaql_rows(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """`GoogleAdsAdapter.run_gaql` filas heterogeneas del SDK (enum-name,
    int, Decimal...) -> `dict[str, str]` estables sobre JSON, misma forma
    que `mcp.application.dto.GaqlResult.rows`."""
    serialized = [{key: _gaql_value(value) for key, value in row.items()} for row in rows]
    return {"rows": serialized, "row_count": len(serialized)}


def _gaql_value(value: Any) -> str:  # noqa: ANN401 - valor crudo del SDK ya extraido
    return "" if value is None else str(value)


def serialize_rendered_image(result: RenderImageResult) -> dict[str, Any]:
    """`render_image`: nunca se loguea `result.payload` ni el prompt que lo
    produjo (ese vive solo en la peticion, que `dispatcher.py` tampoco
    registra) -- solo cruzan el log el nombre del renderizador y el coste."""
    rendered = result.rendered
    return {
        "image_base64": base64.b64encode(result.payload).decode("ascii"),
        "content_type": result.content_type,
        "model_name": result.model_name,
        "cost": {
            "amount": str(rendered.cost_estimate.amount),
            "currency": rendered.cost_estimate.currency,
        },
        "provenance": {
            "renderer": rendered.renderer_used.value,
            "checksum": rendered.checksum,
            "generated_at": rendered.generated_at.isoformat(),
            "duration_s": rendered.duration_s,
        },
    }


def serialize_caps_view(view: CapsView) -> dict[str, Any]:
    """Respuesta de `set_account_caps`/`delete_account_caps`/
    `resolve_account_caps` (contracts/broker-set-account-caps.schema.json).
    `effective` ya viene recortado a `min(fichero, panel)` y al sobre
    vigente: es lo que se aplicara, no lo que se guardo."""
    resolution = view.resolution
    return {
        "platform_account_id": resolution.platform_account_id,
        "source": resolution.source.value,
        "writable": resolution.writable,
        "effective": _serialize_amounts(resolution.effective),
        "from_file": _serialize_cap_amounts(resolution.from_file),
        "from_panel": _serialize_cap_amounts(resolution.from_panel),
        "clamped_by": list(resolution.clamped_by),
        "panel_state_available": view.panel_state_available,
        "envelope": _serialize_envelope(view.envelope),
    }


def _serialize_cap_amounts(
    amounts: EffectiveAmounts | PanelAmounts | None,
) -> dict[str, int] | None:
    """Lo que declara CADA fuente por separado, sin recortar: el panel
    necesita poder decir «guardado X, en vigor Y» en un campo recortado. Los
    tres campos que el panel fija y solo esos -- el suelo no sale de aqui, y
    la divisa es una sola para toda la vista."""
    if amounts is None:
        return None
    return {
        "daily_cap_minor": amounts.daily_cap_minor,
        "monthly_cap_minor": amounts.monthly_cap_minor,
        "ceiling_minor": amounts.ceiling_minor,
    }


def _serialize_amounts(amounts: EffectiveAmounts | None) -> dict[str, int] | None:
    if amounts is None:
        return None
    return {
        "daily_cap_minor": amounts.daily_cap_minor,
        "monthly_cap_minor": amounts.monthly_cap_minor,
        "floor_minor": amounts.floor_minor,
        "ceiling_minor": amounts.ceiling_minor,
    }


def _serialize_envelope(usage: EnvelopeUsage | None) -> dict[str, Any] | None:
    if usage is None:
        return None
    envelope = usage.envelope
    return {
        "max_daily_cap_minor": envelope.max_daily_cap_minor,
        "max_monthly_cap_minor": envelope.max_monthly_cap_minor,
        "max_ceiling_minor": envelope.max_ceiling_minor,
        "min_floor_minor": envelope.min_floor_minor,
        "max_accounts": envelope.max_accounts,
        "accounts_used": usage.accounts_used,
        "max_cap_changes_per_day": envelope.max_cap_changes_per_day,
        "cap_changes_today": usage.cap_changes_today,
        "currency": envelope.currency,
    }
