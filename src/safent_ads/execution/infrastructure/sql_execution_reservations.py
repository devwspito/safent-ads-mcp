"""Same SQL session/transaction as gates, ledger and execution outcomes."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.execution.domain.execution_attempt import ExecutionAttempt
from safent_ads.proposals.domain.proposal import ProposedDiff
from safent_ads.proposals.infrastructure.value_codec import (
    decode_value,
    encode_value,
    money_to_minor,
)
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import EntityRef
from safent_ads.shared.managed_ads import binding_from_json, binding_to_json


class SqlExecutionReservations:
    def __init__(self, session: AsyncSession, clock: Clock) -> None:
        self._session = session
        self._clock = clock

    async def reserve(self, attempt: ExecutionAttempt, effective: ProposedDiff) -> None:
        result = await self._session.execute(
            text("""
            INSERT INTO execution_reservations
                (execution_id, business_id, platform_account_id, entity_ref, currency,
                 positive_delta_minor, parameter, previous_value, proposed_value,
                 diff_hash, created_at, managed_binding)
            SELECT :execution_id, e.business_id, e.platform_account_id, e.entity_ref,
                   a.currency, :delta, :parameter, CAST(:before AS JSONB),
                   CAST(:after AS JSONB), :diff_hash, :now, CAST(:managed_binding AS JSONB)
              FROM ads_execution_targets e JOIN platform_accounts a ON a.id = e.platform_account_id
             WHERE e.entity_ref = :entity AND e.business_id = :business_id
            RETURNING execution_id
        """),
            {
                "execution_id": str(attempt.execution_id),
                "business_id": str(attempt.business_id),
                "entity": str(effective.entity_ref),
                "parameter": effective.parameter,
                "delta": max(0, money_to_minor(effective.after) - money_to_minor(effective.before)),
                "before": encode_value(effective.before),
                "after": encode_value(effective.after),
                "diff_hash": effective.diff_hash,
                "now": self._clock.now(),
                "managed_binding": binding_to_json(effective.managed_binding),
            },
        )
        if result.first() is None:
            raise ValueError("reservation_scope_missing")

    async def get(self, attempt: ExecutionAttempt) -> ProposedDiff | None:
        result = await self._session.execute(
            text("""
            SELECT entity_ref, parameter, previous_value::text AS before,
                   proposed_value::text AS after, diff_hash, managed_binding
              FROM execution_reservations
             WHERE execution_id = :id AND business_id = :business AND state = 'ACTIVE'
        """),
            {"id": str(attempt.execution_id), "business": str(attempt.business_id)},
        )
        row = result.mappings().one_or_none()
        if row is None:
            return None
        diff = ProposedDiff.build(
            entity_ref=EntityRef.parse(row["entity_ref"]),
            parameter=row["parameter"],
            before=decode_value(row["before"]),
            after=decode_value(row["after"]),
            managed_binding=binding_from_json(row["managed_binding"]),
        )
        if diff.diff_hash != row["diff_hash"]:
            raise ValueError("reservation_payload_hash_mismatch")
        return diff

    async def resolve(self, attempt: ExecutionAttempt, *, applied: bool) -> None:
        await self._session.execute(
            text("""
            UPDATE execution_reservations SET state = :state, resolved_at = :now
             WHERE execution_id = :id AND business_id = :business AND state = 'ACTIVE'
        """),
            {
                "id": str(attempt.execution_id),
                "business": str(attempt.business_id),
                "state": "SETTLED" if applied else "RELEASED",
                "now": self._clock.now(),
            },
        )
