"""`ExecutionAttempt` — estado y `idempotency_key` (data-model.md `executions`)."""

from __future__ import annotations

import pytest

from safent_ads.execution.domain.execution_attempt import (
    ExecutionAttempt,
    ExecutionAttemptInvariantError,
    ExecutionAttemptRecorded,
    ExecutionStatus,
    build_idempotency_key,
    build_package_step_idempotency_key,
)
from safent_ads.proposals.domain.identifiers import AuthorizationId, ProposalId
from safent_ads.shared.ids import BusinessId

from .conftest import NOW


def _claim(diff_hash: str = "abcdef0123456789") -> ExecutionAttempt:
    return ExecutionAttempt.claim(
        BusinessId.new(), ProposalId.new(), AuthorizationId.new(), diff_hash, NOW
    )


class TestIdempotencyKeyFormat:
    def test_key_uses_exec_prefix_proposal_and_first_12_of_hash(self) -> None:
        proposal_id = ProposalId.new()

        key = build_idempotency_key(proposal_id, "0123456789abcdef")

        assert key == f"exec-{proposal_id}-0123456789ab"

    def test_claim_uses_the_same_builder(self) -> None:
        proposal_id = ProposalId.new()
        attempt = ExecutionAttempt.claim(
            BusinessId.new(), proposal_id, AuthorizationId.new(), "0123456789abcdef", NOW
        )

        assert attempt.idempotency_key == build_idempotency_key(proposal_id, "0123456789abcdef")

    def test_retry_with_same_proposal_and_diff_hash_yields_same_key(self) -> None:
        """T063: `test_retry_no_duplicate_change` se apoya en que dos
        reclamos del mismo (proposal_id, diff_hash) produzcan la MISMA
        `idempotency_key` — el broker/repo la usa como UNIQUE."""
        proposal_id = ProposalId.new()
        first_attempt = ExecutionAttempt.claim(
            BusinessId.new(), proposal_id, AuthorizationId.new(), "0123456789abcdef", NOW
        )
        second_attempt = ExecutionAttempt.claim(
            BusinessId.new(), proposal_id, AuthorizationId.new(), "0123456789abcdef", NOW
        )

        assert first_attempt.idempotency_key == second_attempt.idempotency_key


class TestPackageStepIdempotencyKeyFormat:
    """`003-paquete-de-campana` data-model.md Revision 2 §R2.6 (BL-4):
    `pkg-<publication_id>-<step_index>`, deliberadamente sin reutilizar
    `build_idempotency_key` -- prefijo distinto, argumentos de otro tipo,
    y estable aunque `proposal_id`/`diff_hash` cambien entre reintentos,
    reanudaciones y re-acuñaciones de autorizacion."""

    def test_key_uses_pkg_prefix_publication_and_zero_padded_index(self) -> None:
        assert build_package_step_idempotency_key("01J8Z9K7Q3R5T6V8W0X2Y4Z6A8", 3) == (
            "pkg-01J8Z9K7Q3R5T6V8W0X2Y4Z6A8-03"
        )

    def test_key_is_never_confused_with_the_exec_prefix(self) -> None:
        assert not build_package_step_idempotency_key("01J8Z9K7Q3R5T6V8W0X2Y4Z6A8", 0).startswith(
            "exec-"
        )

    def test_claim_for_package_step_uses_the_same_builder_and_tags_the_publication(self) -> None:
        proposal_id = ProposalId.new()
        attempt = ExecutionAttempt.claim_for_package_step(
            business_id=BusinessId.new(),
            proposal_id=proposal_id,
            authorization_id=AuthorizationId.new(),
            publication_id="01J8Z9K7Q3R5T6V8W0X2Y4Z6A8",
            step_index=1,
        )

        assert attempt.idempotency_key == build_package_step_idempotency_key(
            "01J8Z9K7Q3R5T6V8W0X2Y4Z6A8", 1
        )
        assert attempt.package_publication_id == "01J8Z9K7Q3R5T6V8W0X2Y4Z6A8"

    def test_started_at_is_none_so_the_immediate_run_once_can_claim_this_row(self) -> None:
        """H4-1 (revision de codigo): `chokepoint_step_executor.py` guarda
        este intento y LUEGO llama a `chokepoint.run_once(proposal_id=...)`
        -- `claim_next` solo se lleva filas con `started_at IS NULL` o con
        el lease caducado. Con `started_at` ya puesto aqui, esa llamada
        inmediata nunca encontraba la fila que el mismo paso acababa de
        crear, y todo paso de escritura de un paquete fallaba en el primer
        intento."""
        attempt = ExecutionAttempt.claim_for_package_step(
            business_id=BusinessId.new(),
            proposal_id=ProposalId.new(),
            authorization_id=AuthorizationId.new(),
            publication_id="01J8Z9K7Q3R5T6V8W0X2Y4Z6A8",
            step_index=1,
        )

        assert attempt.started_at is None

    def test_resuming_with_a_freshly_reissued_authorization_yields_the_same_key(self) -> None:
        """El escenario exacto de BL-4: reanudar re-acuña la `Authorization`
        (nuevo `authorization_id`), pero la clave de idempotencia del paso
        no depende de ella -- nunca depende de `proposal_id` ni `diff_hash`."""
        proposal_id = ProposalId.new()
        first = ExecutionAttempt.claim_for_package_step(
            business_id=BusinessId.new(),
            proposal_id=proposal_id,
            authorization_id=AuthorizationId.new(),
            publication_id="01J8Z9K7Q3R5T6V8W0X2Y4Z6A8",
            step_index=2,
        )
        resumed = ExecutionAttempt.claim_for_package_step(
            business_id=BusinessId.new(),
            proposal_id=proposal_id,
            authorization_id=AuthorizationId.new(),
            publication_id="01J8Z9K7Q3R5T6V8W0X2Y4Z6A8",
            step_index=2,
        )

        assert first.idempotency_key == resumed.idempotency_key


class TestLifecycle:
    def test_claim_starts_in_claimed(self) -> None:
        attempt = _claim()

        assert attempt.status is ExecutionStatus.CLAIMED
        assert attempt.attempt_count == 1

    def test_start_running_then_succeed(self) -> None:
        attempt = _claim()
        attempt.start_running()

        attempt.succeed("70.00", "hash-after", NOW, NOW)

        assert attempt.status is ExecutionStatus.EXECUTED
        assert attempt.applied_value == "70.00"
        assert attempt.platform_state_hash_after == "hash-after"
        assert attempt.finished_at == NOW
        assert attempt.created_external_id is None

    def test_succeed_records_the_created_resource_when_the_write_confirms_one(self) -> None:
        """`WriteResult.created_external_id` (creaciones de paquete): se
        anota siempre que llega, no solo cuando el intento nace de un paso
        de paquete -- cualquier `CREATE_*` puede llevarlo."""
        attempt = _claim()
        attempt.start_running()

        attempt.succeed(
            {"schema_version": 1}, "hash-after", NOW, NOW, created_external_id="meta:campaign:123"
        )

        assert attempt.created_external_id == "meta:campaign:123"

    def test_fail_from_running(self) -> None:
        attempt = _claim()
        attempt.start_running()

        attempt.fail("broker_timeout", NOW)

        assert attempt.status is ExecutionStatus.FAILED
        assert attempt.error_code == "broker_timeout"

    def test_fail_from_claimed_default_deny(self) -> None:
        """Cualquier excepcion antes de `start_running` (p. ej. verificacion
        de autorizacion) tambien debe poder terminar en FAILED — nunca en
        exito (plan.md §6: "toda excepcion o timeout -> FALLIDA")."""
        attempt = _claim()

        attempt.fail("auth_invalid", NOW)

        assert attempt.status is ExecutionStatus.FAILED

    def test_skip_due_to_drift(self) -> None:
        attempt = _claim()

        attempt.skip_due_to_drift(NOW)

        assert attempt.status is ExecutionStatus.SKIPPED_DRIFT

    def test_block_by_guardrail_records_reasons(self) -> None:
        attempt = _claim()

        attempt.block_by_guardrail(("daily_cap_exceeded",), NOW)

        assert attempt.status is ExecutionStatus.BLOCKED_GUARDRAIL
        assert attempt.error_code == "daily_cap_exceeded"

    def test_block_by_brake(self) -> None:
        attempt = _claim()

        attempt.block_by_brake(NOW)

        assert attempt.status is ExecutionStatus.BLOCKED_BRAKE

    def test_succeed_requires_running(self) -> None:
        attempt = _claim()

        with pytest.raises(ExecutionAttemptInvariantError):
            attempt.succeed("70.00", "hash", NOW, NOW)

    @pytest.mark.parametrize(
        "terminal_status",
        [
            ExecutionStatus.EXECUTED,
            ExecutionStatus.FAILED,
            ExecutionStatus.SKIPPED_DRIFT,
            ExecutionStatus.BLOCKED_GUARDRAIL,
            ExecutionStatus.BLOCKED_BRAKE,
        ],
    )
    def test_terminal_states_reject_further_transitions(
        self, terminal_status: ExecutionStatus
    ) -> None:
        attempt = _claim()
        attempt.status = terminal_status

        with pytest.raises(ExecutionAttemptInvariantError):
            attempt.fail("late_error", NOW)

        assert attempt.is_terminal() is True


class TestAuditEvent:
    def test_audit_event_carries_outcome_and_business_id(self) -> None:
        attempt = _claim()
        attempt.block_by_brake(NOW)

        event = attempt.audit_event(NOW)

        assert isinstance(event, ExecutionAttemptRecorded)
        assert event.business_id == attempt.business_id
        assert event.outcome == "blocked_brake"
        assert event.execution_id == str(attempt.execution_id)
