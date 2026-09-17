"""`SqlDecisionRecorder`: traduce eventos de `proposals`/`execution` a
`PendingDecision` y los anexa via `RecordDecision` (reemplaza al
`_NullDecisionRecorder` temporal de `composition/container.py`)."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from safent_ads.audit.domain.entry import ActorKind, DecisionKind
from safent_ads.audit.infrastructure.in_memory_repository import InMemoryDecisionLogRepository
from safent_ads.execution.domain.execution_attempt import ExecutionAttemptRecorded
from safent_ads.execution.domain.guardrails import EmergencyBrakeEngaged
from safent_ads.execution.infrastructure.decision_recorder import (
    SqlDecisionRecorder,
    UnmappedDecisionEventError,
)
from safent_ads.proposals.domain.proposal import ProposalApproved, ProposalRaised
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.events import DomainEvent
from safent_ads.shared.ids import BusinessId

from .conftest import NOW, entity_ref


async def _recorder() -> tuple[SqlDecisionRecorder, InMemoryDecisionLogRepository]:
    repository = InMemoryDecisionLogRepository(FixedClock(NOW))
    return SqlDecisionRecorder(repository), repository


class TestKnownEventTypes:
    async def test_proposal_raised_maps_to_proposal_kind_with_entity_ref(self) -> None:
        recorder, repository = await _recorder()
        business_id = BusinessId.new()
        event = ProposalRaised(
            business_id=business_id,
            occurred_at=NOW,
            proposal_id="11111111-1111-1111-1111-111111111111",
            entity_ref=str(entity_ref()),
            classification="routine",
        )

        await recorder.record(event)

        entry = (await repository.get_by_seq(business_id, 1)) or pytest.fail("no se anexo nada")
        assert entry.kind is DecisionKind.PROPOSAL
        assert entry.actor_kind is ActorKind.SYSTEM
        assert entry.entity_ref is not None
        assert str(entry.entity_ref) == str(entity_ref())
        assert entry.proposal_id is not None
        assert entry.payload["classification"] == "routine"
        assert "business_id" not in entry.payload
        assert "occurred_at" not in entry.payload

    async def test_proposal_approved_maps_to_approval_kind(self) -> None:
        recorder, repository = await _recorder()
        business_id = BusinessId.new()
        event = ProposalApproved(
            business_id=business_id,
            occurred_at=NOW,
            proposal_id="22222222-2222-2222-2222-222222222222",
            diff_hash="deadbeef",
        )

        await recorder.record(event)

        entry = (await repository.get_by_seq(business_id, 1)) or pytest.fail("no se anexo nada")
        assert entry.kind is DecisionKind.APPROVAL
        assert entry.payload["diff_hash"] == "deadbeef"

    @pytest.mark.parametrize(
        ("outcome", "error_code"), [("executed", None), ("unknown", "receipt_not_found")]
    )
    async def test_execution_attempt_recorded_maps_to_execution_kind(
        self, outcome: str, error_code: str | None
    ) -> None:
        recorder, repository = await _recorder()
        business_id = BusinessId.new()
        event = ExecutionAttemptRecorded(
            business_id=business_id,
            occurred_at=NOW,
            execution_id="33333333-3333-3333-3333-333333333333",
            proposal_id="44444444-4444-4444-4444-444444444444",
            outcome=outcome,
            error_code=error_code,
        )

        await recorder.record(event)

        entry = (await repository.get_by_seq(business_id, 1)) or pytest.fail("no se anexo nada")
        assert entry.kind is DecisionKind.EXECUTION
        assert entry.payload["outcome"] == outcome
        assert entry.payload["error_code"] == error_code

    async def test_emergency_brake_engaged_maps_to_brake_kind_with_actor(self) -> None:
        recorder, repository = await _recorder()
        business_id = BusinessId.new()
        event = EmergencyBrakeEngaged(
            business_id=business_id,
            occurred_at=NOW,
            scope_kind="global",
            scope_ref=None,
            mode="all",
            reason="incidente",
            engaged_by="owner:test",
        )

        await recorder.record(event)

        entry = (await repository.get_by_seq(business_id, 1)) or pytest.fail("no se anexo nada")
        assert entry.kind is DecisionKind.BRAKE
        assert entry.payload["engaged_by"] == "owner:test"


@dataclass(frozen=True, kw_only=True)
class _UnknownEvent(DomainEvent):
    pass


class TestUnmappedEvent:
    async def test_unknown_event_type_raises_instead_of_guessing(self) -> None:
        recorder, _repository = await _recorder()
        with pytest.raises(UnmappedDecisionEventError):
            await recorder.record(_UnknownEvent(business_id=BusinessId.new(), occurred_at=NOW))
