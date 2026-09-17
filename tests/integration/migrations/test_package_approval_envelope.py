"""0044 -- el sobre de aprobacion del paquete y el sujeto de autorizacion
(data-model.md "Revision 2" §R2.2/§R2.5). Ida y vuelta sobre base vacia y
sobre base con datos, mismo criterio que `test_campaign_packages.py`."""

import asyncpg
import pytest
from sqlalchemy.exc import DBAPIError
from ulid import ULID

from tests.integration.migrations.conftest import (
    downgrade,
    make_approval,
    make_business,
    make_entity,
    make_platform_account,
    make_proposal,
    state_hash,
    upgrade,
)
from tests.integration.migrations.test_campaign_packages import insert_package, seed_scope
from tests.integration.migrations.test_connection_identity import (
    migration_sandbox as migration_sandbox,  # noqa: PLC0414 - pytest fixture re-export
)

pytestmark = pytest.mark.integration
PREVIOUS = "0043_proposed_by"
CURRENT = "0044_package_approval_envelope"

_ENVELOPE_HASH = "e" * 64


async def insert_publication(pg, package_id, authorization_id, *, envelope_hash=_ENVELOPE_HASH):
    publication_id = str(ULID())
    await pg.execute(
        """INSERT INTO campaign_package_publications
               (id, package_id, authorization_id, approval_envelope, approval_signature,
                envelope_hash, approval_expires_at, approved_plan)
           VALUES ($1, $2, $3, '{"step_plan": []}'::jsonb, $4, $5,
                   now() + interval '30 minutes', '{"campaign": {}}'::jsonb)""",
        publication_id,
        package_id,
        authorization_id,
        b"firma-ed25519-de-prueba",
        envelope_hash,
    )
    return publication_id


async def insert_package_authorization(pg, *, subject_id):
    return await pg.fetchval(
        """INSERT INTO approvals (proposal_id, kind, decision, diff_hash,
                                  guardrail_verdict_hash, issued_by, channel, signature,
                                  expires_at, subject_kind, subject_id)
           VALUES (NULL, 'human_approval', 'approved', $1, $2, 'owner', 'panel',
                   'firma-ed25519-de-prueba', now() + interval '30 minutes', 'package', $3)
           RETURNING id""",
        state_hash(f"diff|{subject_id}"),
        state_hash(f"guardrail|{subject_id}"),
        subject_id,
    )


async def test_publication_requires_the_signed_envelope(migration_sandbox):
    dsn, pg = migration_sandbox
    upgrade(dsn, CURRENT)
    business, account_ref, offering, account = await seed_scope(pg)
    package = await insert_package(pg, business, account_ref, offering)
    entity = await make_entity(pg, business, account)
    proposal = await make_proposal(pg, business, entity["entity_ref"])
    authorization = await make_approval(pg, proposal["id"], proposal["diff_hash"])

    with pytest.raises(asyncpg.NotNullViolationError):
        await pg.execute(
            "INSERT INTO campaign_package_publications (id, package_id, authorization_id) "
            "VALUES ($1, $2, $3)",
            str(ULID()),
            package,
            authorization,
        )

    publication = await insert_publication(pg, package, authorization)
    row = await pg.fetchrow(
        "SELECT envelope_hash, approval_expires_at FROM campaign_package_publications "
        "WHERE id = $1",
        publication,
    )
    assert row["envelope_hash"] == _ENVELOPE_HASH


async def test_campaign_package_publish_as_accepts_only_an_object(migration_sandbox):
    dsn, pg = migration_sandbox
    upgrade(dsn, CURRENT)
    business, account_ref, offering, _ = await seed_scope(pg)
    package = await insert_package(pg, business, account_ref, offering)

    await pg.execute(
        "UPDATE campaign_packages SET publish_as = $2 WHERE id = $1",
        package,
        '{"page_id": "123", "page_name": "Clinica X"}',
    )
    with pytest.raises(asyncpg.CheckViolationError):
        await pg.execute(
            "UPDATE campaign_packages SET publish_as = $2 WHERE id = $1", package, '"not-an-object"'
        )


async def test_authorization_subject_is_exclusive_with_proposal_id(migration_sandbox):
    dsn, pg = migration_sandbox
    upgrade(dsn, CURRENT)

    package_authorization = await insert_package_authorization(pg, subject_id="pkg-1")
    assert package_authorization is not None

    business = await make_business(pg)
    account = await make_platform_account(pg, business)
    entity = await make_entity(pg, business, account)
    proposal = await make_proposal(pg, business, entity["entity_ref"])
    proposal_authorization = await make_approval(pg, proposal["id"], proposal["diff_hash"])
    assert proposal_authorization is not None

    with pytest.raises(asyncpg.CheckViolationError, match="approvals_subject_exclusive_check"):
        await pg.execute(
            """INSERT INTO approvals (proposal_id, kind, decision, diff_hash,
                   guardrail_verdict_hash, issued_by, channel, signature, expires_at)
               VALUES (NULL, 'human_approval', 'approved', $1, $2, 'owner', 'panel',
                       'firma-ed25519-de-prueba', now() + interval '30 minutes')""",
            state_hash("diff|neither"),
            state_hash("guardrail|neither"),
        )
    with pytest.raises(asyncpg.CheckViolationError, match="approvals_subject_exclusive_check"):
        await pg.execute(
            """INSERT INTO approvals (proposal_id, kind, decision, diff_hash,
                   guardrail_verdict_hash, issued_by, channel, signature, expires_at,
                   subject_kind, subject_id)
               VALUES ($1, 'human_approval', 'approved', $2, $3, 'owner', 'panel',
                       'firma-ed25519-de-prueba', now() + interval '30 minutes',
                       'package', 'pkg-2')""",
            proposal["id"],
            state_hash("diff|both"),
            state_hash("guardrail|both"),
        )


async def test_downgrade_refuses_with_package_history_present(migration_sandbox):
    dsn, pg = migration_sandbox
    upgrade(dsn, CURRENT)
    await insert_package_authorization(pg, subject_id="pkg-1")

    with pytest.raises(DBAPIError, match="approvals_package_subject_history_present"):
        downgrade(dsn, PREVIOUS)
    assert await pg.fetchval("SELECT version_num FROM alembic_version") == CURRENT


async def test_round_trip_on_empty_database(migration_sandbox):
    dsn, pg = migration_sandbox
    upgrade(dsn, CURRENT)
    downgrade(dsn, PREVIOUS)
    assert await pg.fetchval("SELECT version_num FROM alembic_version") == PREVIOUS
    upgrade(dsn, CURRENT)
    assert await pg.fetchval("SELECT version_num FROM alembic_version") == CURRENT
