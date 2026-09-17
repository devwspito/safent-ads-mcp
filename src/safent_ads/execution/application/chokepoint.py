"""`ExecutionChokepoint` — el punto unico de escritura (plan.md §6; T063).

Ninguna otra ruta del sistema habla con una plataforma. `run_once()` procesa
como maximo un `ExecutionAttempt` reclamado, registrando su desenlace via
`DecisionRecorder`. UNKNOWN no es terminal: conserva reserva y solo permite
consultar un recibo previo en la siguiente recuperacion.

Default-deny: antes de reservar, un fallo queda FAILED. Despues de reservar,
la incertidumbre conserva proteccion; un error de transporte nunca prueba
que el proveedor no haya aplicado el cambio (ADS-01)."""

from __future__ import annotations

from safent_ads.execution.application.platform_state_revalidator import (
    PlatformStateRevalidator,
)
from safent_ads.execution.application.ports import (
    AdsPlatformWritePort,
    BrakeStatePort,
    ConfirmedWriteRejection,
    ExecutionQueuePort,
    GuardrailSetRepository,
    UnitOfWork,
    WriteCommand,
    WriteResult,
)
from safent_ads.execution.application.reservations import ExecutionReservations
from safent_ads.execution.domain.execution_attempt import ExecutionAttempt, ExecutionStatus
from safent_ads.execution.domain.guardrails import (
    GuardrailChange,
    GuardrailEvaluator,
    GuardrailScope,
    GuardrailVerdict,
    ScopeKind,
    SpendLedger,
    brake_scope_from,
    effective_diff,
    money_pair_from_diff,
)
from safent_ads.execution.domain.undo_policy import UndoGracePolicy
from safent_ads.proposals.application.ports import AuthorizationRepository, ProposalRepository
from safent_ads.proposals.domain.authorization import (
    Authorization,
    AuthorizationGuardrailMismatchError,
    AuthorizationVerificationError,
    AuthorizationVerifier,
)
from safent_ads.proposals.domain.campaign_creation import google_channel_from_creation_plan
from safent_ads.proposals.domain.diff_hash import to_jsonable
from safent_ads.proposals.domain.google_channel_spec import GoogleAdvertisingChannelType
from safent_ads.proposals.domain.identifiers import ProposalId
from safent_ads.proposals.domain.money import Money
from safent_ads.proposals.domain.proposal import Proposal, ProposalState, ProposedDiff
from safent_ads.shared.clock import Clock
from safent_ads.shared.events import DecisionRecorder


class ExecutionChokepoint:
    """Orquesta los 7 pasos de plan.md §6 contra puertos; sin logica de
    negocio propia — esa vive en `GuardrailEvaluator`, `AuthorizationVerifier`
    y `Proposal`/`ExecutionAttempt`."""

    def __init__(
        self,
        *,
        queue: ExecutionQueuePort,
        uow: UnitOfWork,
        brakes: BrakeStatePort,
        proposals: ProposalRepository,
        authorizations: AuthorizationRepository,
        auth_verifier: AuthorizationVerifier,
        guardrail_evaluator: GuardrailEvaluator,
        guardrail_sets: GuardrailSetRepository,
        spend_ledger: SpendLedger,
        reservations: ExecutionReservations,
        revalidator: PlatformStateRevalidator,
        platform_write: AdsPlatformWritePort,
        recorder: DecisionRecorder,
        clock: Clock,
        undo_policy: UndoGracePolicy | None = None,
        enabled_google_channels: frozenset[GoogleAdvertisingChannelType] = frozenset(
            {GoogleAdvertisingChannelType.SEARCH}
        ),
    ) -> None:
        self._queue = queue
        self._uow = uow
        self._brakes = brakes
        self._proposals = proposals
        self._authorizations = authorizations
        self._auth_verifier = auth_verifier
        self._guardrail_evaluator = guardrail_evaluator
        self._guardrail_sets = guardrail_sets
        self._spend_ledger = spend_ledger
        self._reservations = reservations
        self._revalidator = revalidator
        self._platform_write = platform_write
        self._recorder = recorder
        self._clock = clock
        self._undo_policy = undo_policy or UndoGracePolicy()
        self._enabled_google_channels = enabled_google_channels

    async def run_once(self, *, proposal_id: ProposalId | None = None) -> ExecutionStatus | None:
        """Sin `proposal_id`: procesa la fila reclamable mas antigua de
        cualquier negocio (un worker de cola normal). Con `proposal_id`:
        solo la fila de esa propuesta -- lo que necesita
        `ApplyDefensiveAction` (security review F2/F3, nit 3: antes
        reclamaba la mas antigua de CUALQUIERA, y reportaba el desenlace de
        otro negocio como si fuera el suyo)."""
        attempt = await self._queue.claim_next(proposal_id=proposal_id)
        if attempt is None:
            return None
        try:
            return await self._process(attempt)
        except Exception as exc:  # noqa: BLE001 - default-deny, nunca fail-open
            if await self._reservations.get(attempt) is not None:
                return await self._unknown(attempt, f"remote_outcome_unknown:{type(exc).__name__}")
            return await self._fail(attempt, f"unexpected_error:{type(exc).__name__}")

    async def _process(self, attempt: ExecutionAttempt) -> ExecutionStatus:  # noqa: PLR0911 - explicit fail-closed gates
        proposal = await self._proposals.get(attempt.proposal_id)
        if proposal is None:
            return await self._fail(attempt, "proposal_not_found")
        reserved = await self._reservations.get(attempt)
        if reserved is not None or attempt.status in {
            ExecutionStatus.RUNNING,
            ExecutionStatus.UNKNOWN,
        }:
            return await self._reconcile(attempt, proposal, reserved)
        if proposal.state is not ProposalState.SCHEDULED:
            return await self._fail(attempt, "proposal_not_scheduled")
        if self._channel_not_enabled(proposal):
            return await self._fail(attempt, "channel_type_not_enabled")
        # BUG corregido: `previous_value` nunca llegaba fijado desde
        # `LiveRuleStep`/`SubmitApproval`/`UndoExecution` (mismo patron en
        # los tres puntos de construccion) -- `SqlSpendLedger` lo necesita
        # para no partir de 0 y violar `spend_ledger_new_value_minor_check`
        # en cualquier bajada real. Se fija aqui, una unica vez, con lo que
        # ya se acaba de leer (la propuesta): sin segunda consulta, y es el
        # mismo "antes" que el paso 5 revalida contra el estado remoto antes
        # de que nada lo use.
        attempt.previous_value = proposal.diff.before
        authorization = await self._authorizations.get(attempt.authorization_id)
        if authorization is None:
            return await self._fail(attempt, "authorization_not_found")

        scope = _scope_for(proposal)
        gate_status, verdict = await self._pass_gates(attempt, proposal, authorization, scope)
        if gate_status is not None:
            return gate_status
        assert verdict is not None  # noqa: S101 - `_pass_gates` solo omite verdict junto a un status

        expected_hash = proposal.expected_state_hash
        if await self._revalidator.has_drifted(proposal.diff.entity_ref, expected_hash):
            return await self._skip_drift(attempt, proposal)

        # threat-model.md C-18: el freno leido en `_pass_gates` puede quedar
        # obsoleto para cuando llegamos aqui — esa transaccion ya hizo
        # commit y la revalidacion de arriba es una ida y vuelta de red; el
        # propietario pudo pulsar el freno en ese hueco. Se relee SIN cache
        # justo antes de tocar `attempt`/`proposal`, para que "freno
        # pulsado" pare aqui con el intento todavia `CLAIMED` y la propuesta
        # todavia `SCHEDULED` (reevaluable), en vez de dejarla en
        # `EXECUTING`, de donde `Proposal` solo sale hacia EXECUTED/FAILED.
        if await self._brake_blocks(scope, authorization):
            return await self._block_brake(attempt)

        return await self._execute(attempt, proposal, authorization, scope, verdict)

    # ------------------------------------------------------------------
    # Pasos 2-4 de plan.md §6: freno, veredicto de guardarrailes y
    # verificacion de la autorizacion, dentro de la misma transaccion
    # (threat-model.md C-15). `lock_account` es lo PRIMERO que corre en ese
    # bloque -- antes de leer freno o guardarrailes -- para que dos
    # transacciones-puerta concurrentes sobre la MISMA cuenta (un ciclo de
    # `ads-worker` y una llamada de `ads-api` a `apply_defensive_action`,
    # por ejemplo) se serialicen en vez de leer el mismo `spend_ledger` a la
    # vez (security review F2/F3, C-15/C-17). Cuentas distintas no se pisan:
    # el lock es por `platform_account_id`, no global.
    # ------------------------------------------------------------------

    async def _pass_gates(
        self,
        attempt: ExecutionAttempt,
        proposal: Proposal,
        authorization: Authorization,
        scope: GuardrailScope,
    ) -> tuple[ExecutionStatus | None, GuardrailVerdict | None]:
        """Devuelve `(None, verdict)` solo si las cuatro comprobaciones
        pasan -- cualquier otro caso devuelve `(status, None)`: el veredicto
        recien evaluado solo es util (para recortar lo que se escribe, T3)
        cuando de verdad se va a ejecutar."""
        async with self._uow:
            await self._uow.lock_account(proposal.diff.entity_ref)
            fresh = await self._proposals.get(proposal.proposal_id)
            if (
                fresh is None
                or fresh.state is not ProposalState.SCHEDULED
                or fresh.diff.diff_hash != proposal.diff.diff_hash
            ):
                return await self._fail(attempt, "proposal_changed_before_reservation"), None
            if await self._brake_blocks(scope, authorization):
                return await self._block_brake(attempt), None

            verdict = await self._evaluate_guardrails(scope, proposal, authorization)
            verification_outcome = await self._verify_authorization(
                attempt, proposal, authorization, verdict
            )
            if verification_outcome is not None:
                return verification_outcome, None
            if not verdict.allowed:
                return await self._block_guardrail(attempt, verdict), None
            effective = effective_diff(proposal.diff, verdict)
            await self._reservations.reserve(attempt, effective)
        return None, verdict

    def _channel_not_enabled(self, proposal: Proposal) -> bool:
        """T035 security re-check (CWE-284): the SAME write chokepoint
        applies whether the campaign came from a human-approved single
        `Proposal` (`SubmitApproval`) or from a package step
        (`ChokepointStepExecutor.run_once`, `packages.infrastructure.
        chokepoint_step_executor`) -- this is the last gate before
        `_execute` reaches the broker, so it is the one place that closes
        both callers at once regardless of what checked (or skipped) the
        channel upstream. Pure, no port I/O."""
        if not proposal.diff.parameter.startswith("new_campaign:"):
            return False
        after = proposal.diff.after
        creation_plan = after.get("creation_plan") if isinstance(after, dict) else None
        channel = google_channel_from_creation_plan(creation_plan)
        return channel is not None and channel not in self._enabled_google_channels

    async def _brake_blocks(self, scope: GuardrailScope, authorization: Authorization) -> bool:
        brake = await self._brakes.get_effective(brake_scope_from(scope))
        return brake is not None and brake.blocks(authorization.kind)

    async def _evaluate_guardrails(
        self, scope: GuardrailScope, proposal: Proposal, authorization: Authorization
    ) -> GuardrailVerdict:
        guardrails = await self._guardrail_sets.get_effective(scope)
        ledger = await self._spend_ledger.snapshot(scope, proposal.diff.entity_ref)
        before, after = money_pair_from_diff(proposal.diff)
        change = GuardrailChange(
            scope=scope,
            entity_ref=proposal.diff.entity_ref,
            authorization_kind=authorization.kind,
            before=before,
            after=after,
            is_creation=proposal.diff.parameter.startswith("new_campaign:"),
        )
        return self._guardrail_evaluator.evaluate(change, guardrails, ledger)

    async def _verify_authorization(
        self,
        attempt: ExecutionAttempt,
        proposal: Proposal,
        authorization: Authorization,
        verdict: GuardrailVerdict,
    ) -> ExecutionStatus | None:
        # `effective_diff`, no `proposal.diff.diff_hash` a secas: si el
        # guardarraíl recorto el cambio, `AuthorizeRuleAction`/
        # `SubmitApproval`/`UndoExecution` firmaron el diff RECORTADO (BUG
        # corregido, ver su docstring) -- comparar contra el diff crudo
        # denegaria por diff_hash_mismatch cualquier ejecucion recortada,
        # aunque nada hubiese cambiado desde que se autorizo. `effective_diff`
        # ya devuelve el diff SIN recortar cuando `not verdict.allowed` (no
        # hay diff real que recortar), dejando que sea la comprobacion de
        # `guardrail_verdict_hash` la que detecte "el guardarraíl cambio
        # desde que se autorizo", igual que antes de este fix.
        live_diff_hash = effective_diff(proposal.diff, verdict).diff_hash
        try:
            self._auth_verifier.verify(authorization, live_diff_hash, verdict.verdict_hash)
        except AuthorizationGuardrailMismatchError:
            return await self._invalidate_guardrail_changed(attempt, proposal)
        except AuthorizationVerificationError as exc:
            return await self._fail(attempt, f"authorization_invalid:{type(exc).__name__}")
        return None

    # ------------------------------------------------------------------
    # Paso 5: revalidacion de estado remoto.
    # ------------------------------------------------------------------

    async def _skip_drift(self, attempt: ExecutionAttempt, proposal: Proposal) -> ExecutionStatus:
        now = self._clock.now()
        attempt.skip_due_to_drift(now)
        proposal.invalidate("platform_state_drifted", now)
        await self._persist(attempt, proposal)
        return ExecutionStatus.SKIPPED_DRIFT

    # ------------------------------------------------------------------
    # Pasos 6-7: escritura en el broker y persistencia del resultado.
    # ------------------------------------------------------------------

    async def _execute(
        self,
        attempt: ExecutionAttempt,
        proposal: Proposal,
        authorization: Authorization,
        scope: GuardrailScope,
        verdict: GuardrailVerdict,
    ) -> ExecutionStatus:
        # BUG corregido: `GuardrailEvaluator.evaluate` ya calculaba
        # `clamped_after` (suelo/techo/salto maximo), pero nada lo aplicaba
        # nunca -- se escribia `proposal.diff.after` tal cual. `effective_diff`
        # es el diff EFECTIVO (recortado si toca): el mismo que `_verify_
        # authorization` acaba de validar contra la autorizacion firmada.
        effective = effective_diff(proposal.diff, verdict)
        before, _ = money_pair_from_diff(proposal.diff)
        if isinstance(proposal.diff.after, Money) and effective.after == before:
            # Semantica decidida (spec.md/threat-model C-15/C-17): un
            # recorte que deja el cambio en NO-OP (ya estamos en el limite)
            # no escribe nada -- no hay "nuevo valor" que enviar a la
            # plataforma ni al `spend_ledger`.
            return await self._skip_guardrail_noop(attempt, verdict)

        now = self._clock.now()
        attempt.start_running()
        proposal.begin_execution(now)
        # Persistir ANTES de hablar con la plataforma, no solo despues: el
        # trigger de `proposals` valida transiciones de UN salto
        # (SCHEDULED->EXECUTING->{EXECUTED,FAILED}), asi que un unico UPSERT
        # que saltase directo de SCHEDULED a FAILED (si la escritura remota
        # fallase) lo rechazaria. Ademas dejar la fila en EXECUTING antes de
        # la llamada de red es lo que hace que una recuperacion tras caida
        # sepa que este intento ya estaba en vuelo, no solo reclamado.
        async with self._uow:
            await self._persist(attempt, proposal)
        command = _build_write_command(effective)
        try:
            result = await self._platform_write.execute_write(
                command, authorization, attempt.idempotency_key
            )
        except ConfirmedWriteRejection as exc:
            return await self._fail_running(attempt, proposal, str(exc))
        except Exception:  # noqa: BLE001 - a lost response says nothing about remote effect
            return await self._unknown(attempt, "remote_outcome_unknown")
        if to_jsonable(result.applied_value) != to_jsonable(effective.after):
            return await self._unknown(attempt, "receipt_payload_mismatch")
        return await self._succeed(attempt, proposal, result, scope, effective)

    async def _unknown(self, attempt: ExecutionAttempt, code: str) -> ExecutionStatus:
        attempt.mark_unknown(code)
        await self._persist(attempt, None)
        return ExecutionStatus.UNKNOWN

    async def _reconcile(  # noqa: PLR0911 - distinct unknown/confirmed outcomes
        self, attempt: ExecutionAttempt, proposal: Proposal, effective: ProposedDiff | None
    ) -> ExecutionStatus:
        # Recovered CLAIMED/RUNNING reservations may already have reached the broker.
        # No gates, expiry checks, new signature, remote drift read or execute_write here.
        if effective is None:
            return await self._unknown(attempt, "reservation_missing")
        authorization = await self._authorizations.get(attempt.authorization_id)
        if authorization is None or authorization.diff_hash != effective.diff_hash:
            return await self._unknown(attempt, "receipt_payload_mismatch")
        if attempt.status is ExecutionStatus.CLAIMED:
            attempt.start_running()
            proposal.begin_execution(self._clock.now())
            await self._persist(attempt, proposal)
        try:
            result = await self._platform_write.read_receipt(
                _build_write_command(effective), authorization, attempt.idempotency_key
            )
        except ConfirmedWriteRejection as exc:
            return await self._fail_running(attempt, proposal, str(exc))
        except Exception:  # noqa: BLE001 - missing broker, mismatched receipt: keep protection
            return await self._unknown(attempt, "receipt_unavailable")
        if result is None:
            return await self._unknown(attempt, "receipt_not_found")
        if to_jsonable(result.applied_value) != to_jsonable(effective.after):
            return await self._unknown(attempt, "receipt_payload_mismatch")
        attempt.previous_value = effective.before
        return await self._succeed(attempt, proposal, result, _scope_for(proposal), effective)

    async def _skip_guardrail_noop(
        self, attempt: ExecutionAttempt, verdict: GuardrailVerdict
    ) -> ExecutionStatus:
        """`executions.outcome` es un CHECK cerrado en Postgres
        (0009_executions.py) sin hueco para un estado nuevo sin migracion:
        `BLOCKED_GUARDRAIL` es el desenlace existente que mejor encaja --
        nada se escribio, el guardarraíl es la razon, y la propuesta queda
        `SCHEDULED` (reevaluable), igual que el resto de bloqueos de este
        chokepoint. El motivo `guardrail_clamp_is_noop` distingue este caso
        (permitido pero recortado a "sin cambio") de un bloqueo real
        (`verdict.allowed=False`)."""
        attempt.block_by_guardrail(("guardrail_clamp_is_noop", *verdict.reasons), self._clock.now())
        await self._persist(attempt, None)
        return ExecutionStatus.BLOCKED_GUARDRAIL

    async def _succeed(
        self,
        attempt: ExecutionAttempt,
        proposal: Proposal,
        result: WriteResult,
        scope: GuardrailScope,
        effective: ProposedDiff,
    ) -> ExecutionStatus:
        now = self._clock.now()
        grace = self._undo_policy.grace_for(proposal.diff)
        undo_deadline = None if grace is None else now + grace
        attempt.succeed(
            result.applied_value,
            result.confirmed_state_hash,
            undo_deadline,
            now,
            created_external_id=result.created_external_id,
        )
        proposal.record_execution(success=True, now=now)
        # Same account lock as admission: READ COMMITTED must not observe the old
        # ledger followed by an already-settled reservation in separate SELECTs.
        async with self._uow:
            await self._uow.lock_account(effective.entity_ref)
            await self._record_applied_change(scope, proposal, effective)
            await self._reservations.resolve(attempt, applied=True)
            await self._persist(attempt, proposal)
        return ExecutionStatus.EXECUTED

    async def _record_applied_change(
        self, scope: GuardrailScope, proposal: Proposal, effective: ProposedDiff
    ) -> None:
        # El delta anotado en el ledger es el REALMENTE aplicado (recortado
        # si el guardarraíl recorto, T3) -- `proposal.diff` sigue siendo el
        # pedido original, nunca el ledger de lo que de verdad se escribio.
        before, _ = money_pair_from_diff(proposal.diff)
        _, after = money_pair_from_diff(effective)
        entity_ref = proposal.diff.entity_ref
        await self._spend_ledger.record_applied_change(scope, entity_ref, after - before)

    # ------------------------------------------------------------------
    # Salidas comunes: persisten, anexan eventos y registran la decision.
    # ------------------------------------------------------------------

    async def _block_brake(self, attempt: ExecutionAttempt) -> ExecutionStatus:
        attempt.block_by_brake(self._clock.now())
        await self._persist(attempt, None)
        return ExecutionStatus.BLOCKED_BRAKE

    async def _block_guardrail(
        self, attempt: ExecutionAttempt, verdict: GuardrailVerdict
    ) -> ExecutionStatus:
        attempt.block_by_guardrail(verdict.reasons, self._clock.now())
        await self._persist(attempt, None)
        return ExecutionStatus.BLOCKED_GUARDRAIL

    async def _invalidate_guardrail_changed(
        self, attempt: ExecutionAttempt, proposal: Proposal
    ) -> ExecutionStatus:
        now = self._clock.now()
        attempt.block_by_guardrail(("guardrail_verdict_changed_since_authorization",), now)
        proposal.invalidate("guardrail_verdict_changed_since_authorization", now)
        await self._persist(attempt, proposal)
        return ExecutionStatus.BLOCKED_GUARDRAIL

    async def _fail(self, attempt: ExecutionAttempt, error_code: str) -> ExecutionStatus:
        attempt.fail(error_code, self._clock.now())
        await self._persist(attempt, None)
        return ExecutionStatus.FAILED

    async def _fail_running(
        self, attempt: ExecutionAttempt, proposal: Proposal, error_code: str
    ) -> ExecutionStatus:
        now = self._clock.now()
        attempt.fail(error_code, now)
        proposal.record_execution(success=False, now=now, error=error_code)
        await self._persist(attempt, proposal)
        return ExecutionStatus.FAILED

    async def _persist(self, attempt: ExecutionAttempt, proposal: Proposal | None) -> None:
        if attempt.is_terminal() and attempt.status is not ExecutionStatus.EXECUTED:
            await self._reservations.resolve(attempt, applied=False)
        await self._queue.save(attempt)
        if proposal is not None:
            await self._proposals.save(proposal)
            for event in proposal.pull_events():
                await self._recorder.record(event)
        await self._recorder.record(attempt.audit_event(self._clock.now()))


def _scope_for(proposal: Proposal) -> GuardrailScope:
    return GuardrailScope(kind=ScopeKind.ENTITY, ref=str(proposal.diff.entity_ref))


def _build_write_command(effective: ProposedDiff) -> WriteCommand:
    # Se construye ENTERO desde `effective` (el `ProposedDiff` ya recortado
    # que `_verify_authorization` acaba de comprobar contra la autorizacion
    # firmada), nunca releyendo `proposal.diff` -- BUG corregido:
    # `BrokerPlatformWriter._operation` clasificaba RAISE/LOWER desde el
    # diff CRUDO de la propuesta, asi que un recorte que invertia la
    # direccion real (deriva externa que ya dejaba `before` fuera de
    # suelo/techo) llegaba al broker con la operacion equivocada para el
    # valor que de verdad se pedia escribir. `effective.before` coincide
    # siempre con `proposal.diff.before` (el guardarraíl solo recorta
    # `after`), pero viaja explicito para que la infraestructura nunca
    # tenga que recomputarlo.
    return WriteCommand(
        entity_ref=effective.entity_ref,
        parameter=effective.parameter,
        before=effective.before,
        value=effective.after,
        managed_binding=effective.managed_binding,
    )
