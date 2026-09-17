"""0045 -- encadena `package_step` a la `human_approval` de la que deriva
(data-model.md "Veredicto T002": recomendacion adoptada en vez de relajar
`approvals_kind_matches_classification`). Ida y vuelta sobre base vacia y
sobre base con datos, mismo criterio que `test_campaign_packages.py`."""

import asyncpg
import pytest
from sqlalchemy.exc import DBAPIError

from tests.integration.migrations.conftest import (
    downgrade,
    make_approval,
    make_entity,
    make_proposal,
    state_hash,
    upgrade,
)
from tests.integration.migrations.test_campaign_packages import seed_scope
from tests.integration.migrations.test_connection_identity import (
    migration_sandbox as migration_sandbox,  # noqa: PLC0414 - pytest fixture re-export
)
from tests.integration.migrations.test_package_approval_envelope import (
    insert_package_authorization,
)

pytestmark = pytest.mark.integration
PREVIOUS = "0044_package_approval_envelope"
CURRENT = "0045_package_step_auth_chain"


async def make_important_proposal(pg, business, account):
    entity = await make_entity(pg, business, account)
    return await make_proposal(
        pg, business, entity["entity_ref"], parameter="new_campaign:abc", classification="important"
    )


async def make_package_step_approval(pg, proposal_id, diff_hash, *, derived_from_authorization_id):
    return await pg.fetchval(
        """INSERT INTO approvals (proposal_id, kind, decision, diff_hash, guardrail_verdict_hash,
                                  issued_by, channel, signature, expires_at,
                                  derived_from_authorization_id)
           VALUES ($1, 'package_step', 'approved', $2, $3, 'ads-worker', 'rule_engine',
                   'firma-ed25519-de-prueba', now() + interval '1 hour', $4)
           RETURNING id""",
        proposal_id,
        diff_hash,
        state_hash(f"guardrail|{diff_hash}"),
        derived_from_authorization_id,
    )


async def test_chain_column_and_check_roundtrip(migration_sandbox):
    dsn, pg = migration_sandbox
    upgrade(dsn, PREVIOUS)
    upgrade(dsn, CURRENT)

    columns = await pg.fetch(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'approvals' AND column_name = 'derived_from_authorization_id'"
    )
    assert len(columns) == 1

    downgrade(dsn, PREVIOUS)
    assert await pg.fetchval("SELECT version_num FROM alembic_version") == PREVIOUS
    columns = await pg.fetch(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'approvals' AND column_name = 'derived_from_authorization_id'"
    )
    assert len(columns) == 0
    upgrade(dsn, CURRENT)


async def test_package_step_without_a_chain_never_satisfies_important_classification(
    migration_sandbox,
):
    """Regresion explicita: sin `derived_from_authorization_id`, un
    `package_step` sigue sin poder autorizar una propuesta important/critical
    -- exactamente el mismo veredicto que antes de esta migracion (0042)."""
    dsn, pg = migration_sandbox
    upgrade(dsn, CURRENT)
    business, _, _, account = await seed_scope(pg)
    creation = await make_important_proposal(pg, business, account)

    with pytest.raises(asyncpg.RaiseError, match="exige human_approval"):
        await pg.execute(
            "INSERT INTO approvals (proposal_id, kind, decision, diff_hash, "
            "guardrail_verdict_hash, issued_by, channel, signature, expires_at) "
            "VALUES ($1, 'package_step', 'approved', $2, $3, 'ads-worker', 'rule_engine', "
            "'firma-ed25519-de-prueba', now() + interval '1 hour')",
            creation["id"],
            creation["diff_hash"],
            state_hash(f"guardrail|{creation['diff_hash']}"),
        )


async def test_package_step_chained_to_a_live_human_package_approval_is_admitted(
    migration_sandbox,
):
    dsn, pg = migration_sandbox
    upgrade(dsn, CURRENT)
    business, _, _, account = await seed_scope(pg)
    human = await insert_package_authorization(pg, subject_id="pkg-1")
    creation = await make_important_proposal(pg, business, account)

    step_authorization = await make_package_step_approval(
        pg, creation["id"], creation["diff_hash"], derived_from_authorization_id=human
    )
    assert step_authorization is not None


async def test_package_step_chained_to_a_bogus_authorization_is_still_denied(migration_sandbox):
    """"Nunca debilitar el guarda»: apuntar a una autorizacion que no es una
    `human_approval` de sujeto `package` viva no cuela, aunque el campo
    este relleno."""
    dsn, pg = migration_sandbox
    upgrade(dsn, CURRENT)
    business, _, _, account = await seed_scope(pg)
    other_proposal = await make_important_proposal(pg, business, account)
    not_a_package_approval = await make_approval(
        pg, other_proposal["id"], other_proposal["diff_hash"]
    )
    creation = await make_important_proposal(pg, business, account)

    with pytest.raises(asyncpg.RaiseError, match="exige human_approval"):
        await make_package_step_approval(
            pg,
            creation["id"],
            creation["diff_hash"],
            derived_from_authorization_id=not_a_package_approval,
        )


async def test_package_step_chain_check_is_exclusive_by_kind(migration_sandbox):
    """`approvals` es solo-anexable (`approvals_no_update`): la exclusividad
    se prueba al INSERTAR, nunca actualizando una fila ya anexada."""
    dsn, pg = migration_sandbox
    upgrade(dsn, CURRENT)
    business, _, _, account = await seed_scope(pg)
    entity = await make_entity(pg, business, account)
    proposal = await make_proposal(pg, business, entity["entity_ref"])
    human = await insert_package_authorization(pg, subject_id="pkg-2")

    # `human_approval` (o `rule_authorization`) NUNCA lleva
    # `derived_from_authorization_id`: solo `package_step` lo exige.
    with pytest.raises(asyncpg.CheckViolationError, match="approvals_package_step_chain_check"):
        await pg.execute(
            "INSERT INTO approvals (proposal_id, kind, decision, diff_hash, "
            "guardrail_verdict_hash, issued_by, channel, signature, expires_at, "
            "derived_from_authorization_id) "
            "VALUES ($1, 'human_approval', 'approved', $2, $3, 'owner', 'panel', "
            "'firma-ed25519-de-prueba', now() + interval '1 hour', $4)",
            proposal["id"],
            proposal["diff_hash"],
            state_hash(f"guardrail|{proposal['diff_hash']}"),
            human,
        )


async def test_upload_creative_joins_the_step_kind_vocabulary(migration_sandbox):
    dsn, pg = migration_sandbox
    upgrade(dsn, PREVIOUS)
    business, account_ref, offering, _ = await seed_scope(pg)
    del business, account_ref, offering  # solo se necesita para que la migracion previa exista

    upgrade(dsn, CURRENT)
    constraint = await pg.fetchval(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conname = 'campaign_package_steps_kind_check' "
        "AND conrelid = 'campaign_package_steps'::regclass"
    )
    assert "upload_creative" in constraint


async def test_downgrade_refuses_to_erase_chain_or_upload_history(migration_sandbox):
    dsn, pg = migration_sandbox
    upgrade(dsn, CURRENT)
    business, _, _, account = await seed_scope(pg)
    human = await insert_package_authorization(pg, subject_id="pkg-3")
    creation = await make_important_proposal(pg, business, account)
    await make_package_step_approval(
        pg, creation["id"], creation["diff_hash"], derived_from_authorization_id=human
    )

    with pytest.raises(DBAPIError, match="approvals_package_step_chain_history_present"):
        downgrade(dsn, PREVIOUS)
    assert await pg.fetchval("SELECT version_num FROM alembic_version") == CURRENT
