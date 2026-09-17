"""Cierra el residual C-15/C-17 dejado abierto en la re-revision de
security review F2/F3 (2026-09-09): "el candado de `SingleWorkerLock` solo
impide que dos procesos `ads-worker` compitan entre si -- el chokepoint
tambien es alcanzable desde `ads-api`". Sin `SqlUnitOfWork.lock_account`, dos
transacciones-puerta que arrancan a la vez para la MISMA cuenta leen el
mismo `spend_ledger` vacio y las dos pasan un tope diario que solo admite UN
cambio mas -- la carrera de C-17 ("dos workers... ambos pasan el tope
diario"). Con el candado, se serializan por `platform_account_id`: la
segunda ve el apunte de la primera y la corta.

`_run_gate_transaction` reproduce `ExecutionChokepoint._pass_gates` (candado
+ lectura de guardarrailes/ledger + veredicto), con una diferencia
deliberada: aqui el apunte del cambio aplicado (`record_applied_change`) se
hace DENTRO del mismo bloque bloqueado, no despues de la escritura remota
como en produccion. Es la forma honesta de probar la garantia real del
candado -- serializar la transaccion-puerta -- sin depender de la
temporizacion de una escritura de red real, que produccion deja fuera del
alcance de este candado a proposito (ver el residual documentado en
`sql_unit_of_work.py`: el apunte de produccion vive en la transaccion
SIGUIENTE, despues de `execute_write`)."""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.execution.domain.execution_attempt import ExecutionAttempt, build_idempotency_key
from safent_ads.execution.domain.guardrails import (
    GuardrailChange,
    GuardrailEvaluator,
    GuardrailScope,
    ScopeKind,
)
from safent_ads.execution.domain.identifiers import ExecutionId
from safent_ads.execution.infrastructure.sql_execution_queue import SqlExecutionQueue
from safent_ads.execution.infrastructure.sql_guardrail_sets import SqlGuardrailSetRepository
from safent_ads.execution.infrastructure.sql_spend_ledger import SqlSpendLedger
from safent_ads.execution.infrastructure.sql_unit_of_work import SqlUnitOfWork
from safent_ads.proposals.domain.authorization import AuthorizationKind
from safent_ads.proposals.domain.identifiers import AuthorizationId, ProposalId
from safent_ads.proposals.domain.money import Money
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import BusinessId, EntityRef
from tests.contracts.execution.conftest import NOW, AuthorizedProposal, GuardrailLimits, digest
from tests.contracts.execution.conftest import seed_guardrails as _seed_guardrails
from tests.integration.execution.conftest import committed_scenario

pytestmark = pytest.mark.integration

_BEFORE = Money.of("100")
_AFTER = Money.of("150")
_DELTA = Money.of("50")


async def _seed_second_proposal(
    session: AsyncSession, *, entity_ref: EntityRef, business_id: BusinessId
) -> AuthorizedProposal:
    """Segunda propuesta sobre la MISMA entidad (y por tanto la misma
    cuenta) que `scenario.context` -- `seed_proposal`/`seed_authorized_
    proposal` siempre estrenan entidad, asi que aqui se repite su insercion
    a mano en vez de volver a sembrar negocio/cuenta/entidad."""
    proposal_id = uuid.uuid4()
    diff_hash = digest(f"diff-{proposal_id}")
    expected_state_hash = digest(f"state-{proposal_id}")
    # `ix_proposals_open_per_parameter`: una sola propuesta ABIERTA por
    # `(entity_ref, parameter)` (FR-20). `scenario.context` ya tiene una
    # `daily_budget` abierta sobre esta misma entidad -- esta segunda usa
    # otro parametro para no chocar; el veredicto de guardarrailes de este
    # test no lee la columna, solo el `before`/`after` que le pasamos.
    await session.execute(
        text(
            """
            INSERT INTO proposals (id, business_id, entity_ref, parameter, current_value,
                                   proposed_value, diff_hash, classification, cause_key, cause,
                                   evidence, estimated_impact_amount, estimated_impact_currency,
                                   urgency, state, execution_scheduled_at, expires_at)
            VALUES (:id, :business_id, :entity_ref, 'daily_budget_second_gate',
                    CAST(:current_value AS JSONB), CAST(:proposed_value AS JSONB), :diff_hash,
                    'routine', 'M05|budget_high', 'CPL sobre objetivo en 7D',
                    CAST(:evidence AS JSONB), 310, 'EUR', 'recommended', 'scheduled',
                    :scheduled_at, :expires_at)
            """
        ),
        {
            "id": proposal_id,
            "business_id": business_id.value,
            "entity_ref": str(entity_ref),
            "current_value": json.dumps({"type": "money", "amount": "100", "currency": "EUR"}),
            "proposed_value": json.dumps({"type": "money", "amount": "150", "currency": "EUR"}),
            "diff_hash": diff_hash,
            "evidence": json.dumps({"expected_state_hash": expected_state_hash}),
            "scheduled_at": NOW,
            "expires_at": NOW.replace(hour=23),
        },
    )
    context = AuthorizedProposal(
        business_id=business_id,
        proposal_id=ProposalId(proposal_id),
        authorization_id=AuthorizationId(uuid.uuid4()),
        entity_ref=entity_ref,
        diff_hash=diff_hash,
        expected_state_hash=expected_state_hash,
    )
    await _seed_approval(session, context)
    await session.flush()
    return context


async def _seed_approval(session: AsyncSession, context: AuthorizedProposal) -> None:
    await session.execute(
        text(
            """
            INSERT INTO approvals (id, proposal_id, kind, decision, diff_hash,
                                   guardrail_verdict_hash, issued_by, channel, signature,
                                   decided_at, expires_at)
            VALUES (:id, :proposal_id, 'human_approval', 'approved', :diff_hash, :verdict_hash,
                    'owner-de-contrato', 'panel', :signature, :decided_at, :expires_at)
            """
        ),
        {
            "id": uuid.UUID(str(context.authorization_id)),
            "proposal_id": uuid.UUID(str(context.proposal_id)),
            "diff_hash": context.diff_hash,
            "verdict_hash": digest(f"verdict-{context.proposal_id}"),
            "signature": digest(f"firma-{context.proposal_id}"),
            "decided_at": NOW,
            "expires_at": NOW.replace(hour=23),
        },
    )


async def _claim(session: AsyncSession, context: AuthorizedProposal) -> SqlExecutionQueue:
    queue = SqlExecutionQueue(session, FixedClock(NOW))
    await queue.save(
        ExecutionAttempt(
            execution_id=ExecutionId.new(),
            business_id=context.business_id,
            proposal_id=context.proposal_id,
            authorization_id=context.authorization_id,
            idempotency_key=build_idempotency_key(context.proposal_id, context.diff_hash),
            previous_value=_BEFORE,
            platform_state_hash_before=context.expected_state_hash,
            attempt_count=0,
        )
    )
    claimed = await queue.claim_next(proposal_id=context.proposal_id)
    assert claimed is not None
    return queue


async def _run_gate_transaction(
    session: AsyncSession, context: AuthorizedProposal, scope: GuardrailScope
) -> tuple[bool, tuple[str, ...]]:
    """Candado de cuenta + lectura de guardarrailes/ledger + veredicto +
    apunte (si se permite), todo en UNA transaccion -- lo que
    `ExecutionChokepoint._pass_gates` hace con estos mismos puertos."""
    queue = await _claim(session, context)
    uow = SqlUnitOfWork(session)
    async with uow:
        await uow.lock_account(context.entity_ref)
        guardrails = await SqlGuardrailSetRepository(session).get_effective(scope)
        ledger = SqlSpendLedger(session, FixedClock(NOW), queue)
        snapshot = await ledger.snapshot(scope, context.entity_ref)
        verdict = GuardrailEvaluator().evaluate(
            GuardrailChange(
                scope=scope,
                entity_ref=context.entity_ref,
                authorization_kind=AuthorizationKind.HUMAN_APPROVAL,
                before=_BEFORE,
                after=_AFTER,
            ),
            guardrails,
            snapshot,
        )
        if verdict.allowed:
            await ledger.record_applied_change(scope, context.entity_ref, _DELTA)
    return verdict.allowed, verdict.reasons


async def test_two_concurrent_gate_transactions_on_the_same_account_respect_the_daily_cap(
    isolated_database_url: str,
) -> None:
    """Sin `lock_account`, las dos evaluaciones arrancan a la vez, leen el
    ledger vacio las dos y las dos pasan (`daily_cap="50"` admite un unico
    `+50`, no dos). Con el candado, exactamente una se ejecuta y la otra
    queda bloqueada por `daily_cap_exceeded` -- cual de las dos gana la
    carrera por el candado no importa, por eso la asercion no fija el
    orden."""
    async with committed_scenario(isolated_database_url) as scenario:
        entity_ref = scenario.context.entity_ref
        scope = GuardrailScope(kind=ScopeKind.ENTITY, ref=str(entity_ref))

        async with scenario.session() as setup:
            await _seed_guardrails(
                setup,
                scope=scope,
                limits=GuardrailLimits(daily_cap="50", max_step_pct=1.0),
                level="business",
            )
            second_context = await _seed_second_proposal(
                setup, entity_ref=entity_ref, business_id=scenario.context.business_id
            )
            await setup.commit()

        async with scenario.session() as first, scenario.session() as second:
            first_result, second_result = await asyncio.gather(
                _run_gate_transaction(first, scenario.context, scope),
                _run_gate_transaction(second, second_context, scope),
            )

        outcomes = sorted([first_result[0], second_result[0]])
        assert outcomes == [False, True]
        blocked_result = first_result if not first_result[0] else second_result
        assert "daily_cap_exceeded" in blocked_result[1]
