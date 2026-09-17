"""0008_proposals: FR-20 (una sola propuesta abierta por entidad y
parameter), rotacion obligatoria del `diff_hash` y `approvals` inmutable."""

from __future__ import annotations

import asyncpg
import pytest

from tests.integration.migrations.conftest import (
    make_approval,
    make_business,
    make_entity,
    make_platform_account,
    make_proposal,
    state_hash,
)

pytestmark = pytest.mark.integration

_UPSERT_EQUIVALENT = """
    INSERT INTO proposals (business_id, entity_ref, parameter, current_value, proposed_value,
                           diff_hash, classification, cause_key, cause,
                           estimated_impact_amount, estimated_impact_currency, urgency,
                           expires_at)
    VALUES ($1, $2, 'daily_budget', '{"amount": 60}'::jsonb, $3::jsonb, $4, 'routine',
            'limitada-por-presupuesto', 'Limitada por presupuesto', 410, 'EUR', 'critical',
            now() + interval '24 hours')
    ON CONFLICT (entity_ref, parameter)
        WHERE state IN ('pending', 'postponed', 'approved', 'scheduled')
    DO UPDATE SET proposed_value          = EXCLUDED.proposed_value,
                  diff_hash               = EXCLUDED.diff_hash,
                  cause                   = EXCLUDED.cause,
                  estimated_impact_amount = EXCLUDED.estimated_impact_amount,
                  urgency                 = EXCLUDED.urgency
    RETURNING id, diff_hash
"""


async def _entity(pg: asyncpg.Connection) -> tuple[str, str]:
    business_id = await make_business(pg)
    account_id = await make_platform_account(pg, business_id)
    entity = await make_entity(pg, business_id, account_id)
    return business_id, entity["entity_ref"]


async def test_equivalent_proposal_updates_not_duplicates(pg: asyncpg.Connection) -> None:
    business_id, entity_ref = await _entity(pg)
    first = await make_proposal(pg, business_id, entity_ref)

    updated = await pg.fetchrow(
        _UPSERT_EQUIVALENT,
        business_id,
        entity_ref,
        '{"amount": 92, "currency": "EUR"}',
        state_hash("propuesta-equivalente"),
    )

    rows = await pg.fetch(
        "SELECT id, proposed_value, diff_hash FROM proposals WHERE entity_ref = $1", entity_ref
    )
    assert len(rows) == 1
    assert updated["id"] == first["id"]
    assert updated["diff_hash"] != first["diff_hash"]
    assert '"amount": 92' in rows[0]["proposed_value"]


async def test_second_open_proposal_for_the_same_parameter_is_rejected(
    pg: asyncpg.Connection,
) -> None:
    business_id, entity_ref = await _entity(pg)
    await make_proposal(pg, business_id, entity_ref)

    with pytest.raises(asyncpg.UniqueViolationError, match="ix_proposals_open_per_parameter"):
        await make_proposal(pg, business_id, entity_ref)


async def test_a_resolved_proposal_frees_the_parameter(pg: asyncpg.Connection) -> None:
    business_id, entity_ref = await _entity(pg)
    first = await make_proposal(pg, business_id, entity_ref)
    await pg.execute(
        "UPDATE proposals SET state = 'rejected', resolved_at = now() WHERE id = $1",
        first["id"],
    )

    second = await make_proposal(pg, business_id, entity_ref)

    assert second["id"] != first["id"]


async def test_editing_the_value_without_rotating_the_hash_is_rejected(
    pg: asyncpg.Connection,
) -> None:
    business_id, entity_ref = await _entity(pg)
    proposal = await make_proposal(pg, business_id, entity_ref)

    with pytest.raises(asyncpg.RaiseError, match="rotar el diff_hash"):
        await pg.execute(
            "UPDATE proposals SET proposed_value = '{\"amount\": 120}'::jsonb WHERE id = $1",
            proposal["id"],
        )


async def test_illegal_state_transition_is_rejected(pg: asyncpg.Connection) -> None:
    business_id, entity_ref = await _entity(pg)
    proposal = await make_proposal(pg, business_id, entity_ref)

    with pytest.raises(asyncpg.RaiseError, match="pending -> executing no permitida"):
        await pg.execute(
            "UPDATE proposals SET state = 'executing' WHERE id = $1", proposal["id"]
        )


async def test_grace_window_state_needs_its_schedule(pg: asyncpg.Connection) -> None:
    business_id, entity_ref = await _entity(pg)
    proposal = await make_proposal(pg, business_id, entity_ref)
    await pg.execute("UPDATE proposals SET state = 'approved' WHERE id = $1", proposal["id"])

    with pytest.raises(asyncpg.CheckViolationError, match="scheduled_needs_time"):
        await pg.execute(
            "UPDATE proposals SET state = 'scheduled' WHERE id = $1", proposal["id"]
        )

    await pg.execute(
        """
        UPDATE proposals
           SET state = 'scheduled', execution_scheduled_at = now() + interval '30 minutes'
         WHERE id = $1
        """,
        proposal["id"],
    )


async def test_approvals_are_append_only(pg: asyncpg.Connection) -> None:
    business_id, entity_ref = await _entity(pg)
    proposal = await make_proposal(pg, business_id, entity_ref)
    approval_id = await make_approval(pg, proposal["id"], proposal["diff_hash"])

    with pytest.raises(asyncpg.RaiseError, match="append-only"):
        await pg.execute(
            "UPDATE approvals SET decision = 'revoked' WHERE id = $1", approval_id
        )
    with pytest.raises(asyncpg.RaiseError, match="append-only"):
        await pg.execute("DELETE FROM approvals WHERE id = $1", approval_id)

    revocation = await make_approval(
        pg, proposal["id"], proposal["diff_hash"], decision="revoked"
    )
    assert revocation != approval_id


async def test_the_same_diff_is_not_approved_twice(pg: asyncpg.Connection) -> None:
    business_id, entity_ref = await _entity(pg)
    proposal = await make_proposal(pg, business_id, entity_ref)
    await make_approval(pg, proposal["id"], proposal["diff_hash"])

    with pytest.raises(asyncpg.UniqueViolationError, match="ix_approvals_live_decision"):
        await make_approval(pg, proposal["id"], proposal["diff_hash"])


async def test_important_proposal_refuses_a_rule_authorization(pg: asyncpg.Connection) -> None:
    business_id, entity_ref = await _entity(pg)
    proposal = await make_proposal(pg, business_id, entity_ref, classification="important")
    rule_id = await pg.fetchval("SELECT id FROM rules WHERE code = 'M02' AND scope = 'global'")

    with pytest.raises(asyncpg.RaiseError, match="important exige human_approval"):
        await pg.execute(
            """
            INSERT INTO approvals (proposal_id, kind, decision, diff_hash,
                                   guardrail_verdict_hash, issued_by, rule_id, channel,
                                   signature, expires_at)
            VALUES ($1, 'rule_authorization', 'approved', $2, $3, 'M02', $4, 'rule_engine',
                    'firma-ed25519-de-prueba', now() + interval '1 hour')
            """,
            proposal["id"],
            proposal["diff_hash"],
            state_hash("guardrail-verdict"),
            rule_id,
        )
