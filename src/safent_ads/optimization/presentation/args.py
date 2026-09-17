"""Argumentos pydantic de las herramientas de `optimization` (mismo
criterio de seguridad que `economics.presentation.args`: sin campos extra,
inmutables, `business_id` obligatorio -- duplicado localmente, no
importado, para que `optimization` no dependa de `mcp` (plan.md §4))."""

from __future__ import annotations

from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from safent_ads.shared.ids import EntityRef, EntityRefFormatError

_UUID_PATTERN = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"


def _validate_entity_ref(value: str) -> str:
    try:
        EntityRef.parse(value)
    except EntityRefFormatError as exc:
        raise ValueError(str(exc)) from exc
    return value


BusinessIdStr = Annotated[str, Field(pattern=_UUID_PATTERN)]
ProductIdStr = Annotated[str, Field(pattern=_UUID_PATTERN)]
EntityRefStr = Annotated[str, AfterValidator(_validate_entity_ref)]


class OptimizationToolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class GetMarginalRoasArgs(OptimizationToolArgs):
    business_id: BusinessIdStr
    entity_ref: EntityRefStr


class DiagnoseEntityArgs(OptimizationToolArgs):
    business_id: BusinessIdStr
    entity_ref: EntityRefStr


class SimulateSpendChangeArgs(OptimizationToolArgs):
    business_id: BusinessIdStr
    entity_ref: EntityRefStr
    product_id: ProductIdStr
    current_daily_spend_amount: str
    spend_multiplier: float = Field(gt=0)


class ProposeReallocationPlanArgs(OptimizationToolArgs):
    business_id: BusinessIdStr
