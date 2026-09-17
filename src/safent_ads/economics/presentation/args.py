"""Argumentos pydantic de las herramientas de `economics` (mismo criterio de
seguridad que `mcp.presentation.args.ToolArgs`: sin campos extra,
inmutables, `business_id` obligatorio -- duplicado localmente, no
importado, para que `economics` no dependa de `mcp` (plan.md §4))."""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from safent_ads.shared.ids import EntityRef, EntityRefFormatError

_UUID_PATTERN = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
_OPAQUE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"


def _validate_entity_ref(value: str) -> str:
    try:
        EntityRef.parse(value)
    except EntityRefFormatError as exc:
        raise ValueError(str(exc)) from exc
    return value


BusinessIdStr = Annotated[str, Field(pattern=_UUID_PATTERN)]
ProductIdStr = Annotated[str, Field(pattern=_UUID_PATTERN)]
PlatformAccountIdStr = Annotated[str, Field(pattern=_UUID_PATTERN)]
PlatformStr = Annotated[str, Field(pattern=r"^(google|meta)$")]
OpaqueIdStr = Annotated[str, Field(pattern=_OPAQUE_ID_PATTERN)]
EntityRefStr = Annotated[str, AfterValidator(_validate_entity_ref)]


class EconomicsToolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class GetUnitEconomicsArgs(EconomicsToolArgs):
    business_id: BusinessIdStr
    product_id: ProductIdStr


class GetTargetCpaArgs(EconomicsToolArgs):
    business_id: BusinessIdStr
    product_id: ProductIdStr


class GetAttributionLagCurveArgs(EconomicsToolArgs):
    business_id: BusinessIdStr
    product_id: ProductIdStr
    platform: PlatformStr


class GetCohortProjectionArgs(EconomicsToolArgs):
    business_id: BusinessIdStr
    product_id: ProductIdStr
    platform: PlatformStr
    observed: int = Field(ge=0)
    age_days: int = Field(ge=0)


class GetPlatformDivergenceArgs(EconomicsToolArgs):
    business_id: BusinessIdStr
    platform_account_id: PlatformAccountIdStr


class BuildTrackingTemplateArgs(EconomicsToolArgs):
    business_id: BusinessIdStr
    platform: PlatformStr
    campaign_ref: str = Field(min_length=1, max_length=128)
    offering_code: str | None = Field(default=None, max_length=64)
    calendar_event_code: str | None = Field(default=None, max_length=64)


class ValidateUtmConsistencyArgs(EconomicsToolArgs):
    business_id: BusinessIdStr
    account_ref: OpaqueIdStr


class GetCrmReconciliationArgs(EconomicsToolArgs):
    business_id: BusinessIdStr
    window_start: date
    window_end: date
    lag_days: int = Field(default=0, ge=0)


class CompareAttributionWindowsArgs(EconomicsToolArgs):
    business_id: BusinessIdStr
    entity_ref: EntityRefStr
    as_of: date


class GetConversionBridgeHealthArgs(EconomicsToolArgs):
    business_id: BusinessIdStr
    as_of: datetime
