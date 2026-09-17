"""`ExecutionChokepoint.run_once()` — el punto unico de escritura (T063/T064)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from safent_ads.execution.application.chokepoint import ExecutionChokepoint
from safent_ads.execution.application.platform_state_revalidator import PlatformStateRevalidator
from safent_ads.execution.domain.execution_attempt import ExecutionAttempt, ExecutionStatus
from safent_ads.execution.domain.guardrails import (
    BrakeMode,
    BrakeScope,
    BrakeScopeKind,
    EmergencyBrake,
    GuardrailEvaluator,
    GuardrailScope,
    GuardrailSet,
    LedgerSnapshot,
    ScopeKind,
    brake_scope_from,
)
from safent_ads.execution.testing.fakes import (
    FakeAdsPlatformWritePort,
    FakeBrakeStatePort,
    FakeDecisionRecorder,
    FakeExecutionQueuePort,
    FakeExecutionReservations,
    FakeGuardrailSetRepository,
    FakePlatformReaderPort,
    FakeSpendLedger,
    FakeUnitOfWork,
)
from safent_ads.proposals.domain.authorization import AuthorizationKind, AuthorizationVerifier
from safent_ads.proposals.domain.money import Money
from safent_ads.proposals.domain.proposal import Proposal, ProposalState, ProposedDiff
from safent_ads.proposals.testing.fakes import (
    FakeAuthorizationRepository,
    FakeProposalRepository,
    FakeVerifierPort,
)
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import EntityRef
from tests.unit.execution.test_campaign_creation_budget import creation_payload

from .conftest import (
    NOW,
    budget_diff,
    empty_ledger,
    entity_scope,
    evaluate_guardrails,
    guardrail_set,
    make_scheduled_proposal,
    sign_matching_authorization,
)


@dataclass
class Scenario:
    chokepoint: ExecutionChokepoint
    queue: FakeExecutionQueuePort
    uow: FakeUnitOfWork
    brakes: FakeBrakeStatePort
    proposals_repo: FakeProposalRepository
    authorizations_repo: FakeAuthorizationRepository
    platform_write: FakeAdsPlatformWritePort
    spend_ledger: FakeSpendLedger
    recorder: FakeDecisionRecorder
    proposal: Proposal


async def _build_scenario(
    *,
    diff: ProposedDiff | None = None,
    expected_state_hash: str | None = None,
    live_state_hash: str = "irrelevant",
    platform_write: FakeAdsPlatformWritePort | None = None,
    guardrails: GuardrailSet | None = None,
    live_guardrails: GuardrailSet | None = None,
    ledger: LedgerSnapshot | None = None,
    kind: AuthorizationKind = AuthorizationKind.HUMAN_APPROVAL,
    brakes: FakeBrakeStatePort | None = None,
    revalidator_hook: Callable[[], None] | None = None,
) -> tuple[Scenario, ExecutionAttempt]:
    """Construye una propuesta `SCHEDULED` con una `Authorization` cuyo
    `diff_hash`/`guardrail_verdict_hash` coinciden con lo que el chokepoint
    recalculara en vivo, un `ExecutionAttempt` ya reclamado, y todos los
    puertos cableados con dobles en memoria.

    `live_guardrails`, si se pasa, es lo que el chokepoint ve AL EJECUTAR —
    distinto de `guardrails` (lo que se uso para firmar la autorizacion) —
    para simular que el guardarraíl del ambito cambio entre medias.
    `brakes`, si se pasa, permite compartir el mismo freno entre varios
    escenarios/chokepoints (p. ej. dos ciclos sucesivos de un worker)."""
    proposal = make_scheduled_proposal(diff=diff, expected_state_hash=expected_state_hash)
    # El chokepoint calcula su propio ambito con `_scope_for` (ENTITY sobre
    # `proposal.diff.entity_ref`) — hay que usar EXACTAMENTE ese mismo
    # ambito al cablear los dobles o las claves nunca coinciden.
    scope = entity_scope(proposal.diff.entity_ref)
    resolved_guardrails = guardrails or guardrail_set(scope=scope)
    resolved_ledger = ledger or empty_ledger()
    verdict = evaluate_guardrails(proposal, scope, resolved_guardrails, resolved_ledger, kind)
    authorization = sign_matching_authorization(proposal, verdict, kind=kind)

    proposals_repo = FakeProposalRepository()
    authorizations_repo = FakeAuthorizationRepository()
    await proposals_repo.save(proposal)
    await authorizations_repo.save(authorization)

    queue = FakeExecutionQueuePort()
    attempt = ExecutionAttempt.claim(
        proposal.business_id,
        proposal.proposal_id,
        authorization.authorization_id,
        proposal.diff.diff_hash,
        NOW,
    )
    queue.enqueue(attempt)

    brakes = brakes or FakeBrakeStatePort()
    resolved_platform_write = platform_write or FakeAdsPlatformWritePort()
    spend_ledger = FakeSpendLedger({scope.ref: resolved_ledger})
    recorder = FakeDecisionRecorder()
    uow = FakeUnitOfWork()

    chokepoint = ExecutionChokepoint(
        reservations=FakeExecutionReservations(),
        queue=queue,
        uow=uow,
        brakes=brakes,
        proposals=proposals_repo,
        authorizations=authorizations_repo,
        auth_verifier=AuthorizationVerifier(FakeVerifierPort(), FixedClock(NOW)),
        guardrail_evaluator=GuardrailEvaluator(),
        guardrail_sets=FakeGuardrailSetRepository(
            {scope.ref: live_guardrails or resolved_guardrails}
        ),
        spend_ledger=spend_ledger,
        revalidator=PlatformStateRevalidator(
            FakePlatformReaderPort(
                state_hash_by_entity={str(proposal.diff.entity_ref): live_state_hash},
                on_fetch=revalidator_hook,
            )
        ),
        platform_write=resolved_platform_write,
        recorder=recorder,
        clock=FixedClock(NOW),
    )

    scenario = Scenario(
        chokepoint=chokepoint,
        queue=queue,
        uow=uow,
        brakes=brakes,
        proposals_repo=proposals_repo,
        authorizations_repo=authorizations_repo,
        platform_write=resolved_platform_write,
        spend_ledger=spend_ledger,
        recorder=recorder,
        proposal=proposal,
    )
    return scenario, attempt


def _brake_scope_matching(scope_ref: str) -> BrakeScope:
    return brake_scope_from(GuardrailScope(kind=ScopeKind.PLATFORM_ACCOUNT, ref=scope_ref))


class TestHappyPath:
    async def test_executes_and_confirms_state(self) -> None:
        scenario, _attempt = await _build_scenario()

        outcome = await scenario.chokepoint.run_once()

        assert outcome is ExecutionStatus.EXECUTED
        assert scenario.platform_write.call_count == 1
        saved_proposal = await scenario.proposals_repo.get(scenario.proposal.proposal_id)
        assert saved_proposal is not None
        assert saved_proposal.state is ProposalState.EXECUTED

    async def test_records_applied_change_in_ledger(self) -> None:
        scenario, _attempt = await _build_scenario()

        await scenario.chokepoint.run_once()

        assert len(scenario.spend_ledger.recorded_changes) == 1

    async def test_records_decision_on_success(self) -> None:
        scenario, _attempt = await _build_scenario()

        await scenario.chokepoint.run_once()

        assert len(scenario.recorder.recorded) >= 1

    async def test_no_work_returns_none(self) -> None:
        scenario, _attempt = await _build_scenario()
        await scenario.queue.claim_next()  # drena la unica entrada

        outcome = await scenario.chokepoint.run_once()

        assert outcome is None
        assert scenario.platform_write.call_count == 0


class TestDefaultDenyOnMissingReferences:
    async def test_proposal_not_found_fails(self) -> None:
        scenario, _attempt = await _build_scenario()
        scenario.proposals_repo.clear()

        outcome = await scenario.chokepoint.run_once()

        assert outcome is ExecutionStatus.FAILED
        assert scenario.platform_write.call_count == 0

    async def test_authorization_not_found_fails(self) -> None:
        scenario, _attempt = await _build_scenario()
        scenario.authorizations_repo.clear()

        outcome = await scenario.chokepoint.run_once()

        assert outcome is ExecutionStatus.FAILED
        assert scenario.platform_write.call_count == 0


class TestBrakeEngaged:
    async def test_brake_engaged_blocks_and_never_writes(self) -> None:
        scenario, _attempt = await _build_scenario()
        scope = entity_scope(scenario.proposal.diff.entity_ref)
        brake = EmergencyBrake(scope=_brake_scope_matching(scope.ref), mode=BrakeMode.ALL)
        brake.engage("incidente", NOW)
        await scenario.brakes.save(brake)

        outcome = await scenario.chokepoint.run_once()

        assert outcome is ExecutionStatus.BLOCKED_BRAKE
        assert scenario.platform_write.call_count == 0

    async def test_brake_engaged_between_gate_commit_and_write_blocks(self) -> None:
        """threat-model.md C-18 (F2/F3 security review, finding 1): el
        propietario pulsa el freno DESPUES de que `_pass_gates` ya hizo
        commit -- en la ventana de la revalidacion remota, antes de la
        escritura. `FakePlatformReaderPort.on_fetch` dispara el freno
        exactamente ahi, sin depender de temporizacion real. Antes del fix
        nada volvia a leer el freno entre ese commit y `execute_write`."""
        brakes = FakeBrakeStatePort()

        def _engage_brake_mid_flight() -> None:
            scope_ref = entity_scope(scenario.proposal.diff.entity_ref).ref
            brake = EmergencyBrake(scope=_brake_scope_matching(scope_ref), mode=BrakeMode.ALL)
            brake.engage("propietario pulsa el freno a media ejecucion", NOW)
            brakes.engage_now(brake)

        scenario, _attempt = await _build_scenario(
            expected_state_hash="hash-before",
            live_state_hash="hash-before",
            brakes=brakes,
            revalidator_hook=_engage_brake_mid_flight,
        )

        outcome = await scenario.chokepoint.run_once()

        assert outcome is ExecutionStatus.BLOCKED_BRAKE
        assert scenario.platform_write.call_count == 0
        saved_proposal = await scenario.proposals_repo.get(scenario.proposal.proposal_id)
        assert saved_proposal is not None
        assert saved_proposal.state is ProposalState.SCHEDULED

    async def test_autonomous_brake_does_not_block_human_approval(self) -> None:
        scenario, _attempt = await _build_scenario(kind=AuthorizationKind.HUMAN_APPROVAL)
        scope = entity_scope(scenario.proposal.diff.entity_ref)
        brake = EmergencyBrake(scope=_brake_scope_matching(scope.ref), mode=BrakeMode.AUTONOMOUS)
        brake.engage("degradacion", NOW)
        await scenario.brakes.save(brake)

        outcome = await scenario.chokepoint.run_once()

        assert outcome is ExecutionStatus.EXECUTED

    async def test_global_brake_engaged_blocks_and_never_writes(self) -> None:
        """BUG corregido: el chokepoint solo comprobaba el freno de la
        cuenta -- uno GLOBAL activo dejaba pasar la escritura entera."""
        scenario, _attempt = await _build_scenario()
        brake = EmergencyBrake(scope=BrakeScope(kind=BrakeScopeKind.GLOBAL), mode=BrakeMode.ALL)
        brake.engage("parada general", NOW)
        await scenario.brakes.save(brake)

        outcome = await scenario.chokepoint.run_once()

        assert outcome is ExecutionStatus.BLOCKED_BRAKE
        assert scenario.platform_write.call_count == 0

    async def test_business_brake_engaged_blocks_and_never_writes(self) -> None:
        """Mismo bug, ambito NEGOCIO: el freno del propietario para TODAS
        las cuentas de su negocio, no solo si se pulsa por cuenta."""
        scenario, _attempt = await _build_scenario()
        scope = entity_scope(scenario.proposal.diff.entity_ref)
        scenario.brakes.link_account_to_business(scope.ref or "", "negocio-1")
        brake = EmergencyBrake(
            scope=BrakeScope(kind=BrakeScopeKind.BUSINESS, ref="negocio-1"), mode=BrakeMode.ALL
        )
        brake.engage("gasto disparado en el negocio", NOW)
        await scenario.brakes.save(brake)

        outcome = await scenario.chokepoint.run_once()

        assert outcome is ExecutionStatus.BLOCKED_BRAKE
        assert scenario.platform_write.call_count == 0

    async def test_another_businesss_brake_does_not_block(self) -> None:
        scenario, _attempt = await _build_scenario()
        scope = entity_scope(scenario.proposal.diff.entity_ref)
        scenario.brakes.link_account_to_business(scope.ref or "", "negocio-1")
        brake = EmergencyBrake(
            scope=BrakeScope(kind=BrakeScopeKind.BUSINESS, ref="otro-negocio"), mode=BrakeMode.ALL
        )
        brake.engage("incidente de otro negocio", NOW)
        await scenario.brakes.save(brake)

        outcome = await scenario.chokepoint.run_once()

        assert outcome is ExecutionStatus.EXECUTED

    async def test_global_autonomous_brake_does_not_block_human_approval(self) -> None:
        scenario, _attempt = await _build_scenario(kind=AuthorizationKind.HUMAN_APPROVAL)
        brake = EmergencyBrake(
            scope=BrakeScope(kind=BrakeScopeKind.GLOBAL), mode=BrakeMode.AUTONOMOUS
        )
        brake.engage("degradacion", NOW)
        await scenario.brakes.save(brake)

        outcome = await scenario.chokepoint.run_once()

        assert outcome is ExecutionStatus.EXECUTED


class TestAccountLock:
    """Security review F2/F3, C-15/C-17: `lock_account` es lo primero que
    corre dentro de la transaccion-puerta, antes de leer freno o
    guardarrailes. `FakeUnitOfWork` no bloquea de verdad (un solo hilo de
    test nunca compite consigo mismo) -- lo que se prueba aqui es el ORDEN,
    no la exclusion mutua real; esa la prueban los tests de integracion
    contra Postgres (`tests/integration/execution/`)."""

    async def test_locks_the_account_before_reading_the_brake_or_guardrails(self) -> None:
        scenario, _attempt = await _build_scenario()

        await scenario.chokepoint.run_once()

        entity_ref = str(scenario.proposal.diff.entity_ref)
        lock_index = scenario.uow.calls.index(f"lock_account:{entity_ref}")
        begin_index = scenario.uow.calls.index("begin")
        commit_index = scenario.uow.calls.index("commit")
        assert begin_index < lock_index < commit_index

    async def test_locks_the_account_even_when_the_brake_blocks(self) -> None:
        scenario, _attempt = await _build_scenario()
        scope = entity_scope(scenario.proposal.diff.entity_ref)
        brake = EmergencyBrake(scope=_brake_scope_matching(scope.ref), mode=BrakeMode.ALL)
        brake.engage("incidente", NOW)
        await scenario.brakes.save(brake)

        outcome = await scenario.chokepoint.run_once()

        assert outcome is ExecutionStatus.BLOCKED_BRAKE
        entity_ref = str(scenario.proposal.diff.entity_ref)
        assert f"lock_account:{entity_ref}" in scenario.uow.calls


class TestGuardrailBlocked:
    async def test_guardrail_blocked_defers_without_invalidating_proposal(self) -> None:
        maxed_out_ledger = empty_ledger(changes_today=2)
        guardrails = guardrail_set(max_changes_per_entity_day=2)
        scenario, _attempt = await _build_scenario(guardrails=guardrails, ledger=maxed_out_ledger)

        outcome = await scenario.chokepoint.run_once()

        assert outcome is ExecutionStatus.BLOCKED_GUARDRAIL
        assert scenario.platform_write.call_count == 0
        saved_proposal = await scenario.proposals_repo.get(scenario.proposal.proposal_id)
        assert saved_proposal is not None
        assert saved_proposal.state is ProposalState.SCHEDULED

    async def test_guardrail_changed_since_authorization_invalidates_proposal(self) -> None:
        """Entre la autorizacion y la ejecucion, el guardarraíl del ambito se
        endurece (p. ej. el propietario lo edito) — el veredicto en vivo ya
        no coincide con el firmado (T058: `test_edited_proposal_invalidates_authorization`,
        aqui aplicado al lado del guardarraíl en vez del `diff_hash`)."""
        authorized_guardrails = guardrail_set(max_changes_per_entity_day=5)
        tighter_guardrails = guardrail_set(max_changes_per_entity_day=0)
        scenario, _attempt = await _build_scenario(
            guardrails=authorized_guardrails, live_guardrails=tighter_guardrails
        )

        outcome = await scenario.chokepoint.run_once()

        assert outcome is ExecutionStatus.BLOCKED_GUARDRAIL
        assert scenario.platform_write.call_count == 0
        saved_proposal = await scenario.proposals_repo.get(scenario.proposal.proposal_id)
        assert saved_proposal is not None
        assert saved_proposal.state is ProposalState.INVALIDATED


class TestPreviousValueBackfill:
    async def test_previous_value_is_backfilled_from_the_proposal(self) -> None:
        """BUG corregido: `LiveRuleStep`/`SubmitApproval`/`UndoExecution`
        construian el `ExecutionAttempt` sin `previous_value` (mismo patron
        en los tres) -- `SqlSpendLedger` lo necesita para no partir de 0 y
        violar el CHECK de `spend_ledger` contra Postgres real en cualquier
        bajada. `ExecutionChokepoint._process` lo fija con lo que ya carga
        de la propuesta -- sin segunda consulta -- antes de que nada lo
        use."""
        scenario, attempt = await _build_scenario()
        assert attempt.previous_value is None  # como lo deja `ExecutionAttempt.claim`

        outcome = await scenario.chokepoint.run_once()

        assert outcome is ExecutionStatus.EXECUTED
        assert attempt.previous_value == scenario.proposal.diff.before


class TestGuardrailClamp:
    """BUG corregido: `GuardrailEvaluator.evaluate` calculaba `clamped_after`
    pero nada en la capa de aplicacion lo aplicaba nunca -- se escribia
    `proposal.diff.after` tal cual, sin recortar."""

    async def test_clamped_change_writes_the_clamped_value_not_the_requested_one(self) -> None:
        diff = budget_diff(before="100", after="50")
        guardrails = guardrail_set(floor="80", ceiling="300", max_step_pct=1.0)
        scenario, attempt = await _build_scenario(diff=diff, guardrails=guardrails)

        outcome = await scenario.chokepoint.run_once()

        assert outcome is ExecutionStatus.EXECUTED
        assert scenario.platform_write.call_count == 1
        written_command, _authorization, _key = scenario.platform_write.calls[0]
        assert written_command.value == Money.of("80"), (
            "el presupuesto escrito nunca debe cruzar el suelo del guardarraíl"
        )
        assert attempt.applied_value == Money.of("80")
        # `proposal.diff` NUNCA se muta (INV-1): sigue siendo el pedido
        # original, sin recortar.
        saved_proposal = await scenario.proposals_repo.get(scenario.proposal.proposal_id)
        assert saved_proposal is not None
        assert saved_proposal.diff.after == Money.of("50")

    async def test_clamp_to_the_current_value_is_a_noop_blocked_by_guardrail(self) -> None:
        """El suelo ya es el valor actual (`before`): recortar deja el
        cambio en NO-OP -- nada que escribir. `executions.outcome` es un
        CHECK cerrado en Postgres sin hueco para un estado nuevo sin
        migracion (0009_executions.py); `BLOCKED_GUARDRAIL` es el desenlace
        existente que mejor encaja."""
        diff = budget_diff(before="80", after="50")
        guardrails = guardrail_set(floor="80", ceiling="300", max_step_pct=1.0)
        scenario, attempt = await _build_scenario(diff=diff, guardrails=guardrails)

        outcome = await scenario.chokepoint.run_once()

        assert outcome is ExecutionStatus.BLOCKED_GUARDRAIL
        assert scenario.platform_write.call_count == 0
        assert attempt.error_code is not None
        assert "guardrail_clamp_is_noop" in attempt.error_code
        saved_proposal = await scenario.proposals_repo.get(scenario.proposal.proposal_id)
        assert saved_proposal is not None
        assert saved_proposal.state is ProposalState.SCHEDULED


class TestStateDriftAbortsWrite:
    async def test_state_drift_aborts_write(self) -> None:
        scenario, _attempt = await _build_scenario(
            expected_state_hash="hash-before", live_state_hash="hash-DIFFERENT"
        )

        outcome = await scenario.chokepoint.run_once()

        assert outcome is ExecutionStatus.SKIPPED_DRIFT
        assert scenario.platform_write.call_count == 0
        saved_proposal = await scenario.proposals_repo.get(scenario.proposal.proposal_id)
        assert saved_proposal is not None
        assert saved_proposal.state is ProposalState.INVALIDATED

    async def test_no_drift_when_hashes_match(self) -> None:
        scenario, _attempt = await _build_scenario(
            expected_state_hash="hash-before", live_state_hash="hash-before"
        )

        outcome = await scenario.chokepoint.run_once()

        assert outcome is ExecutionStatus.EXECUTED


class TestPlatformWriteFailure:
    async def test_platform_write_failure_retains_unknown(self) -> None:
        failing_port = FakeAdsPlatformWritePort(fail_with=ConnectionError("broker unreachable"))
        scenario, _attempt = await _build_scenario(platform_write=failing_port)

        outcome = await scenario.chokepoint.run_once()

        assert outcome is ExecutionStatus.UNKNOWN
        saved_proposal = await scenario.proposals_repo.get(scenario.proposal.proposal_id)
        assert saved_proposal is not None
        assert saved_proposal.state is ProposalState.EXECUTING


class _ExplodingGuardrailSetRepository:
    """Simula un fallo de infraestructura (p. ej. la BD cae) DENTRO de la
    transaccion — no esta envuelto por ningun `try/except` propio del
    chokepoint, asi que debe propagar hasta el `except Exception` de
    `run_once` (plan.md §6: "Toda excepcion o timeout ⇒ FALLIDA")."""

    async def get_effective(self, _scope: GuardrailScope) -> GuardrailSet:
        raise RuntimeError("guardrail store unreachable")


class TestUnexpectedExceptionDefaultsDeny:
    async def test_unclassified_exception_never_produces_success(self) -> None:
        scenario, _attempt = await _build_scenario()
        scenario.chokepoint._guardrail_sets = _ExplodingGuardrailSetRepository()  # type: ignore[assignment]

        outcome = await scenario.chokepoint.run_once()

        assert outcome is ExecutionStatus.FAILED
        assert scenario.platform_write.call_count == 0


class TestRetryNoDuplicateChange:
    async def test_retry_no_duplicate_change(self) -> None:
        """Dos intentos que referencian la MISMA propuesta comparten
        `idempotency_key` (T063). El segundo `run_once` nunca vuelve a
        escribir en la plataforma: la propuesta ya esta `EXECUTED` y
        `Proposal.begin_execution` lo rechaza antes de llegar al broker."""
        scenario, first_attempt = await _build_scenario()
        proposal = scenario.proposal
        authorization = await scenario.authorizations_repo.get_active_for_proposal(
            proposal.proposal_id
        )
        assert authorization is not None
        second_attempt = ExecutionAttempt.claim(
            proposal.business_id,
            proposal.proposal_id,
            authorization.authorization_id,
            proposal.diff.diff_hash,
            NOW,
        )
        assert second_attempt.idempotency_key == first_attempt.idempotency_key
        scenario.queue.enqueue(second_attempt)

        first_outcome = await scenario.chokepoint.run_once()
        second_outcome = await scenario.chokepoint.run_once()

        assert first_outcome is ExecutionStatus.EXECUTED
        assert second_outcome is ExecutionStatus.FAILED
        assert scenario.platform_write.call_count == 1


def _display_creation_payload() -> dict[str, object]:
    payload = creation_payload()
    payload["creation_plan"]["native"] = {
        "advertising_channel_type": "DISPLAY",
        "bidding_strategy": {"kind": "MANUAL_CPC"},
        "contains_eu_political_advertising": "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING",
    }
    return payload


class TestChannelNotEnabled:
    """T035 security re-check (2026-09-15, CWE-284): the SAME write
    chokepoint services `SubmitApproval` (a human-approved single
    `Proposal`) and a package step (`ChokepointStepExecutor.run_once`,
    `packages.infrastructure.chokepoint_step_executor`) -- this is the last
    gate before `_execute` reaches `platform_write`, so it is the one place
    that closes both callers regardless of what an upstream layer checked
    or skipped."""

    async def test_a_display_creation_plan_fails_before_writing(self) -> None:
        ref = EntityRef.parse("google:account:123")
        diff = ProposedDiff.build(
            entity_ref=ref,
            parameter="new_campaign:test",
            before=None,
            after=_display_creation_payload(),
        )
        scenario, _attempt = await _build_scenario(diff=diff)

        outcome = await scenario.chokepoint.run_once()

        assert outcome is ExecutionStatus.FAILED
        assert scenario.platform_write.call_count == 0
        saved = await scenario.proposals_repo.get(scenario.proposal.proposal_id)
        assert saved is not None
        assert saved.state is not ProposalState.EXECUTED

    async def test_a_search_creation_plan_still_writes(self) -> None:
        ref = EntityRef.parse("google:account:123")
        diff = ProposedDiff.build(
            entity_ref=ref, parameter="new_campaign:test", before=None, after=creation_payload()
        )
        scenario, _attempt = await _build_scenario(diff=diff)

        outcome = await scenario.chokepoint.run_once()

        assert outcome is ExecutionStatus.EXECUTED
        assert scenario.platform_write.call_count == 1
