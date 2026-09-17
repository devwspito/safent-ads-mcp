"""Repositorios SQL de `optimization` sobre `marginal_estimates`,
`response_curves` y `allocation_plans` (migracion 0017_optimization).

`marginal_estimates` y `response_curves` son cache materializada (UPSERT,
mismo patron que `economics.infrastructure.sql_repositories.
SqlLagCurveRepository`); `allocation_plans` es historico solo-anexable
(siempre INSERT)."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Final

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.economics.domain.money import Money
from safent_ads.optimization.application.dto import StoredResponseCurve
from safent_ads.optimization.domain.allocation import AllocationPlan
from safent_ads.optimization.domain.marginal import EstimationMethod, MarginalEstimate
from safent_ads.optimization.domain.response_curve import (
    CurveConfidence,
    HillCurve,
    ObservedSpendRange,
    PowerCurve,
)
from safent_ads.shared.ids import BusinessId, EntityRef

__all__ = [
    "SqlAllocationPlanRepository",
    "SqlMarginalEstimateRepository",
    "SqlResponseCurveRepository",
]

_UPSERT_MARGINAL_ESTIMATE: Final = """
    INSERT INTO marginal_estimates (
        business_id, entity_ref, value, ci_low, ci_high, method, sample_size, computed_at
    ) VALUES (
        :business_id, :entity_ref, :value, :ci_low, :ci_high, :method, :sample_size, :computed_at
    )
    ON CONFLICT ON CONSTRAINT marginal_estimates_unique DO UPDATE
        SET value       = EXCLUDED.value,
            ci_low      = EXCLUDED.ci_low,
            ci_high     = EXCLUDED.ci_high,
            method      = EXCLUDED.method,
            sample_size = EXCLUDED.sample_size,
            computed_at = EXCLUDED.computed_at
"""

_SELECT_MARGINAL_ESTIMATE: Final = """
    SELECT value, ci_low, ci_high, method, sample_size
      FROM marginal_estimates
     WHERE business_id = :business_id AND entity_ref = :entity_ref
"""


class SqlMarginalEstimateRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_latest(
        self, *, business_id: BusinessId, entity_ref: EntityRef
    ) -> MarginalEstimate | None:
        result = await self._session.execute(
            text(_SELECT_MARGINAL_ESTIMATE),
            {"business_id": business_id.value, "entity_ref": str(entity_ref)},
        )
        row = result.mappings().first()
        return None if row is None else _row_to_marginal_estimate(row)

    async def save(
        self,
        *,
        business_id: BusinessId,
        entity_ref: EntityRef,
        estimate: MarginalEstimate,
        computed_at: datetime,
    ) -> None:
        await self._session.execute(
            text(_UPSERT_MARGINAL_ESTIMATE),
            {
                "business_id": business_id.value,
                "entity_ref": str(entity_ref),
                "value": estimate.value,
                "ci_low": estimate.ci_low,
                "ci_high": estimate.ci_high,
                "method": estimate.method.value,
                "sample_size": estimate.sample_size,
                "computed_at": computed_at,
            },
        )
        await self._session.flush()


def _row_to_marginal_estimate(row: RowMapping) -> MarginalEstimate:
    return MarginalEstimate(
        value=float(row["value"]),
        ci_low=float(row["ci_low"]),
        ci_high=float(row["ci_high"]),
        method=EstimationMethod(row["method"]),
        sample_size=row["sample_size"],
    )


_UPSERT_RESPONSE_CURVE: Final = """
    INSERT INTO response_curves (
        business_id, channel_id, curve_type, params, observed_min_spend, observed_max_spend,
        residual_std, curve_confidence, computed_at
    ) VALUES (
        :business_id, :channel_id, :curve_type, CAST(:params AS jsonb), :observed_min_spend,
        :observed_max_spend, :residual_std, :curve_confidence, now()
    )
    ON CONFLICT ON CONSTRAINT response_curves_unique DO UPDATE
        SET curve_type         = EXCLUDED.curve_type,
            params              = EXCLUDED.params,
            observed_min_spend  = EXCLUDED.observed_min_spend,
            observed_max_spend  = EXCLUDED.observed_max_spend,
            residual_std        = EXCLUDED.residual_std,
            curve_confidence    = EXCLUDED.curve_confidence,
            computed_at         = EXCLUDED.computed_at
"""

_SELECT_RESPONSE_CURVE: Final = """
    SELECT curve_type, params, observed_min_spend, observed_max_spend, residual_std,
           curve_confidence
      FROM response_curves
     WHERE business_id = :business_id AND channel_id = :channel_id
"""


class SqlResponseCurveRepository:
    """`entity_ref` en el puerto se traduce a `channel_id` en la tabla: la
    misma clave, salvo para canales residuales (§3b) -- ver docstring de la
    migracion."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_latest(
        self, *, business_id: BusinessId, entity_ref: EntityRef
    ) -> StoredResponseCurve | None:
        result = await self._session.execute(
            text(_SELECT_RESPONSE_CURVE),
            {"business_id": business_id.value, "channel_id": str(entity_ref)},
        )
        row = result.mappings().first()
        return None if row is None else _row_to_response_curve(row)

    async def save(
        self, *, business_id: BusinessId, channel_id: str, record: StoredResponseCurve
    ) -> None:
        curve_type, params = _curve_to_params(record.curve)
        await self._session.execute(
            text(_UPSERT_RESPONSE_CURVE),
            {
                "business_id": business_id.value,
                "channel_id": channel_id,
                "curve_type": curve_type,
                "params": json.dumps(params),
                "observed_min_spend": record.observed_range.min_spend,
                "observed_max_spend": record.observed_range.max_spend,
                "residual_std": record.residual_std,
                "curve_confidence": record.curve_confidence.value,
            },
        )
        await self._session.flush()


def _curve_to_params(curve: HillCurve | PowerCurve) -> tuple[str, dict[str, float]]:
    if isinstance(curve, HillCurve):
        return "hill", {"e_max": curve.e_max, "k": curve.k}
    return "power", {"a": curve.a, "b": curve.b}


def _row_to_response_curve(row: RowMapping) -> StoredResponseCurve:
    params = row["params"]
    curve: HillCurve | PowerCurve
    if row["curve_type"] == "hill":
        curve = HillCurve(e_max=float(params["e_max"]), k=float(params["k"]))
    else:
        curve = PowerCurve(a=float(params["a"]), b=float(params["b"]))
    return StoredResponseCurve(
        curve=curve,
        observed_range=ObservedSpendRange(
            min_spend=float(row["observed_min_spend"]), max_spend=float(row["observed_max_spend"])
        ),
        residual_std=float(row["residual_std"]),
        curve_confidence=CurveConfidence(row["curve_confidence"]),
    )


_INSERT_ALLOCATION_PLAN: Final = """
    INSERT INTO allocation_plans (
        id, business_id, donor_entity_ref, donor_current_daily_spend_minor,
        donor_proposed_daily_spend_minor, donor_step_pct, receiver_entity_ref,
        receiver_current_daily_spend_minor, receiver_proposed_daily_spend_minor,
        receiver_step_pct, expected_contribution_delta_amount,
        expected_contribution_delta_currency, decrease_proposal_id, increase_proposal_id
    ) VALUES (
        :id, :business_id, :donor_entity_ref, :donor_current_minor, :donor_proposed_minor,
        :donor_step_pct, :receiver_entity_ref, :receiver_current_minor, :receiver_proposed_minor,
        :receiver_step_pct, :contribution_delta_amount, :contribution_delta_currency,
        :decrease_proposal_id, :increase_proposal_id
    )
"""


class SqlAllocationPlanRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save(
        self,
        *,
        plan: AllocationPlan,
        decrease_proposal_id: str | None,
        increase_proposal_id: str | None,
    ) -> None:
        await self._session.execute(
            text(_INSERT_ALLOCATION_PLAN),
            _plan_params(
                plan,
                decrease_proposal_id=decrease_proposal_id,
                increase_proposal_id=increase_proposal_id,
            ),
        )
        await self._session.flush()


def _plan_params(
    plan: AllocationPlan, *, decrease_proposal_id: str | None, increase_proposal_id: str | None
) -> dict[str, Any]:
    return {
        "id": plan.plan_id.value,
        "business_id": plan.business_id.value,
        "donor_entity_ref": str(plan.donor_step.entity_ref),
        "donor_current_minor": _to_minor(plan.donor_step.current_daily_spend),
        "donor_proposed_minor": _to_minor(plan.donor_step.proposed_daily_spend),
        "donor_step_pct": plan.donor_step.step_pct,
        "receiver_entity_ref": str(plan.receiver_step.entity_ref),
        "receiver_current_minor": _to_minor(plan.receiver_step.current_daily_spend),
        "receiver_proposed_minor": _to_minor(plan.receiver_step.proposed_daily_spend),
        "receiver_step_pct": plan.receiver_step.step_pct,
        "contribution_delta_amount": plan.expected_contribution_delta.amount,
        "contribution_delta_currency": plan.expected_contribution_delta.currency,
        "decrease_proposal_id": decrease_proposal_id,
        "increase_proposal_id": increase_proposal_id,
    }


def _to_minor(money: Money) -> int:
    return int(money.amount * 100)
