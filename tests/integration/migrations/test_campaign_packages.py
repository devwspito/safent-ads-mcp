"""0042 guarda el arbol del paquete, su publicacion y cada paso.

La ida y vuelta se demuestra corriendola sobre base vacia y sobre base con
datos: un downgrade que no se ha ejecutado no existe (mismo criterio que
`test_migration_round_trip`)."""

import asyncpg
import pytest
from sqlalchemy.exc import DBAPIError
from ulid import ULID

from tests.integration.migrations.conftest import (
    downgrade,
    make_approval,
    make_business,
    make_entity,
    make_offering,
    make_platform_account,
    make_proposal,
    upgrade,
)
from tests.integration.migrations.test_connection_identity import (
    migration_sandbox as migration_sandbox,  # noqa: PLC0414 - pytest fixture re-export
)

pytestmark = pytest.mark.integration
PREVIOUS = "0041_campaign_drafts"
CURRENT = "0042_campaign_packages"

_PLAN = '{"campaign": {"name": "Reserva de citas"}}'
_HASH = "d" * 64


async def constraint(pg, name):
    return await pg.fetchval(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conname = $1 AND conrelid = 'approvals'::regclass",
        name,
    )


async def seed_scope(pg):
    """Negocio con cuenta y oferta propias: la pertenencia que el paquete exige."""
    business = await make_business(pg)
    account = await make_platform_account(pg, business)
    account_ref = await pg.fetchval(
        "SELECT account_ref FROM platform_accounts WHERE id = $1", account
    )
    return business, account_ref, await make_offering(pg, business), account


async def insert_package(pg, business, account_ref, offering, *, state="proposed", digest=_HASH):
    package_id = str(ULID())
    await pg.execute(
        """INSERT INTO campaign_packages (id, business_id, platform, account_ref, offering_id,
               state, package_hash, plan, budget, rationale, expires_at)
           VALUES ($1, $2, 'meta', $3, $4, $5, $6, $7::jsonb, '{"daily": "20.00"}'::jsonb,
                   '{"why": "lo pidio el dueno"}'::jsonb, now() + interval '2 days')""",
        package_id,
        business,
        account_ref,
        offering,
        state,
        digest,
        _PLAN,
    )
    return package_id


async def seed_authorization(pg, business, account, *, classification="routine"):
    entity = await make_entity(pg, business, account)
    proposal = await make_proposal(
        pg, business, entity["entity_ref"], classification=classification
    )
    return proposal, await make_approval(pg, proposal["id"], proposal["diff_hash"])


async def seed_publication(pg, package_id, authorization_id):
    publication = str(ULID())
    await pg.execute(
        "INSERT INTO campaign_package_publications (id, package_id, authorization_id) "
        "VALUES ($1, $2, $3)",
        publication,
        package_id,
        authorization_id,
    )
    await pg.executemany(
        "INSERT INTO campaign_package_steps (publication_id, step_index, kind, local_ref, "
        "parent_local_ref) VALUES ($1, $2, $3, $4, $5)",
        [
            (publication, 0, "create_campaign", "campaign", None),
            (publication, 1, "create_ad_set", "as#1", "campaign"),
            (publication, 2, "create_ad", "as#1/ad#1", "as#1"),
            (publication, 3, "activate_campaign", "campaign", None),
        ],
    )
    return publication


async def test_package_step_vocabulary_and_partial_indexes_roundtrip(migration_sandbox):
    dsn, pg = migration_sandbox
    upgrade(dsn, PREVIOUS)
    assert "package_step" not in await constraint(pg, "approvals_kind_check")

    upgrade(dsn, CURRENT)
    # `kind` es TEXT + CHECK, no un ENUM: admitir un valor nuevo es reescribir
    # las dos CHECK, sin reescribir la tabla ni ningun tipo (T002).
    assert "package_step" in await constraint(pg, "approvals_kind_check")
    assert "package_step" in await constraint(pg, "approvals_rule_kind_check")
    indexes = await pg.fetch(
        "SELECT indexname, indexdef FROM pg_indexes WHERE tablename LIKE 'campaign_package%'"
    )
    partial = {row["indexname"]: row["indexdef"] for row in indexes}
    assert "WHERE (state = ANY" in partial["ix_campaign_packages_open_dedup"]
    assert "UNIQUE" in partial["ix_campaign_package_steps_single_running"]

    downgrade(dsn, PREVIOUS)
    assert await pg.fetchval("SELECT version_num FROM alembic_version") == PREVIOUS
    assert "package_step" not in await constraint(pg, "approvals_kind_check")
    assert "package_step" not in await constraint(pg, "approvals_rule_kind_check")
    upgrade(dsn, CURRENT)
    assert "package_step" in await constraint(pg, "approvals_kind_check")


async def test_package_belongs_to_one_business_and_only_one_stays_open(migration_sandbox):
    dsn, pg = migration_sandbox
    upgrade(dsn, CURRENT)
    business, account_ref, offering, _ = await seed_scope(pg)
    _, ajena, _, _ = await seed_scope(pg)
    await insert_package(pg, business, account_ref, offering)

    # FR-20: un solo paquete abierto por (negocio, cuenta, oferta).
    with pytest.raises(asyncpg.UniqueViolationError, match="ix_campaign_packages_open_dedup"):
        await insert_package(pg, business, account_ref, offering, state="draft")
    # La cuenta de otro negocio no es alcanzable ni equivocandose.
    with pytest.raises(asyncpg.ForeignKeyViolationError, match="campaign_packages_account_fk"):
        await insert_package(pg, business, ajena, offering, state="draft")
    with pytest.raises(asyncpg.RaiseError, match="nace draft o proposed"):
        await insert_package(pg, business, account_ref, offering, state="published")
    # Un paquete publicado sigue permitiendo proponer otro para la misma oferta.
    await pg.execute("UPDATE campaign_packages SET state = 'rejected'")
    await insert_package(pg, business, account_ref, offering)


async def test_package_transitions_go_one_hop_and_the_hash_follows_the_plan(migration_sandbox):
    dsn, pg = migration_sandbox
    upgrade(dsn, CURRENT)
    business, account_ref, offering, _ = await seed_scope(pg)
    package = await insert_package(pg, business, account_ref, offering)

    with pytest.raises(asyncpg.RaiseError, match="transicion proposed -> published no permitida"):
        await pg.execute("UPDATE campaign_packages SET state = 'published' WHERE id = $1", package)
    with pytest.raises(asyncpg.RaiseError, match="rotar el package_hash"):
        await pg.execute(
            'UPDATE campaign_packages SET plan = \'{"campaign": {"name": "Otra"}}\'::jsonb '
            "WHERE id = $1",
            package,
        )
    with pytest.raises(asyncpg.RaiseError, match="pertenencia del paquete es inmutable"):
        await pg.execute(
            "UPDATE campaign_packages SET account_ref = 'meta:act_otra' WHERE id = $1", package
        )

    for state in ("approved", "publishing", "verifying", "published"):
        await pg.execute("UPDATE campaign_packages SET state = $2 WHERE id = $1", package, state)
    assert await pg.fetchval("SELECT state FROM campaign_packages WHERE id = $1", package) == (
        "published"
    )
    # `published` es terminal: deshacer pausa la campana, no reabre el paquete.
    with pytest.raises(asyncpg.RaiseError, match="transicion published -> proposed no permitida"):
        await pg.execute("UPDATE campaign_packages SET state = 'proposed' WHERE id = $1", package)


async def test_publication_writes_one_step_at_a_time_and_never_repeats_one(migration_sandbox):
    dsn, pg = migration_sandbox
    upgrade(dsn, CURRENT)
    business, account_ref, offering, account = await seed_scope(pg)
    package = await insert_package(pg, business, account_ref, offering)
    _, authorization = await seed_authorization(pg, business, account)
    publication = await seed_publication(pg, package, authorization)

    with pytest.raises(asyncpg.UniqueViolationError):
        await pg.execute(
            "INSERT INTO campaign_package_publications (id, package_id, authorization_id) "
            "VALUES ($1, $2, $3)",
            str(ULID()),
            package,
            authorization,
        )
    with pytest.raises(asyncpg.RaiseError, match="activar es siempre el ultimo paso"):
        await pg.execute(
            "INSERT INTO campaign_package_steps (publication_id, step_index, kind, local_ref, "
            "parent_local_ref) VALUES ($1, 4, 'create_ad', 'as#1/ad#2', 'as#1')",
            publication,
        )

    running = "UPDATE campaign_package_steps SET state = 'running' WHERE step_index = $1"
    with pytest.raises(asyncpg.RaiseError, match="pasos anteriores sin terminar"):
        await pg.execute(running, 1)
    await pg.execute(running, 0)
    await pg.execute(
        "UPDATE campaign_package_steps SET state = 'done', "
        "created_entity_ref = 'meta:campaign:c-9' WHERE step_index = 0"
    )
    with pytest.raises(asyncpg.RaiseError, match="transicion done -> running no permitida"):
        await pg.execute(running, 0)
    with pytest.raises(asyncpg.RaiseError, match="recibo confirmado es inmutable"):
        await pg.execute(
            "UPDATE campaign_package_steps SET created_entity_ref = 'meta:campaign:otra' "
            "WHERE step_index = 0"
        )

    # Un paso `unknown` detiene la saga hasta reconciliarse por recibo.
    await pg.execute(running, 1)
    await pg.execute("UPDATE campaign_package_steps SET state = 'unknown' WHERE step_index = 1")
    with pytest.raises(asyncpg.RaiseError, match="pasos anteriores sin terminar"):
        await pg.execute(running, 2)
    # Activar no crea entidad: no hay recibo que guardar.
    with pytest.raises(asyncpg.CheckViolationError, match="steps_receipt_check"):
        await pg.execute(
            "UPDATE campaign_package_steps SET created_entity_ref = 'meta:campaign:c-9' "
            "WHERE step_index = 3"
        )


async def test_package_step_authorization_still_meets_the_human_signature_gate(migration_sandbox):
    """El CHECK admite `package_step`, pero `approvals_kind_matches_classification`
    sigue exigiendo firma humana en lo `important`/`critical` -- y todo paso de
    paquete es `create_*`. Relajar ese trigger es decision del modelo de
    amenaza (T001), no de esta migracion."""
    dsn, pg = migration_sandbox
    upgrade(dsn, CURRENT)
    business, _, _, account = await seed_scope(pg)
    routine, _ = await seed_authorization(pg, business, account)
    await make_approval(pg, routine["id"], "9" * 64, kind="package_step")

    entity = await make_entity(pg, business, account)
    creation = await make_proposal(
        pg, business, entity["entity_ref"], parameter="new_campaign:abc", classification="important"
    )
    with pytest.raises(asyncpg.RaiseError, match="exige human_approval"):
        await make_approval(pg, creation["id"], creation["diff_hash"], kind="package_step")


async def test_downgrade_never_erases_packages_or_step_signatures(migration_sandbox):
    dsn, pg = migration_sandbox
    upgrade(dsn, CURRENT)
    business, account_ref, offering, account = await seed_scope(pg)
    package = await insert_package(pg, business, account_ref, offering)

    with pytest.raises(DBAPIError, match="campaign_packages_not_empty"):
        downgrade(dsn, PREVIOUS)
    assert await pg.fetchval("SELECT version_num FROM alembic_version") == CURRENT
    assert await pg.fetchval("SELECT count(*) FROM campaign_packages") == 1

    await pg.execute("DELETE FROM campaign_packages WHERE id = $1", package)
    routine, _ = await seed_authorization(pg, business, account)
    await make_approval(pg, routine["id"], "9" * 64, kind="package_step")
    # `approvals` es solo-anexable: si ya se firmo un paso, no hay vuelta atras
    # silenciosa -- la migracion se para y lo dice.
    with pytest.raises(DBAPIError, match="approvals_package_step_history_present"):
        downgrade(dsn, PREVIOUS)
    assert await pg.fetchval("SELECT version_num FROM alembic_version") == CURRENT


async def test_existing_rows_survive_the_round_trip_byte_for_byte(migration_sandbox):
    dsn, pg = migration_sandbox
    upgrade(dsn, PREVIOUS)
    business = await make_business(pg)
    account = await make_platform_account(pg, business)
    entity = await make_entity(pg, business, account)
    proposal = await make_proposal(pg, business, entity["entity_ref"])
    await make_approval(pg, proposal["id"], proposal["diff_hash"])
    snapshot = """SELECT jsonb_build_object(
        'proposals', (SELECT jsonb_agg(to_jsonb(p) ORDER BY p.id) FROM proposals p),
        'approvals', (SELECT jsonb_agg(to_jsonb(a) ORDER BY a.id) FROM approvals a),
        'accounts', (SELECT jsonb_agg(to_jsonb(x) ORDER BY x.id) FROM platform_accounts x))::text"""
    before = await pg.fetchval(snapshot)

    upgrade(dsn, CURRENT)
    assert await pg.fetchval(snapshot) == before
    downgrade(dsn, PREVIOUS)
    assert await pg.fetchval("SELECT version_num FROM alembic_version") == PREVIOUS
    assert await pg.fetchval(snapshot) == before
    upgrade(dsn, CURRENT)
    assert await pg.fetchval(snapshot) == before
