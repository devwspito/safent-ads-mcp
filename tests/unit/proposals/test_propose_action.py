"""`ProposeAction` (T081/T068): crea o actualiza (FR-20) una propuesta
`pendiente`, nunca toca una plataforma."""

from __future__ import annotations

from safent_ads.proposals.application.propose_action import (
    ProposeAction,
    ProposeActionCommand,
)
from safent_ads.proposals.domain.classification import (
    Classification,
    ClassificationPolicy,
    ProposalKind,
)
from safent_ads.proposals.domain.money import Money
from safent_ads.proposals.domain.priority import ExpiryPolicy, Urgency
from safent_ads.proposals.domain.proposal import ProposalState, ProposedDiff
from safent_ads.proposals.testing.fakes import FakeProposalRepository
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import BusinessId

from .conftest import NOW, budget_diff, cause, entity_ref, evidence

_POLICY = ClassificationPolicy(critical_impact_threshold=Money.of("100000"))


def _use_case(proposals: FakeProposalRepository) -> ProposeAction:
    return ProposeAction(
        proposals=proposals,
        classification_policy=_POLICY,
        expiry_policy=ExpiryPolicy(),
        clock=FixedClock(NOW),
    )


def _command(**overrides: object) -> ProposeActionCommand:
    defaults: dict[str, object] = {
        "business_id": BusinessId.new(),
        "diff": budget_diff(),
        "kind": ProposalKind.BUDGET_DECREASE,
        "cause": cause(rule_id=None),
        "cause_type": "cost_per_lead_high",
        "evidence": evidence(),
        "estimated_impact": Money.of("310"),
        "urgency": Urgency.RECOMMENDED,
    }
    defaults.update(overrides)
    return ProposeActionCommand(**defaults)  # type: ignore[arg-type]


class TestCreatesAPendingProposal:
    async def test_creates_pending_with_computed_classification_and_expiry(self) -> None:
        proposals = FakeProposalRepository()
        use_case = _use_case(proposals)

        result = await use_case.execute(_command())

        stored = await proposals.get(result.proposal_id)
        assert stored is not None
        assert stored.state is ProposalState.PENDING
        assert result.classification is Classification.ROUTINE
        assert result.expires_at == NOW + ExpiryPolicy().normal_ttl

    async def test_budget_increase_is_always_important(self) -> None:
        proposals = FakeProposalRepository()
        use_case = _use_case(proposals)

        result = await use_case.execute(
            _command(kind=ProposalKind.BUDGET_INCREASE, diff=budget_diff(before="70", after="100"))
        )

        assert result.classification is Classification.IMPORTANT

    async def test_critical_impact_escalates_regardless_of_kind(self) -> None:
        proposals = FakeProposalRepository()
        use_case = _use_case(proposals)

        result = await use_case.execute(_command(estimated_impact=Money.of("999999")))

        assert result.classification is Classification.CRITICAL


class TestEquivalentConsolidation:
    async def test_a_second_call_for_the_same_entity_and_parameter_updates_not_duplicates(
        self,
    ) -> None:
        proposals = FakeProposalRepository()
        use_case = _use_case(proposals)
        command = _command()
        first = await use_case.execute(command)

        second = await use_case.execute(
            _command(business_id=command.business_id, diff=budget_diff(before="100", after="65"))
        )

        assert second.proposal_id == first.proposal_id
        assert len(proposals.all()) == 1
        stored = await proposals.get(first.proposal_id)
        assert stored is not None
        assert stored.diff.after == Money.of("65")

    async def test_a_different_parameter_on_the_same_entity_is_a_separate_proposal(self) -> None:
        proposals = FakeProposalRepository()
        use_case = _use_case(proposals)
        first = await use_case.execute(_command())

        second_diff = ProposedDiff.build(
            entity_ref=entity_ref(), parameter="status", before="ACTIVE", after="PAUSED"
        )
        second = await use_case.execute(_command(diff=second_diff))

        assert second.proposal_id != first.proposal_id
        assert len(proposals.all()) == 2


class TestProposedBy:
    """004 tasks.md A8: `person:<user_id>` cuando la crea una llamada MCP
    con puesto, `None` cuando la crea el motor de reglas -- nunca cambia
    `diff_hash` ni participa en la clasificacion."""

    async def test_person_caller_is_stored_as_proposed_by(self) -> None:
        proposals = FakeProposalRepository()
        use_case = _use_case(proposals)

        result = await use_case.execute(_command(proposed_by="person:ana-uuid"))

        stored = await proposals.get(result.proposal_id)
        assert stored is not None
        assert stored.proposed_by == "person:ana-uuid"

    async def test_rule_engine_call_leaves_proposed_by_null(self) -> None:
        proposals = FakeProposalRepository()
        use_case = _use_case(proposals)

        result = await use_case.execute(_command())

        stored = await proposals.get(result.proposal_id)
        assert stored is not None
        assert stored.proposed_by is None

    async def test_proposed_by_never_changes_the_diff_hash(self) -> None:
        with_person = await _use_case(FakeProposalRepository()).execute(
            _command(proposed_by="person:ana-uuid")
        )
        without_person = await _use_case(FakeProposalRepository()).execute(_command())

        assert with_person.diff_hash == without_person.diff_hash
        assert with_person.classification == without_person.classification
