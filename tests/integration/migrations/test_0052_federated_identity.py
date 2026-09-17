"""0052_federated_identity: identidad federada, origen de la sesion y el
salto de ida y vuelta al proveedor (spec 002b, data-model.md §Migration plan).

Tres bloques, el patron de `test_0035_mcp_oauth.py`:

1. Ida y vuelta sobre una base propia (head -> 0051 -> head): un downgrade
   que no se corre es un downgrade que no existe.
2. Los invariantes que la BD tiene que aplicar por si sola -- cada CHECK
   rechaza exactamente lo que dice rechazar, sobre la base compartida ya
   migrada.
3. El arreglo de integridad de `sessions.owner_id` (paso 4): que la clave
   ajena se localiza POR SU COLUMNA y no por su nombre (dos bases, una con
   el nombre por defecto de Postgres y otra renombrada a mano), que borrar
   un dueno FALLA en 0051 y FUNCIONA en 0052, y que la cascada llega hasta
   las transacciones federadas.

Nada de esto importa `application` ni `infrastructure` (guardia de
`test_chain_isolation.py`): las filas entran por SQL crudo."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
from testcontainers.community.postgres import PostgresContainer

from tests.integration.migrations.conftest import (
    downgrade,
    to_alembic_dsn,
    to_asyncpg_dsn,
    upgrade,
    with_database,
)

pytestmark = pytest.mark.integration

_ROUND_TRIP_DB = "ads_federated_identity_round_trip"
_PREVIOUS_REVISION = "0051_merge_oauth_lane003"
_TABLES = {"owner_federated_identities", "federated_login_transactions"}

_GOOGLE_ISSUER = "https://accounts.google.com"

# Nombre por defecto de Postgres, verificado en la BD desplegada de la VM
# (T001). La migracion no lo supone: lo detecta. Este test cubre las dos
# ramas de esa deteccion.
_DEFAULT_SESSIONS_OWNER_FK = "sessions_owner_id_fkey"
_RENAMED_SESSIONS_OWNER_FK = "sessions_owner_legacy_fk"

_INSERT_IDENTITY = """
    INSERT INTO owner_federated_identities (owner_id, issuer, subject, email_at_binding,
                                            bound_at, last_seen_at)
    VALUES ($1, $2, $3, $4, $5, $6)
"""

_INSERT_SESSION = """
    INSERT INTO sessions (id, owner_id, token_hash, expires_at, origin,
                          last_federated_auth_at)
    VALUES ($1, $2, $3, now() + interval '30 minutes', $4, $5)
"""

_INSERT_TRANSACTION = """
    INSERT INTO federated_login_transactions (state_hash, nonce_hash, purpose, session_id,
                                              txn_id, created_at, expires_at, consumed_at)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
"""

_FIND_SESSIONS_OWNER_FK = """
    SELECT c.conname, c.confdeltype::text AS delete_action
      FROM pg_constraint c
     WHERE c.conrelid = 'sessions'::regclass
       AND c.contype = 'f'
       AND c.confrelid = 'owners'::regclass
       AND c.conkey = ARRAY[(SELECT a.attnum
                               FROM pg_attribute a
                              WHERE a.attrelid = 'sessions'::regclass
                                AND a.attname = 'owner_id'
                                AND NOT a.attisdropped)]
"""


def _sha256_hex(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _fresh_hash() -> str:
    return _sha256_hex(uuid.uuid4().hex)


def _code_challenge() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")


async def _make_owner(pg: asyncpg.Connection) -> uuid.UUID:
    owner_id = uuid.uuid4()
    await pg.execute(
        "INSERT INTO owners (id, email, password_hash) "
        "VALUES ($1, $2, 'argon2id$fixture$not-a-real-hash')",
        owner_id,
        f"owner-{owner_id.hex[:8]}@safent.example",
    )
    return owner_id


async def _make_session(
    pg: asyncpg.Connection,
    owner_id: uuid.UUID,
    *,
    origin: str = "password",
    federated_auth_at: datetime | None = None,
) -> uuid.UUID:
    session_id = uuid.uuid4()
    await pg.execute(
        _INSERT_SESSION, session_id, owner_id, _fresh_hash(), origin, federated_auth_at
    )
    return session_id


async def _make_identity(
    pg: asyncpg.Connection,
    owner_id: uuid.UUID,
    *,
    issuer: str = _GOOGLE_ISSUER,
    subject: str | None = None,
    email: str = "dueno@example.com",
    bound_at: datetime | None = None,
    last_seen_at: datetime | None = None,
) -> str:
    resolved_subject = subject if subject is not None else uuid.uuid4().hex
    anchor = bound_at or datetime.now(UTC)
    await pg.execute(
        _INSERT_IDENTITY,
        owner_id,
        issuer,
        resolved_subject,
        email,
        anchor,
        last_seen_at if last_seen_at is not None else anchor,
    )
    return resolved_subject


async def _make_authorization_request(
    pg: asyncpg.Connection, *, consented_by: uuid.UUID | None = None
) -> uuid.UUID:
    """Transaccion de consentimiento a la que volver tras el salto.

    Sin `consented_by` queda PENDING, que es el estado REAL mientras el
    dueno da la vuelta por Google: `owner_id` es nulo porque todavia no ha
    consentido (CHECK `(consented_at IS NULL) = (owner_id IS NULL)` de
    0035). Con `consented_by` queda CONSENTED y colgada del dueno.
    """
    client_id = str(uuid.uuid4())
    await pg.execute(
        """
        INSERT INTO oauth_clients (client_id, client_name, redirect_uris,
                                   token_endpoint_auth_method, grant_types,
                                   requested_scopes, created_at)
        VALUES ($1, 'Claude Code', '["http://127.0.0.1:54321/callback"]'::jsonb, 'none',
                '["authorization_code","refresh_token"]'::jsonb, 'ads:read', now())
        """,
        client_id,
    )
    txn_id = uuid.uuid4()
    await pg.execute(
        """
        INSERT INTO oauth_authorization_requests (txn_id, client_id, owner_id, redirect_uri,
                                                  redirect_uri_explicit, code_challenge,
                                                  scopes, resource, code_hash, state,
                                                  created_at, expires_at, consented_at)
        VALUES ($1, $2, $3, 'http://127.0.0.1:54321/callback', true, $4, 'ads:read',
                'https://ads.example.com/mcp', $5, $6,
                now(), now() + interval '10 minutes', $7)
        """,
        txn_id,
        client_id,
        consented_by,
        _code_challenge(),
        _fresh_hash() if consented_by is not None else None,
        "CONSENTED" if consented_by is not None else "PENDING",
        datetime.now(UTC) if consented_by is not None else None,
    )
    return txn_id


async def _make_transaction(
    pg: asyncpg.Connection,
    *,
    purpose: str = "login",
    session_id: uuid.UUID | None = None,
    txn_id: uuid.UUID | None = None,
    state_hash: str | None = None,
    nonce_hash: str | None = None,
    created_at: datetime | None = None,
    expires_at: datetime | None = None,
    consumed_at: datetime | None = None,
) -> str:
    created = created_at or datetime.now(UTC)
    resolved_state = state_hash if state_hash is not None else _fresh_hash()
    await pg.execute(
        _INSERT_TRANSACTION,
        resolved_state,
        nonce_hash if nonce_hash is not None else _fresh_hash(),
        purpose,
        session_id,
        txn_id,
        created,
        expires_at if expires_at is not None else created + timedelta(minutes=10),
        consumed_at,
    )
    return resolved_state


@pytest.fixture(autouse=True)
async def _leave_no_oauth_clients_behind(pg: asyncpg.Connection) -> AsyncIterator[None]:
    """`tests/integration/mcp_oauth/test_sql_repositories.py` cuenta clientes
    sin consentimiento sobre TODA la base compartida, asi que cada fila que
    este modulo deja atras se convierte en un fallo de otro modulo cuando las
    dos suites corren juntas. Se borran solo los clientes creados aqui (el
    `DELETE` arrastra sus solicitudes y, con ellas, las transacciones
    federadas que colgaban de ellas)."""
    before = [row["client_id"] for row in await pg.fetch("SELECT client_id FROM oauth_clients")]
    yield
    await pg.execute("DELETE FROM oauth_clients WHERE client_id <> ALL($1::text[])", before)


async def _tables(dsn: str) -> set[str]:
    connection = await asyncpg.connect(dsn)
    try:
        rows = await connection.fetch(
            """
            SELECT c.relname AS name
              FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = 'public' AND c.relkind = 'r'
            """
        )
        return {row["name"] for row in rows}
    finally:
        await connection.close()


async def _session_columns(dsn: str) -> set[str]:
    connection = await asyncpg.connect(dsn)
    try:
        rows = await connection.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'sessions'"
        )
        return {row["column_name"] for row in rows}
    finally:
        await connection.close()


async def _fresh_database(container: PostgresContainer, name: str) -> tuple[str, str]:
    """Base vacia propia. Devuelve `(dsn de alembic, dsn de asyncpg)`."""
    admin_dsn = to_asyncpg_dsn(container.get_connection_url())
    admin = await asyncpg.connect(admin_dsn)
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
        await admin.execute(f'CREATE DATABASE "{name}"')
    finally:
        await admin.close()
    return (
        with_database(to_alembic_dsn(container.get_connection_url()), name),
        with_database(admin_dsn, name),
    )


# --- 1. ida y vuelta -------------------------------------------------------


async def test_upgrade_downgrade_upgrade(postgres_container: PostgresContainer) -> None:
    alembic_dsn, query_dsn = await _fresh_database(postgres_container, _ROUND_TRIP_DB)

    await asyncio.to_thread(upgrade, alembic_dsn)
    assert _TABLES <= await _tables(query_dsn)
    assert {"origin", "last_federated_auth_at"} <= await _session_columns(query_dsn)

    await asyncio.to_thread(downgrade, alembic_dsn, _PREVIOUS_REVISION)
    after_downgrade = await _tables(query_dsn)
    assert after_downgrade & _TABLES == set()
    assert "sessions" in after_downgrade
    assert {"origin", "last_federated_auth_at"} & await _session_columns(query_dsn) == set()

    await asyncio.to_thread(upgrade, alembic_dsn)
    assert _TABLES <= await _tables(query_dsn)
    assert {"origin", "last_federated_auth_at"} <= await _session_columns(query_dsn)


# --- 2. invariantes de `owner_federated_identities` ------------------------


async def test_only_google_is_an_accepted_issuer(pg: asyncpg.Connection) -> None:
    owner_id = await _make_owner(pg)

    with pytest.raises(
        asyncpg.CheckViolationError, match="owner_federated_identities_issuer_check"
    ):
        await _make_identity(pg, owner_id, issuer="https://login.microsoftonline.com")


async def test_a_subject_is_never_empty_nor_longer_than_255(pg: asyncpg.Connection) -> None:
    owner_id = await _make_owner(pg)
    constraint = "owner_federated_identities_subject_check"

    with pytest.raises(asyncpg.CheckViolationError, match=constraint):
        await _make_identity(pg, owner_id, subject="")

    with pytest.raises(asyncpg.CheckViolationError, match=constraint):
        await _make_identity(pg, owner_id, subject="9" * 256)


async def test_an_email_at_binding_has_a_plausible_length(pg: asyncpg.Connection) -> None:
    owner_id = await _make_owner(pg)
    constraint = "owner_federated_identities_email_check"

    with pytest.raises(asyncpg.CheckViolationError, match=constraint):
        await _make_identity(pg, owner_id, email="a")

    with pytest.raises(asyncpg.CheckViolationError, match=constraint):
        await _make_identity(pg, owner_id, email=f"{'a' * 245}@example.com")


async def test_last_seen_never_precedes_the_binding(pg: asyncpg.Connection) -> None:
    owner_id = await _make_owner(pg)
    bound_at = datetime.now(UTC)

    with pytest.raises(asyncpg.CheckViolationError, match="owner_federated_identities_seen_check"):
        await _make_identity(
            pg, owner_id, bound_at=bound_at, last_seen_at=bound_at - timedelta(seconds=1)
        )


async def test_a_google_subject_belongs_to_exactly_one_owner(pg: asyncpg.Connection) -> None:
    """PK `(issuer, subject)`: confusion de identidad descartada por la BD,
    no por el caso de uso."""
    subject = uuid.uuid4().hex
    await _make_identity(pg, await _make_owner(pg), subject=subject)

    with pytest.raises(asyncpg.UniqueViolationError, match="owner_federated_identities_pkey"):
        await _make_identity(pg, await _make_owner(pg), subject=subject)


async def test_an_owner_is_never_bound_to_two_google_accounts(pg: asyncpg.Connection) -> None:
    """UNIQUE `(owner_id, issuer)`: re-vincular exige intervencion
    administrativa (edge case «cuenta recreada»)."""
    owner_id = await _make_owner(pg)
    await _make_identity(pg, owner_id)

    with pytest.raises(
        asyncpg.UniqueViolationError, match="owner_federated_identities_one_per_owner"
    ):
        await _make_identity(pg, owner_id)


# --- 2b. invariantes de `sessions` ----------------------------------------


async def test_a_session_origin_belongs_to_the_closed_vocabulary(pg: asyncpg.Connection) -> None:
    owner_id = await _make_owner(pg)

    with pytest.raises(asyncpg.CheckViolationError, match="sessions_origin_check"):
        await _make_session(pg, owner_id, origin="google")


async def test_a_federated_session_cannot_exist_without_its_freshness_mark(
    pg: asyncpg.Connection,
) -> None:
    owner_id = await _make_owner(pg)

    with pytest.raises(asyncpg.CheckViolationError, match="sessions_federated_origin_check"):
        await _make_session(pg, owner_id, origin="federated", federated_auth_at=None)

    await _make_session(pg, owner_id, origin="federated", federated_auth_at=datetime.now(UTC))


async def test_a_session_defaults_to_the_least_capable_origin(pg: asyncpg.Connection) -> None:
    """El DEFAULT es fail-closed: un escritor con un bug deja al dueno
    fuera, no dentro."""
    owner_id = await _make_owner(pg)
    session_id = uuid.uuid4()
    await pg.execute(
        "INSERT INTO sessions (id, owner_id, token_hash, expires_at) "
        "VALUES ($1, $2, $3, now() + interval '30 minutes')",
        session_id,
        owner_id,
        _fresh_hash(),
    )

    row = await pg.fetchrow(
        "SELECT origin, last_federated_auth_at FROM sessions WHERE id = $1", session_id
    )
    assert row["origin"] == "password"
    assert row["last_federated_auth_at"] is None


# --- 2c. invariantes de `federated_login_transactions` --------------------


async def test_state_and_nonce_are_fingerprints_and_not_the_values(
    pg: asyncpg.Connection,
) -> None:
    with pytest.raises(
        asyncpg.CheckViolationError, match="federated_login_transactions_state_hash_check"
    ):
        await _make_transaction(pg, state_hash="un-state-en-claro")

    with pytest.raises(
        asyncpg.CheckViolationError, match="federated_login_transactions_nonce_hash_check"
    ):
        await _make_transaction(pg, nonce_hash=_fresh_hash().upper())


async def test_a_purpose_belongs_to_the_closed_vocabulary(pg: asyncpg.Connection) -> None:
    with pytest.raises(
        asyncpg.CheckViolationError, match="federated_login_transactions_purpose_check"
    ):
        await _make_transaction(pg, purpose="enroll")


async def test_a_transaction_always_has_a_window(pg: asyncpg.Connection) -> None:
    created = datetime.now(UTC)

    with pytest.raises(asyncpg.CheckViolationError, match="federated_login_transactions_ttl_check"):
        await _make_transaction(pg, created_at=created, expires_at=created)


async def test_a_session_is_bound_if_and_only_if_the_purpose_is_reidentify(
    pg: asyncpg.Connection,
) -> None:
    """Una vuelta de Google no puede marcar frescura en una sesion que no
    inicio el salto (research.md Decision C)."""
    constraint = "federated_login_transactions_session_iff_reidentify_check"
    owner_id = await _make_owner(pg)
    session_id = await _make_session(pg, owner_id)

    with pytest.raises(asyncpg.CheckViolationError, match=constraint):
        await _make_transaction(pg, purpose="login", session_id=session_id)

    with pytest.raises(asyncpg.CheckViolationError, match=constraint):
        await _make_transaction(pg, purpose="reidentify", session_id=None)

    await _make_transaction(pg, purpose="reidentify", session_id=session_id)
    await _make_transaction(pg, purpose="login", session_id=None)


async def test_consuming_a_transaction_is_a_single_atomic_transition(
    pg: asyncpg.Connection,
) -> None:
    """El UPDATE condicional del repositorio: desconocida, caducada o ya
    consumida devuelven cero filas (FR-115)."""
    now = datetime.now(UTC)
    state_hash = await _make_transaction(pg)

    consume = """
        UPDATE federated_login_transactions
           SET consumed_at = $2
         WHERE state_hash = $1 AND consumed_at IS NULL AND expires_at > $2
        RETURNING state_hash
    """
    assert await pg.fetchval(consume, state_hash, now) is not None
    assert await pg.fetchval(consume, state_hash, now) is None
    assert await pg.fetchval(consume, _fresh_hash(), now) is None

    expired_state = await _make_transaction(
        pg, created_at=now - timedelta(minutes=30), expires_at=now - timedelta(minutes=20)
    )
    assert await pg.fetchval(consume, expired_state, now) is None


async def test_the_live_index_serves_the_janitor_sweep(pg: asyncpg.Connection) -> None:
    """`ix_federated_login_transactions_live` existe y es parcial sobre las
    filas sin consumir: es el unico indice que esta tabla justifica."""
    definition = await pg.fetchval(
        "SELECT indexdef FROM pg_indexes WHERE indexname = $1",
        "ix_federated_login_transactions_live",
    )
    assert definition is not None
    assert "expires_at" in definition
    assert "consumed_at IS NULL" in definition


# --- 3. el arreglo de integridad de `sessions.owner_id` -------------------


async def test_deleting_an_owner_drags_sessions_transactions_and_identities(
    pg: asyncpg.Connection,
) -> None:
    """NFR-107 completo: hoy (0051) este DELETE falla por clave ajena."""
    owner_id = await _make_owner(pg)
    session_id = await _make_session(
        pg, owner_id, origin="federated", federated_auth_at=datetime.now(UTC)
    )
    await _make_identity(pg, owner_id)
    reidentify_state = await _make_transaction(
        pg,
        purpose="reidentify",
        session_id=session_id,
        txn_id=await _make_authorization_request(pg),
    )
    login_state = await _make_transaction(
        pg, txn_id=await _make_authorization_request(pg, consented_by=owner_id)
    )

    await pg.execute("DELETE FROM owners WHERE id = $1", owner_id)

    assert await pg.fetchval("SELECT count(*) FROM sessions WHERE owner_id = $1", owner_id) == 0
    assert (
        await pg.fetchval(
            "SELECT count(*) FROM owner_federated_identities WHERE owner_id = $1", owner_id
        )
        == 0
    )
    assert (
        await pg.fetchval(
            "SELECT count(*) FROM federated_login_transactions WHERE state_hash = $1",
            reidentify_state,
        )
        == 0
    )
    # La transaccion de `login` no cuelga del dueno por `sessions` (no hay
    # sesion todavia), sino por `oauth_authorization_requests`, que tambien
    # cae en cascada una vez consentida.
    assert (
        await pg.fetchval(
            "SELECT count(*) FROM federated_login_transactions WHERE state_hash = $1", login_state
        )
        == 0
    )


async def test_a_login_jump_over_a_pending_request_survives_the_owner_deletion(
    pg: asyncpg.Connection,
) -> None:
    """Limite declarado de la cascada, no un olvido: mientras la solicitud
    de consentimiento sigue PENDING su `owner_id` es NULO (CHECK de 0035),
    asi que no hay arista por la que el borrado del dueno llegue a esa fila.

    No incumple NFR-107: la fila no guarda ni correo ni identificador del
    proveedor -- solo dos huellas sha256, un proposito y dos instantes -- y
    el janitor existente la barre a los 10 minutos por `expires_at`."""
    owner_id = await _make_owner(pg)
    pending_txn = await _make_authorization_request(pg)
    orphan_state = await _make_transaction(pg, txn_id=pending_txn)

    await pg.execute("DELETE FROM owners WHERE id = $1", owner_id)

    row = await pg.fetchrow(
        "SELECT session_id, purpose FROM federated_login_transactions WHERE state_hash = $1",
        orphan_state,
    )
    assert row is not None
    assert row["session_id"] is None
    assert row["purpose"] == "login"


async def test_deleting_a_session_drags_only_its_own_federated_transactions(
    pg: asyncpg.Connection,
) -> None:
    owner_id = await _make_owner(pg)
    doomed = await _make_session(pg, owner_id)
    survivor = await _make_session(pg, owner_id)
    doomed_state = await _make_transaction(pg, purpose="reidentify", session_id=doomed)
    survivor_state = await _make_transaction(pg, purpose="reidentify", session_id=survivor)

    await pg.execute("DELETE FROM sessions WHERE id = $1", doomed)

    remaining = await pg.fetch(
        "SELECT state_hash FROM federated_login_transactions WHERE state_hash = ANY($1::text[])",
        [doomed_state, survivor_state],
    )
    assert [row["state_hash"] for row in remaining] == [survivor_state]


@pytest.mark.parametrize(
    ("database", "rename_to"),
    [
        ("ads_federated_fk_default_name", None),
        ("ads_federated_fk_renamed", _RENAMED_SESSIONS_OWNER_FK),
    ],
)
async def test_the_owner_foreign_key_is_found_by_its_column_not_by_its_name(
    postgres_container: PostgresContainer, database: str, rename_to: str | None
) -> None:
    """0052 detecta la clave ajena en `pg_constraint` por la columna que
    cubre. Con el nombre por defecto de Postgres (el de la VM, T001) y con
    uno cualquiera: en los dos casos gana `ON DELETE CASCADE` y CONSERVA su
    nombre, asi que el `downgrade` es simetrico."""
    alembic_dsn, query_dsn = await _fresh_database(postgres_container, database)
    await asyncio.to_thread(upgrade, alembic_dsn, _PREVIOUS_REVISION)

    connection = await asyncpg.connect(query_dsn)
    try:
        before = await connection.fetchrow(_FIND_SESSIONS_OWNER_FK)
        assert before["conname"] == _DEFAULT_SESSIONS_OWNER_FK
        assert before["delete_action"] == "a"  # NO ACTION: borrar un dueno falla hoy
        if rename_to is not None:
            await connection.execute(
                f'ALTER TABLE sessions RENAME CONSTRAINT "{before["conname"]}" TO "{rename_to}"'
            )

        expected_name = rename_to or _DEFAULT_SESSIONS_OWNER_FK
        owner_id = await _make_owner(connection)
        # Insercion literal de 0001_bootstrap: en 0051 `sessions` todavia no
        # tiene `origin` ni `last_federated_auth_at`.
        session_id = uuid.uuid4()
        await connection.execute(
            "INSERT INTO sessions (id, owner_id, token_hash, expires_at) "
            "VALUES ($1, $2, $3, now() + interval '30 minutes')",
            session_id,
            owner_id,
            _fresh_hash(),
        )
        with pytest.raises(asyncpg.ForeignKeyViolationError, match=expected_name):
            await connection.execute("DELETE FROM owners WHERE id = $1", owner_id)

        await asyncio.to_thread(upgrade, alembic_dsn)

        after = await connection.fetchrow(_FIND_SESSIONS_OWNER_FK)
        assert after["conname"] == expected_name
        assert after["delete_action"] == "c"  # CASCADE
        await connection.execute("DELETE FROM owners WHERE id = $1", owner_id)
        assert (
            await connection.fetchval("SELECT count(*) FROM sessions WHERE id = $1", session_id)
            == 0
        )

        await asyncio.to_thread(downgrade, alembic_dsn, _PREVIOUS_REVISION)
        restored = await connection.fetchrow(_FIND_SESSIONS_OWNER_FK)
        assert restored["conname"] == expected_name
        assert restored["delete_action"] == "a"
    finally:
        await connection.close()
