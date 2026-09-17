"""`AuthorizeRuleAction` (T067): acuña `rule_authorization` solo si la
condicion de la regla esta viva Y el guardarraíl en vivo lo permite."""

from __future__ import annotations

import pytest

from safent_ads.execution.application.authorize_rule_action import (
    AuthorizeRuleAction,
    AuthorizeRuleActionCommand,
    RuleAuthorizationDenialReason,
    RuleAuthorizationDeniedError,
)
from safent_ads.execution.domain.guardrails import (
    BrakeMode,
    BrakeScope,
    BrakeScopeKind,
    EmergencyBrake,
    GuardrailEvaluator,
    GuardrailSet,
    LedgerSnapshot,
    brake_scope_from,
)
from safent_ads.execution.testing.fakes import (
    FakeBrakeStatePort,
    FakeGuardrailSetRepository,
    FakeRuleConditionPort,
    FakeSpendLedger,
)
from safent_ads.proposals.domain.authorization import AuthorizationKind
from safent_ads.proposals.domain.classification import Classification
from safent_ads.proposals.domain.proposal import Proposal, ProposalState
from safent_ads.proposals.testing.fakes import (
    FakeAuthorizationRepository,
    FakeProposalRepository,
    FakeSignerPort,
)
from safent_ads.shared.clock import FixedClock

from .conftest import NOW, empty_ledger, entity_scope, guardrail_set, make_pending_proposal

_SCOPE = entity_scope()


async def _build_use_case(
    proposal: Proposal,
    *,
    rule_is_live: bool = True,
    guardrails: GuardrailSet | None = None,
    ledger: LedgerSnapshot | None = None,
) -> tuple[AuthorizeRuleAction, FakeAuthorizationRepository, FakeBrakeStatePort]:
    proposals = FakeProposalRepository()
    await proposals.save(proposal)
    authorizations = FakeAuthorizationRepository()
    brakes = FakeBrakeStatePort()
    guardrail_sets = FakeGuardrailSetRepository({_SCOPE.ref: guardrails or guardrail_set()})
    spend_ledger = FakeSpendLedger({_SCOPE.ref: ledger or empty_ledger()})
    use_case = AuthorizeRuleAction(
        proposals=proposals,
        authorizations=authorizations,
        rule_condition=FakeRuleConditionPort(lambda _rule_id, _entity_ref: rule_is_live),
        brakes=brakes,
        guardrail_evaluator=GuardrailEvaluator(),
        guardrail_sets=guardrail_sets,
        spend_ledger=spend_ledger,
        signer=FakeSignerPort(),
        clock=FixedClock(NOW),
    )
    return use_case, authorizations, brakes


class TestRuleAuthorizationRequiresLiveCondition:
    async def test_rule_authorization_requires_live_condition(self) -> None:
        proposal = make_pending_proposal()
        use_case, _authorizations, _brakes = await _build_use_case(proposal, rule_is_live=False)

        with pytest.raises(RuleAuthorizationDeniedError) as excinfo:
            await use_case.execute(AuthorizeRuleActionCommand(proposal.proposal_id, "M05"))

        assert excinfo.value.reason is RuleAuthorizationDenialReason.RULE_NOT_APPLICABLE

    async def test_proposal_not_pending_is_also_not_applicable(self) -> None:
        proposal = make_pending_proposal()
        proposal.reject(NOW, reason="ya no aplica")
        use_case, _authorizations, _brakes = await _build_use_case(proposal)

        with pytest.raises(RuleAuthorizationDeniedError) as excinfo:
            await use_case.execute(AuthorizeRuleActionCommand(proposal.proposal_id, "M05"))

        assert excinfo.value.reason is RuleAuthorizationDenialReason.RULE_NOT_APPLICABLE


class TestBrakeEngagedDenies:
    async def test_denies_when_brake_engaged(self) -> None:
        proposal = make_pending_proposal()
        use_case, _authorizations, brakes = await _build_use_case(proposal)
        brake = EmergencyBrake(scope=brake_scope_from(_SCOPE), mode=BrakeMode.ALL)
        brake.engage("incidente", NOW)
        await brakes.save(brake)

        with pytest.raises(RuleAuthorizationDeniedError) as excinfo:
            await use_case.execute(AuthorizeRuleActionCommand(proposal.proposal_id, "M05"))

        assert excinfo.value.reason is RuleAuthorizationDenialReason.BRAKE_ENGAGED

    async def test_autonomous_brake_also_blocks_rule_authorization(self) -> None:
        proposal = make_pending_proposal()
        use_case, _authorizations, brakes = await _build_use_case(proposal)
        brake = EmergencyBrake(scope=brake_scope_from(_SCOPE), mode=BrakeMode.AUTONOMOUS)
        brake.engage("degradacion", NOW)
        await brakes.save(brake)

        with pytest.raises(RuleAuthorizationDeniedError) as excinfo:
            await use_case.execute(AuthorizeRuleActionCommand(proposal.proposal_id, "M05"))

        assert excinfo.value.reason is RuleAuthorizationDenialReason.BRAKE_ENGAGED

    async def test_denies_when_global_brake_engaged(self) -> None:
        """BUG corregido: solo se comprobaba el freno de la cuenta -- uno
        GLOBAL activo dejaba acuñar la `rule_authorization` igualmente."""
        proposal = make_pending_proposal()
        use_case, _authorizations, brakes = await _build_use_case(proposal)
        brake = EmergencyBrake(scope=BrakeScope(kind=BrakeScopeKind.GLOBAL), mode=BrakeMode.ALL)
        brake.engage("parada general", NOW)
        await brakes.save(brake)

        with pytest.raises(RuleAuthorizationDeniedError) as excinfo:
            await use_case.execute(AuthorizeRuleActionCommand(proposal.proposal_id, "M05"))

        assert excinfo.value.reason is RuleAuthorizationDenialReason.BRAKE_ENGAGED

    async def test_denies_when_business_brake_engaged(self) -> None:
        proposal = make_pending_proposal()
        use_case, _authorizations, brakes = await _build_use_case(proposal)
        brakes.link_account_to_business(_SCOPE.ref, "negocio-1")
        brake = EmergencyBrake(
            scope=BrakeScope(kind=BrakeScopeKind.BUSINESS, ref="negocio-1"), mode=BrakeMode.ALL
        )
        brake.engage("gasto disparado en el negocio", NOW)
        await brakes.save(brake)

        with pytest.raises(RuleAuthorizationDeniedError) as excinfo:
            await use_case.execute(AuthorizeRuleActionCommand(proposal.proposal_id, "M05"))

        assert excinfo.value.reason is RuleAuthorizationDenialReason.BRAKE_ENGAGED

    async def test_another_businesss_brake_does_not_deny(self) -> None:
        proposal = make_pending_proposal(classification=Classification.ROUTINE)
        use_case, authorizations, brakes = await _build_use_case(proposal)
        brakes.link_account_to_business(_SCOPE.ref, "negocio-1")
        brake = EmergencyBrake(
            scope=BrakeScope(kind=BrakeScopeKind.BUSINESS, ref="otro-negocio"), mode=BrakeMode.ALL
        )
        brake.engage("incidente de otro negocio", NOW)
        await brakes.save(brake)

        await use_case.execute(AuthorizeRuleActionCommand(proposal.proposal_id, "M05"))

        assert await authorizations.get_active_for_proposal(proposal.proposal_id) is not None


class TestGuardrailBlockedDenies:
    async def test_denies_when_guardrail_blocks(self) -> None:
        proposal = make_pending_proposal()
        maxed_out_ledger = empty_ledger(changes_today=2)
        guardrails = guardrail_set(max_changes_per_entity_day=2)
        use_case, authorizations, _brakes = await _build_use_case(
            proposal, guardrails=guardrails, ledger=maxed_out_ledger
        )

        with pytest.raises(RuleAuthorizationDeniedError) as excinfo:
            await use_case.execute(AuthorizeRuleActionCommand(proposal.proposal_id, "M05"))

        assert excinfo.value.reason is RuleAuthorizationDenialReason.GUARDRAIL_BLOCKED
        assert await authorizations.get_active_for_proposal(proposal.proposal_id) is None


class TestMintsAuthorizationOnSuccess:
    async def test_mints_rule_authorization_and_approves_proposal(self) -> None:
        proposal = make_pending_proposal(classification=Classification.ROUTINE)
        use_case, authorizations, _brakes = await _build_use_case(proposal)

        authorization = await use_case.execute(
            AuthorizeRuleActionCommand(proposal.proposal_id, "M05")
        )

        assert authorization.kind is AuthorizationKind.RULE_AUTHORIZATION
        assert authorization.diff_hash == proposal.diff.diff_hash
        assert authorization.issued_by == "M05"
        stored = await authorizations.get_active_for_proposal(proposal.proposal_id)
        assert stored is not None
        assert stored.authorization_id == authorization.authorization_id
        assert proposal.state is ProposalState.APPROVED
