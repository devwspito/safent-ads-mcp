"""`CampaignBrief` (tasks.md T113/T114; spec.md US5; FR-35/FR-36): lo que
una `Proposal` de tipo `CREATE_CAMPAIGN` necesita para nacer PAUSED --
objetivo, presupuesto de prueba, duracion y criterio de exito/muerte.
Compartido por el ciclo deterministico (T113, `OpportunityCandidate`) y la
tool MCP `propose_campaign` (T114): ambos caminos terminan en la MISMA
`Proposal`, ninguno toca una plataforma."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass

from safent_ads.proposals.domain.campaign_creation import CampaignCreationError, creation_budget
from safent_ads.proposals.domain.campaign_creation import (
    google_channel_from_creation_plan as google_channel_from_creation_plan,  # noqa: PLC0414 - public compatibility re-export
)
from safent_ads.proposals.domain.money import Money
from safent_ads.shared.ids import EntityRef, PlatformCode

# `safent-bundle/triggers/weekly-opportunities.json`: "presupuesto de prueba
# (<= 10% del gasto diario, minimo 20 EUR/dia), duracion 7-14 dias".
MIN_TEST_BUDGET = Money.of("20.00", "EUR")
MIN_DURATION_DAYS = 7
MAX_DURATION_DAYS = 14
_MAX_TEXT_LENGTH = 280


class CampaignBriefInvariantError(ValueError):
    """El brief no cumple FR-35 (objetivo, presupuesto, duracion, criterio
    de exito/muerte) o las cotas del bundle (20-14 dias)."""


@dataclass(frozen=True, kw_only=True, slots=True)
class CampaignBrief:
    objective: str
    platform: PlatformCode
    offering_id: str
    daily_budget: Money
    duration_days: int
    success_criterion: str
    kill_criterion: str
    angle: str
    targeting_seed: str
    geo: str | None = None
    calendar_event_id: str | None = None
    creation_plan: dict[str, object] | None = None

    def __post_init__(self) -> None:
        _require_text(self.objective, field_name="objective")
        _require_text(self.success_criterion, field_name="success_criterion")
        _require_text(self.kill_criterion, field_name="kill_criterion")
        _require_text(self.angle, field_name="angle")
        _require_text(self.targeting_seed, field_name="targeting_seed")
        if self.daily_budget.currency != MIN_TEST_BUDGET.currency:
            raise CampaignBriefInvariantError(
                f"daily_budget debe estar en {MIN_TEST_BUDGET.currency} "
                f"(spec.md Assumption 5): {self.daily_budget.currency!r}"
            )
        # The test-budget floor and 7-14 day window bound the deterministic
        # cycle's own unsupervised proposals (campaign_brief_policy.py: "ampliar
        # el presupuesto es decision del propietario al aprobar, nunca de este
        # ciclo"). A brief that already carries an explicit native creation_plan
        # IS that owner decision -- a concrete, PAUSED, human/agent-negotiated
        # plan -- so the test-cycle bounds do not apply to it; creation_budget()
        # (via validate_creation_plan below) still forces daily_budget to match
        # creation_plan exactly.
        if self.creation_plan is None:
            if self.daily_budget.amount < MIN_TEST_BUDGET.amount:
                raise CampaignBriefInvariantError(
                    f"daily_budget por debajo del minimo de prueba "
                    f"({MIN_TEST_BUDGET.amount} {MIN_TEST_BUDGET.currency}): "
                    f"{self.daily_budget.amount}"
                )
            if not (MIN_DURATION_DAYS <= self.duration_days <= MAX_DURATION_DAYS):
                raise CampaignBriefInvariantError(
                    f"duration_days fuera de [{MIN_DURATION_DAYS}, {MAX_DURATION_DAYS}]: "
                    f"{self.duration_days}"
                )
        self.validate_creation_plan()
        if self.creation_plan is not None:
            object.__setattr__(self, "creation_plan", deepcopy(self.creation_plan))

    def validate_creation_plan(self, account_ref: EntityRef | None = None) -> None:
        if self.creation_plan is None:
            return
        creation_budget(
            {
                "creation_plan": self.creation_plan,
                "daily_budget_amount": str(self.daily_budget.amount),
                "daily_budget_currency": self.daily_budget.currency,
            },
            account_ref,
        )
        if self.creation_plan["platform"] != self.platform.value:
            raise CampaignCreationError("campaign_creation_scope_mismatch")


def _require_text(value: str, *, field_name: str) -> None:
    if not value.strip():
        raise CampaignBriefInvariantError(f"{field_name} no puede estar vacio (FR-35)")
    if len(value) > _MAX_TEXT_LENGTH:
        raise CampaignBriefInvariantError(
            f"{field_name} supera {_MAX_TEXT_LENGTH} caracteres: {len(value)}"
        )


# `google_channel_from_creation_plan` moved to `proposals.domain.
# campaign_creation` (T035 security re-check 2026-09-15): a single shared
# implementation for every channel gate, `proposals.application.
# submit_approval`/`execution.application.chokepoint` included, without a
# reverse dependency of `proposals` on `opportunities`. Re-exported above
# under the same name -- callers of this module (`mcp.presentation.
# campaign_draft_tools`, `opportunities.application.propose_campaign`)
# never changed.
