"""`AllocationPlan` — asignacion equimarginal de cartera (profitability-
engine.md §3): mover euro del menor `mContribution` al mayor hasta
igualarlos, con `min_viable_spend`, `LearningGate`, `GuardrailPolicy.clamp()`
y un paso `step_pct` acotado. **Siempre dos propuestas separadas**: bajada
AUTO, subida con aprobacion (FR-12) -- nunca una decision compuesta.

Reutiliza `signals.domain.gates.LearningGate` y `rules.domain.guardrail.
GuardrailPolicy` (optimization N4.5 depende de signals N3 y rules N4,
plan.md §4): la puerta de aprendizaje y el guardarrail no se reinventan."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from safent_ads.economics.domain.money import Money
from safent_ads.optimization.domain.errors import MinViableSpendViolationError
from safent_ads.optimization.domain.identifiers import AllocationPlanId
from safent_ads.optimization.domain.marginal import MarginalEstimate
from safent_ads.rules.domain.guardrail import GuardrailPolicy
from safent_ads.shared.ids import BusinessId, EntityRef
from safent_ads.signals.domain.gates import LearningGate, LearningStatus

MIN_STEP_PCT = 0.05
MAX_STEP_PCT = 0.20
DEFAULT_DAILY_MOVEMENT_CAP_PCT = 0.15
MIN_CANDIDATES_TO_COMPARE = 2
BUY_CADENCE_DAYS = 7
SELL_CADENCE_DAYS = 3


class AllocationDirection(StrEnum):
    DECREASE = "decrease"
    INCREASE = "increase"


class AllocationVetoReason(StrEnum):
    LEARNING = "learning"
    ALREADY_EQUIMARGINAL = "already_equimarginal"
    CONFIDENCE_INTERVALS_OVERLAP_WRONG_WAY = "confidence_intervals_overlap_wrong_way"


@dataclass(frozen=True, kw_only=True, slots=True)
class AllocationCandidate:
    entity_ref: EntityRef
    current_daily_spend: Money
    min_viable_daily_spend: Money
    marginal_estimate: MarginalEstimate
    learning_status: LearningStatus

    @property
    def is_eligible(self) -> bool:
        return LearningGate.evaluate(self.learning_status).passed


@dataclass(frozen=True, kw_only=True, slots=True)
class AllocationStep:
    entity_ref: EntityRef
    direction: AllocationDirection
    current_daily_spend: Money
    proposed_daily_spend: Money
    step_pct: float
    cadence_days: int

    @property
    def requires_approval(self) -> bool:
        """Bajar es reversible y de bajo riesgo -> AUTO; subir gasto siempre
        exige aprobacion humana (FR-12)."""
        return self.direction is AllocationDirection.INCREASE

    @property
    def delta(self) -> Money:
        return self.proposed_daily_spend - self.current_daily_spend


@dataclass(frozen=True, kw_only=True, slots=True)
class AllocationPlan:
    plan_id: AllocationPlanId
    business_id: BusinessId
    donor_step: AllocationStep
    receiver_step: AllocationStep
    expected_contribution_delta: Money
    created_at: datetime


@dataclass(frozen=True, kw_only=True, slots=True)
class AllocationVerdict:
    plan: AllocationPlan | None
    veto_reason: AllocationVetoReason | None

    @classmethod
    def accepted(cls, plan: AllocationPlan) -> AllocationVerdict:
        return cls(plan=plan, veto_reason=None)

    @classmethod
    def vetoed(cls, reason: AllocationVetoReason) -> AllocationVerdict:
        return cls(plan=None, veto_reason=reason)


def _step_pct(value: float, reference: float) -> float:
    if reference == 0:
        return MAX_STEP_PCT
    raw = 0.5 * abs(value / reference - 1)
    return min(max(raw, MIN_STEP_PCT), MAX_STEP_PCT)


def _to_minor(money: Money) -> int:
    return int(money.amount * 100)


def _from_minor(minor: int, currency: str) -> Money:
    return Money.of(minor, currency).scaled_by(Decimal("0.01"))


def build_equimarginal_plan(
    *,
    plan_id: AllocationPlanId,
    business_id: BusinessId,
    donor: AllocationCandidate,
    receiver: AllocationCandidate,
    now: datetime,
    donor_guardrail: GuardrailPolicy | None = None,
    receiver_guardrail: GuardrailPolicy | None = None,
) -> AllocationVerdict:
    if not (donor.is_eligible and receiver.is_eligible):
        return AllocationVerdict.vetoed(AllocationVetoReason.LEARNING)
    donor_value = donor.marginal_estimate.value
    receiver_value = receiver.marginal_estimate.value
    if donor_value >= receiver_value:
        return AllocationVerdict.vetoed(AllocationVetoReason.ALREADY_EQUIMARGINAL)
    if donor.marginal_estimate.ci_low >= receiver.marginal_estimate.ci_high:
        return AllocationVerdict.vetoed(AllocationVetoReason.CONFIDENCE_INTERVALS_OVERLAP_WRONG_WAY)

    donor_step = _build_donor_step(donor, reference_value=receiver_value)
    receiver_step = _build_receiver_step(receiver, reference_value=donor_value)
    if receiver_guardrail is not None:
        receiver_step = _reclamp_step(receiver_step, receiver_guardrail)
    if donor_guardrail is not None:
        donor_step = _reclamp_step(donor_step, donor_guardrail)

    contribution_delta = Money.of(
        float(donor_step.delta.amount) * donor_value
        + float(receiver_step.delta.amount) * receiver_value,
        donor.current_daily_spend.currency,
    )
    plan = AllocationPlan(
        plan_id=plan_id,
        business_id=business_id,
        donor_step=donor_step,
        receiver_step=receiver_step,
        expected_contribution_delta=contribution_delta,
        created_at=now,
    )
    return AllocationVerdict.accepted(plan)


def _build_donor_step(donor: AllocationCandidate, *, reference_value: float) -> AllocationStep:
    step_pct = _step_pct(donor.marginal_estimate.value, reference_value)
    proposed = donor.current_daily_spend.scaled_by(1 - step_pct)
    if proposed.amount < donor.min_viable_daily_spend.amount:
        proposed = donor.min_viable_daily_spend
    return AllocationStep(
        entity_ref=donor.entity_ref,
        direction=AllocationDirection.DECREASE,
        current_daily_spend=donor.current_daily_spend,
        proposed_daily_spend=proposed,
        step_pct=step_pct,
        cadence_days=SELL_CADENCE_DAYS,
    )


def _build_receiver_step(
    receiver: AllocationCandidate, *, reference_value: float
) -> AllocationStep:
    step_pct = _step_pct(receiver.marginal_estimate.value, reference_value)
    proposed = receiver.current_daily_spend.scaled_by(1 + step_pct)
    return AllocationStep(
        entity_ref=receiver.entity_ref,
        direction=AllocationDirection.INCREASE,
        current_daily_spend=receiver.current_daily_spend,
        proposed_daily_spend=proposed,
        step_pct=step_pct,
        cadence_days=BUY_CADENCE_DAYS,
    )


def _reclamp_step(step: AllocationStep, guardrail: GuardrailPolicy) -> AllocationStep:
    verdict = guardrail.clamp(
        current_minor=_to_minor(step.current_daily_spend),
        proposed_minor=_to_minor(step.proposed_daily_spend),
        changes_today=0,
        spent_month_minor=0,
    )
    clamped_amount = _from_minor(verdict.allowed_value_minor, step.current_daily_spend.currency)
    if (
        step.direction is AllocationDirection.DECREASE
        and clamped_amount.amount < step.proposed_daily_spend.amount
    ):
        # el guardarrail nunca puede exigir bajar MAS de lo que ya proponiamos;
        # si lo hiciera, el propio guardarrail definiria un suelo inconsistente.
        raise MinViableSpendViolationError(
            f"guardarrail clamp por debajo del suelo minimo viable: {clamped_amount.amount}"
        )
    return AllocationStep(
        entity_ref=step.entity_ref,
        direction=step.direction,
        current_daily_spend=step.current_daily_spend,
        proposed_daily_spend=clamped_amount,
        step_pct=step.step_pct,
        cadence_days=step.cadence_days,
    )


def enforce_daily_movement_cap(
    steps: tuple[AllocationStep, ...],
    *,
    portfolio_daily_spend: Money,
    cap_pct: float = DEFAULT_DAILY_MOVEMENT_CAP_PCT,
) -> tuple[AllocationStep, ...]:
    """Guardarrail de cartera (§3: '<= 15% del gasto diario por ciclo'):
    si el movimiento total absoluto excede el tope, escala todos los pasos
    proporcionalmente -- nunca deja pasar mas de lo permitido."""
    cap = portfolio_daily_spend.amount * Decimal(str(cap_pct))
    total_movement = sum((abs(step.delta.amount) for step in steps), Decimal("0"))
    if total_movement <= cap or total_movement == 0:
        return steps
    scale = float(cap / total_movement)
    return tuple(_scale_step(step, scale) for step in steps)


def _scale_step(step: AllocationStep, scale: float) -> AllocationStep:
    scaled_delta = step.delta.scaled_by(scale)
    proposed = step.current_daily_spend + scaled_delta
    return AllocationStep(
        entity_ref=step.entity_ref,
        direction=step.direction,
        current_daily_spend=step.current_daily_spend,
        proposed_daily_spend=proposed,
        step_pct=step.step_pct * scale,
        cadence_days=step.cadence_days,
    )


def select_donor_and_receiver(
    candidates: tuple[AllocationCandidate, ...],
) -> tuple[AllocationCandidate, AllocationCandidate] | None:
    """El menor y el mayor `mContribution` entre los candidatos elegibles
    (§3: 'mover euro del menor mContribution al mayor'). `None` si hay
    menos de dos elegibles -- no hay donante/receptor que comparar."""
    eligible = [c for c in candidates if c.is_eligible]
    if len(eligible) < MIN_CANDIDATES_TO_COMPARE:
        return None
    donor = min(eligible, key=lambda c: c.marginal_estimate.value)
    receiver = max(eligible, key=lambda c: c.marginal_estimate.value)
    if donor.entity_ref == receiver.entity_ref:
        return None
    return donor, receiver
