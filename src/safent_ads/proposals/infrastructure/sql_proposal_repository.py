"""Adaptador SQL de `ProposalRepository` sobre `proposals` (0008_proposals).

SQL crudo via `text()`, como `audit/infrastructure/sql_repository.py`: la
tabla tiene un trigger que valida la maquina de estados y otro que obliga a
rotar el `diff_hash` al cambiar `proposed_value`. Un modelo declarativo que
asuma que el cliente manda sobre esas columnas mentiria sobre quien decide.

Vive dentro de la transaccion del `AsyncSession` que le pasan; no hace
`commit()` -- el limite lo pone `SqlUnitOfWork` (execution/infrastructure),
porque el reclamo de la cola y la evaluacion del guardarraíl tienen que
caber en la MISMA transaccion (threat-model.md C-15).

Traducciones de frontera (el dominio nunca ve esto):
- `Cause.rule_id`/`CauseKey.rule_id` son *codigos* de regla (`M05`), no
  UUID: se resuelven a `rules.id` por codigo y ambito mas especifico, y el
  codigo original viaja en el sobre `evidence` para volver intacto.
- `evidence` guarda un sobre con las evidencias, los identificadores
  opacos de la causa y el `expected_state_hash`, que no tienen columna."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.observability.metrics import record_proposal_saved
from safent_ads.proposals.domain.cause import Cause, CauseKey, Evidence
from safent_ads.proposals.domain.classification import Classification
from safent_ads.proposals.domain.diff_hash import compute_diff_hash
from safent_ads.proposals.domain.identifiers import ProposalId
from safent_ads.proposals.domain.money import Money
from safent_ads.proposals.domain.priority import Priority, Urgency
from safent_ads.proposals.domain.proposal import (
    PostponedReason,
    Proposal,
    ProposalState,
    ProposedDiff,
)
from safent_ads.proposals.infrastructure.value_codec import decode_value, encode_value
from safent_ads.shared.ids import BusinessId, EntityRef
from safent_ads.shared.managed_ads import binding_from_json, binding_to_json
from safent_ads.shared.physical_ads_sql import (
    PHYSICAL_ACCOUNT_LOCK_SQL,
    PHYSICAL_ENTITY_REFS_SQL,
)

# Estados vivos de `ix_proposals_open_per_parameter`: mientras la propuesta
# este en uno de ellos, una equivalente la ACTUALIZA en vez de crear otra
# (FR-20). La lista se repite aqui a proposito: si la migracion la cambiara,
# el test de contrato lo detecta, no un import silencioso.
LIVE_STATES: tuple[str, ...] = ("pending", "postponed", "approved", "scheduled")

_RESOLVED_STATES: frozenset[ProposalState] = frozenset(
    {
        ProposalState.REJECTED,
        ProposalState.EXPIRED,
        ProposalState.INVALIDATED,
        ProposalState.EXECUTED,
        ProposalState.FAILED,
    }
)

# `rules.code` es unico por `scope_key`, no globalmente: gana la regla mas
# especifica que alcance a esta entidad, igual que compone `GuardrailSet`.
_RULE_BY_CODE = """
    (SELECT r.id FROM rules r
      WHERE r.code = :rule_code
        AND (r.scope = 'global'
             OR (r.scope = 'business' AND r.business_id = :business_id)
             OR (r.scope = 'campaign' AND r.campaign_entity_ref = :entity_ref)
             OR (r.scope = 'platform_account' AND r.platform_account_id =
                   (SELECT e.platform_account_id FROM ad_entities e
                     WHERE e.entity_ref = :entity_ref)))
      ORDER BY CASE r.scope
                 WHEN 'campaign' THEN 0 WHEN 'platform_account' THEN 1
                 WHEN 'business' THEN 2 ELSE 3 END
      LIMIT 1)
"""

_COLUMNS = """
    id, business_id, entity_ref, parameter, current_value::text AS current_value_text,
    proposed_value::text AS proposed_value_text, diff_hash, classification, cause_key,
    cause, evidence::text AS evidence_text, estimated_impact_amount,
    estimated_impact_currency, expected_contribution_delta_amount,
    expected_contribution_delta_currency, urgency, calendar_event_id, state, postponed_until,
    postponed_reason, owner_context, expires_at, execution_scheduled_at, created_at,
    managed_binding, proposed_by
"""

_UPSERT_TEMPLATE = """
    INSERT INTO proposals (
        id, business_id, entity_ref, parameter, current_value, proposed_value, diff_hash,
        classification, cause_key, cause, evidence, estimated_impact_amount,
        estimated_impact_currency, expected_contribution_delta_amount,
        expected_contribution_delta_currency, urgency, calendar_event_id, signal_id, rule_id,
        state, postponed_until, postponed_reason, owner_context, expires_at,
        execution_scheduled_at, resolved_at, created_at, managed_binding, proposed_by
    ) VALUES (
        :id, :business_id, :entity_ref, :parameter, CAST(:current_value AS JSONB),
        CAST(:proposed_value AS JSONB), :diff_hash, :classification, :cause_key, :cause,
        CAST(:evidence AS JSONB), :estimated_impact_amount, :estimated_impact_currency,
        :expected_contribution_delta_amount, :expected_contribution_delta_currency,
        :urgency, :calendar_event_id, :signal_id, {rule_by_code}, :state, :postponed_until,
        :postponed_reason, :owner_context, :expires_at, :execution_scheduled_at,
        CASE WHEN :is_resolved THEN now() ELSE NULL END, :created_at,
        CAST(:managed_binding AS JSONB), :proposed_by
    )
    ON CONFLICT (id) DO UPDATE SET
        managed_binding = EXCLUDED.managed_binding,
        current_value = EXCLUDED.current_value,
        proposed_value = EXCLUDED.proposed_value,
        diff_hash = EXCLUDED.diff_hash,
        classification = EXCLUDED.classification,
        cause_key = EXCLUDED.cause_key,
        cause = EXCLUDED.cause,
        evidence = EXCLUDED.evidence,
        estimated_impact_amount = EXCLUDED.estimated_impact_amount,
        estimated_impact_currency = EXCLUDED.estimated_impact_currency,
        expected_contribution_delta_amount = EXCLUDED.expected_contribution_delta_amount,
        expected_contribution_delta_currency = EXCLUDED.expected_contribution_delta_currency,
        urgency = EXCLUDED.urgency,
        calendar_event_id = EXCLUDED.calendar_event_id,
        signal_id = EXCLUDED.signal_id,
        rule_id = EXCLUDED.rule_id,
        state = EXCLUDED.state,
        postponed_until = EXCLUDED.postponed_until,
        postponed_reason = EXCLUDED.postponed_reason,
        owner_context = EXCLUDED.owner_context,
        expires_at = EXCLUDED.expires_at,
        execution_scheduled_at = EXCLUDED.execution_scheduled_at,
        resolved_at = COALESCE(proposals.resolved_at, EXCLUDED.resolved_at),
        proposed_by = COALESCE(proposals.proposed_by, EXCLUDED.proposed_by)
"""
_UPSERT_SQL = text(_UPSERT_TEMPLATE.format(rule_by_code=_RULE_BY_CODE))

_GET_TEMPLATE = """
    SELECT {columns} FROM proposals WHERE id = :id
"""
_GET_SQL = text(_GET_TEMPLATE.format(columns=_COLUMNS))

# FR-20: consolidacion. El UNIQUE parcial garantiza como mucho una fila.
_FIND_LIVE_EQUIVALENT_TEMPLATE = """
    SELECT {columns} FROM proposals
    WHERE entity_ref = :entity_ref AND parameter = :parameter
      AND state = ANY(:live_states)
"""
_FIND_LIVE_EQUIVALENT_SQL = text(_FIND_LIVE_EQUIVALENT_TEMPLATE.format(columns=_COLUMNS))

_FIND_PHYSICAL_EQUIVALENT_SQL = text(f"""
    SELECT {_COLUMNS} FROM proposals
    WHERE entity_ref IN ({PHYSICAL_ENTITY_REFS_SQL}) AND parameter=:parameter
      AND (state=ANY(:live_states) OR state='executing'
           OR EXISTS (SELECT 1 FROM executions x
                      LEFT JOIN execution_reservations r ON r.execution_id=x.id
                      WHERE x.proposal_id=proposals.id
                        AND (x.outcome IN ('RUNNING','UNKNOWN') OR r.state='ACTIVE')))
    ORDER BY created_at,id LIMIT 1
""")  # noqa: S608 - fixed SQL fragments, bound values

# FR-19: lentes del panel. `ix_proposals_contribution_delta (business_id,
# expected_contribution_delta_amount DESC NULLS LAST)` (0017_optimization)
# cubre el nuevo orden por defecto (profitability-engine.md §8: "la cola
# cambia de eje" -- ya no urgency/expires_at primero). Las propuestas sin
# contribucion estimada (reglas M01-M24 todavia no cablean `optimization`)
# van al final, ordenadas por urgencia -- misma regla que
# `proposals.domain.ranking.rank_proposals`, version SQL.
_LIST_BY_LENS_COLUMNS_TEMPLATE = """
    SELECT {columns} FROM proposals
    WHERE business_id = :business_id AND state = ANY(:states)
    {{filters}}
    ORDER BY expected_contribution_delta_amount DESC NULLS LAST,
             CASE urgency WHEN 'critical' THEN 0 WHEN 'recommended' THEN 1 ELSE 2 END,
             expires_at, id
    LIMIT :limit OFFSET :offset
"""
_LIST_BY_LENS_TEMPLATE = _LIST_BY_LENS_COLUMNS_TEMPLATE.format(columns=_COLUMNS)

# Caducador del MaintenanceCycle. El trigger valida pending|postponed ->
# expired; cualquier otro estado no entra en el WHERE.
_EXPIRE_DUE_SQL = text("""
    UPDATE proposals SET state = 'expired', resolved_at = now()
    WHERE id IN (
        SELECT id FROM proposals
        WHERE state IN ('pending', 'postponed') AND expires_at <= :now
        ORDER BY expires_at
        LIMIT :limit
        FOR UPDATE SKIP LOCKED
    )
    RETURNING id
""")


class ProposalIntegrityError(RuntimeError):
    """El `diff_hash` almacenado no coincide con el recalculado desde el
    payload vivo (invariante 1 de data-model.md). Nunca se ejecuta un cambio
    cuyo hash no se puede reproducir: denegar por defecto."""


@dataclass(frozen=True, slots=True)
class ProposalLens:
    """Filtro de la cola del panel (FR-19: urgencia y calendario)."""

    business_id: BusinessId
    urgency: Urgency | None = None
    calendar_event_id: uuid.UUID | None = None
    states: tuple[str, ...] = LIVE_STATES
    limit: int = 50
    offset: int = 0


def _encode_evidence(proposal: Proposal) -> str:
    """Sobre de lo que no tiene columna propia en `proposals`."""
    return json.dumps(
        {
            "items": [
                {
                    "metric": item.metric,
                    "actual": item.actual,
                    "target": item.target,
                    "window_preset": item.window_preset,
                }
                for item in proposal.evidence
            ],
            "cause": {
                "signal_id": proposal.cause.signal_id,
                "rule_id": proposal.cause.rule_id,
            },
            "cause_key": {
                "rule_id": proposal.cause_key.rule_id,
                "cause_type": proposal.cause_key.cause_type,
            },
            "expected_state_hash": proposal.expected_state_hash,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _optional_uuid(raw: str | None, field: str) -> str | None:
    """`Priority.calendar_event_id` es la clave de una fila de `calendar_events`.
    Un valor que no es UUID no se guarda a medias ni se descarta en silencio."""
    if raw is None:
        return None
    try:
        return str(uuid.UUID(raw))
    except ValueError as exc:
        raise ValueError(f"{field} debe ser el UUID de una fila existente: {raw!r}") from exc


def _optional_enum_value(reason: PostponedReason | None) -> str | None:
    return None if reason is None else reason.value


def _optional_amount(money: Money | None) -> Decimal | None:
    return None if money is None else money.amount


def _optional_currency(money: Money | None) -> str | None:
    return None if money is None else money.currency


def _row_to_optional_money(amount: Decimal | None, currency: str | None) -> Money | None:
    if amount is None or currency is None:
        return None
    return Money(amount=Decimal(amount), currency=currency)


def _upsert_params(proposal: Proposal) -> dict[str, Any]:
    diff = proposal.diff
    if (
        compute_diff_hash(
            diff.entity_ref, diff.parameter, diff.before, diff.after, diff.managed_binding
        )
        != diff.diff_hash
    ):
        raise ProposalIntegrityError("proposal_payload_hash_mismatch")
    return {
        "managed_binding": binding_to_json(diff.managed_binding),
        "id": str(proposal.proposal_id),
        "business_id": str(proposal.business_id),
        "entity_ref": str(diff.entity_ref),
        "parameter": diff.parameter,
        "current_value": encode_value(diff.before),
        "proposed_value": encode_value(diff.after),
        "diff_hash": diff.diff_hash,
        "classification": proposal.classification.value,
        "cause_key": proposal.cause_key.as_grouping_key(),
        "cause": proposal.cause.text,
        "evidence": _encode_evidence(proposal),
        "estimated_impact_amount": proposal.estimated_impact.amount,
        "estimated_impact_currency": proposal.estimated_impact.currency,
        "expected_contribution_delta_amount": _optional_amount(
            proposal.expected_contribution_delta
        ),
        "expected_contribution_delta_currency": _optional_currency(
            proposal.expected_contribution_delta
        ),
        "urgency": proposal.priority.urgency.value,
        "calendar_event_id": _optional_uuid(
            proposal.priority.calendar_event_id, "Priority.calendar_event_id"
        ),
        "signal_id": _optional_uuid(proposal.cause.signal_id, "Cause.signal_id"),
        "rule_code": proposal.cause.rule_id,
        "state": proposal.state.value,
        "postponed_until": proposal.postpone_until,
        "postponed_reason": _optional_enum_value(proposal.postponed_reason),
        "owner_context": proposal.owner_context,
        "expires_at": proposal.expires_at,
        "execution_scheduled_at": proposal.execution_scheduled_at,
        "is_resolved": proposal.state in _RESOLVED_STATES,
        "created_at": proposal.created_at,
        "proposed_by": proposal.proposed_by,
    }


def _row_to_proposal(row: Any) -> Proposal:  # noqa: ANN401 - fila heterogenea de SQLAlchemy
    envelope = json.loads(row.evidence_text)
    entity_ref = EntityRef.parse(row.entity_ref)
    diff = _row_to_diff(row, entity_ref)
    cause_key_data = envelope["cause_key"]
    return Proposal(
        proposal_id=ProposalId(uuid.UUID(str(row.id))),
        business_id=BusinessId.parse(str(row.business_id)),
        diff=diff,
        classification=Classification(row.classification),
        cause=Cause(
            text=row.cause,
            signal_id=envelope["cause"]["signal_id"],
            rule_id=envelope["cause"]["rule_id"],
        ),
        cause_key=CauseKey(
            entity_ref=entity_ref,
            rule_id=cause_key_data["rule_id"],
            cause_type=cause_key_data["cause_type"],
        ),
        evidence=tuple(Evidence(**item) for item in envelope["items"]),
        estimated_impact=Money(
            amount=Decimal(row.estimated_impact_amount),
            currency=row.estimated_impact_currency,
        ),
        priority=Priority(
            urgency=Urgency(row.urgency),
            calendar_event_id=None if row.calendar_event_id is None else str(row.calendar_event_id),
        ),
        created_at=row.created_at,
        expires_at=row.expires_at,
        state=ProposalState(row.state),
        expected_state_hash=envelope["expected_state_hash"],
        postpone_until=row.postponed_until,
        postponed_reason=(
            None if row.postponed_reason is None else PostponedReason(row.postponed_reason)
        ),
        owner_context=row.owner_context,
        execution_scheduled_at=row.execution_scheduled_at,
        expected_contribution_delta=_row_to_optional_money(
            row.expected_contribution_delta_amount, row.expected_contribution_delta_currency
        ),
        proposed_by=row.proposed_by,
    )


def _row_to_diff(row: Any, entity_ref: EntityRef) -> ProposedDiff:  # noqa: ANN401
    before = decode_value(row.current_value_text)
    after = decode_value(row.proposed_value_text)
    binding = binding_from_json(row.managed_binding)
    recomputed = compute_diff_hash(entity_ref, row.parameter, before, after, binding)
    if recomputed != row.diff_hash:
        raise ProposalIntegrityError(
            f"proposal {row.id}: diff_hash almacenado {row.diff_hash} != recalculado {recomputed}"
        )
    return ProposedDiff(
        entity_ref=entity_ref,
        parameter=row.parameter,
        before=before,
        after=after,
        diff_hash=row.diff_hash,
        managed_binding=binding,
    )


class SqlProposalRepository:
    """Implementa `proposals.application.ports.ProposalRepository`
    estructuralmente (sin heredar del `Protocol`), mas las consultas de cola
    que el panel y el `MaintenanceCycle` necesitan."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, proposal_id: ProposalId) -> Proposal | None:
        result = await self._session.execute(_GET_SQL, {"id": str(proposal_id)})
        row = result.one_or_none()
        return None if row is None else _row_to_proposal(row)

    async def save(self, proposal: Proposal) -> None:
        await self._session.execute(_UPSERT_SQL, _upsert_params(proposal))
        record_proposal_saved(proposal.state.value)

    async def find_live_equivalent(self, entity_ref: EntityRef, parameter: str) -> Proposal | None:
        """Physical FR-20, serialized through the same account lock as execution.

        Unknown/active reservations remain covering even if the proposal was
        marked terminal. No authorization or execution receipt is rewritten.
        """
        account_id = (
            await self._session.execute(
                text("SELECT platform_account_id FROM ad_entities WHERE entity_ref=:entity_ref"),
                {"entity_ref": str(entity_ref)},
            )
        ).scalar_one_or_none()
        if account_id is not None:
            await self._session.execute(
                text(PHYSICAL_ACCOUNT_LOCK_SQL), {"platform_account_id": account_id}
            )
        result = await self._session.execute(
            _FIND_PHYSICAL_EQUIVALENT_SQL if account_id is not None else _FIND_LIVE_EQUIVALENT_SQL,
            {
                "entity_ref": str(entity_ref),
                "parameter": parameter,
                "live_states": list(LIVE_STATES),
            },
        )
        row = result.one_or_none()
        return None if row is None else _row_to_proposal(row)

    async def list_by_lens(self, lens: ProposalLens) -> tuple[Proposal, ...]:
        filters: list[str] = []
        params: dict[str, Any] = {
            "business_id": str(lens.business_id),
            "states": list(lens.states),
            "limit": lens.limit,
            "offset": lens.offset,
        }
        if lens.urgency is not None:
            filters.append("AND urgency = :urgency")
            params["urgency"] = lens.urgency.value
        if lens.calendar_event_id is not None:
            filters.append("AND calendar_event_id = :calendar_event_id")
            params["calendar_event_id"] = str(lens.calendar_event_id)
        sql = text(_LIST_BY_LENS_TEMPLATE.format(filters="\n    ".join(filters)))
        result = await self._session.execute(sql, params)
        return tuple(_row_to_proposal(row) for row in result.all())

    async def expire_due(self, now: datetime, limit: int = 500) -> tuple[ProposalId, ...]:
        """Caduca en lote lo vencido. `SKIP LOCKED` para que dos ciclos
        solapados no se bloqueen entre si sobre las mismas filas."""
        result = await self._session.execute(_EXPIRE_DUE_SQL, {"now": now, "limit": limit})
        return tuple(ProposalId(uuid.UUID(str(row.id))) for row in result.all())
