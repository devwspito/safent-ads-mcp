"""0035_mcp_oauth: las cuatro tablas del acceso delegado al MCP.

Ida y vuelta sobre una base propia (head -> 0034 -> head: un downgrade que
no se corre es un downgrade que no existe) y, sobre la base compartida ya
migrada, los invariantes que la BD tiene que aplicar por si sola --
threat-model.md C-38 (codigo de un solo uso), C-43 (rotacion de refresh),
C-44 (solo hashes) y la coherencia consentimiento/propietario.

El fixture `pg` no envuelve nada en una transaccion que se deshaga: cada
test crea sus propias filas con identificadores frescos y no reutiliza
constantes entre tests (mismo motivo que `_fresh_chat_id` en
test_telegram_pairing.py)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
import uuid
from datetime import UTC, datetime

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

_ROUND_TRIP_DB = "ads_mcp_oauth_round_trip"
_PREVIOUS_REVISION = "0034_execution_reservations"
_TABLES = {
    "oauth_clients",
    "oauth_authorization_requests",
    "oauth_grants",
    "oauth_tokens",
}

_INSERT_CLIENT = """
    INSERT INTO oauth_clients (client_id, client_name, redirect_uris,
                               token_endpoint_auth_method, client_secret_hash,
                               grant_types, requested_scopes, created_at)
    VALUES ($1, 'Claude Code', '["http://127.0.0.1:54321/callback"]'::jsonb, $2, $3,
            '["authorization_code","refresh_token"]'::jsonb, 'ads:read ads:propose', now())
"""

_INSERT_REQUEST = """
    INSERT INTO oauth_authorization_requests (txn_id, client_id, owner_id, redirect_uri,
                                              redirect_uri_explicit, code_challenge,
                                              scopes, resource, code_hash, state,
                                              created_at, expires_at, consented_at, redeemed_at)
    VALUES ($1, $2, $3, 'http://127.0.0.1:54321/callback', true, $4,
            'ads:read', 'https://ads.example.com/mcp', $5, $6,
            now(), now() + interval '10 minutes', $7, $8)
"""

_INSERT_GRANT = """
    INSERT INTO oauth_grants (grant_id, owner_id, client_id, authorization_txn_id,
                              scopes, resource, created_at)
    VALUES ($1, $2, $3, $4, 'ads:read', 'https://ads.example.com/mcp', now())
"""

_INSERT_ISSUED_CREDENTIAL = """
    INSERT INTO oauth_tokens (token_hash, grant_id, kind, state, issued_at, expires_at,
                              rotated_from)
    VALUES ($1, $2, $3, $4, now(), now() + interval '60 minutes', $5)
"""


def _sha256_hex(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _fresh_hash() -> str:
    """Hash de un token que nunca existio: cada fila necesita el suyo porque
    toda columna de hash de este esquema es UNIQUE."""
    return _sha256_hex(uuid.uuid4().hex)


def _code_challenge() -> str:
    """PKCE S256: 43 caracteres base64url sin relleno (RFC 7636 §4.2)."""
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


async def _make_client(
    pg: asyncpg.Connection,
    *,
    auth_method: str = "none",
    secret_hash: str | None = None,
) -> str:
    client_id = str(uuid.uuid4())
    await pg.execute(_INSERT_CLIENT, client_id, auth_method, secret_hash)
    return client_id


async def _make_request(
    pg: asyncpg.Connection,
    client_id: str,
    *,
    owner_id: uuid.UUID | None = None,
    code_hash: str | None = None,
    state: str = "PENDING",
    consented: bool = False,
    redeemed: bool = False,
) -> uuid.UUID:
    txn_id = uuid.uuid4()
    now = datetime.now(UTC)
    await pg.execute(
        _INSERT_REQUEST,
        txn_id,
        client_id,
        owner_id,
        _code_challenge(),
        code_hash,
        state,
        now if consented else None,
        now if redeemed else None,
    )
    return txn_id


async def _make_grant(pg: asyncpg.Connection) -> uuid.UUID:
    """Concesion suelta (sin enlace al canje que la creo): a estos tests les
    basta el agregado Grant, no de donde nacio."""
    owner_id = await _make_owner(pg)
    client_id = await _make_client(pg)
    grant_id = uuid.uuid4()
    await pg.execute(_INSERT_GRANT, grant_id, owner_id, client_id, None)
    return grant_id


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


async def test_upgrade_downgrade_upgrade(postgres_container: PostgresContainer) -> None:
    admin_dsn = to_asyncpg_dsn(postgres_container.get_connection_url())
    admin = await asyncpg.connect(admin_dsn)
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{_ROUND_TRIP_DB}"')
        await admin.execute(f'CREATE DATABASE "{_ROUND_TRIP_DB}"')
    finally:
        await admin.close()

    alembic_dsn = with_database(
        to_alembic_dsn(postgres_container.get_connection_url()), _ROUND_TRIP_DB
    )
    query_dsn = with_database(admin_dsn, _ROUND_TRIP_DB)

    await asyncio.to_thread(upgrade, alembic_dsn)
    assert _TABLES <= await _tables(query_dsn)

    await asyncio.to_thread(downgrade, alembic_dsn, _PREVIOUS_REVISION)
    after_downgrade = await _tables(query_dsn)
    assert after_downgrade & _TABLES == set()
    assert "execution_reservations" in after_downgrade

    await asyncio.to_thread(upgrade, alembic_dsn)
    assert _TABLES <= await _tables(query_dsn)


async def test_a_public_client_cannot_carry_a_secret(pg: asyncpg.Connection) -> None:
    with pytest.raises(
        asyncpg.CheckViolationError, match="oauth_clients_public_has_no_secret_check"
    ):
        await _make_client(pg, auth_method="none", secret_hash=_fresh_hash())


async def test_a_confidential_client_must_carry_a_secret(pg: asyncpg.Connection) -> None:
    with pytest.raises(
        asyncpg.CheckViolationError, match="oauth_clients_public_has_no_secret_check"
    ):
        await _make_client(pg, auth_method="client_secret_post", secret_hash=None)

    await _make_client(pg, auth_method="client_secret_post", secret_hash=_fresh_hash())


async def test_a_client_secret_never_lands_in_clear(pg: asyncpg.Connection) -> None:
    with pytest.raises(asyncpg.CheckViolationError, match="oauth_clients_client_secret_hash_check"):
        await _make_client(pg, auth_method="client_secret_basic", secret_hash="s3cret-en-claro")


async def test_a_code_hash_cannot_be_reused_by_two_requests(pg: asyncpg.Connection) -> None:
    client_id = await _make_client(pg)
    owner_id = await _make_owner(pg)
    code_hash = _fresh_hash()
    await _make_request(
        pg, client_id, owner_id=owner_id, code_hash=code_hash, state="CONSENTED", consented=True
    )

    with pytest.raises(
        asyncpg.UniqueViolationError, match="ix_oauth_authorization_requests_code_hash"
    ):
        await _make_request(
            pg,
            client_id,
            owner_id=await _make_owner(pg),
            code_hash=code_hash,
            state="CONSENTED",
            consented=True,
        )


async def test_a_consent_timestamp_requires_an_owner(pg: asyncpg.Connection) -> None:
    client_id = await _make_client(pg)

    with pytest.raises(
        asyncpg.CheckViolationError,
        match="oauth_authorization_requests_owner_iff_consented_check",
    ):
        await _make_request(
            pg, client_id, owner_id=None, code_hash=_fresh_hash(), state="CONSENTED", consented=True
        )


async def test_an_owner_cannot_appear_without_consenting(pg: asyncpg.Connection) -> None:
    client_id = await _make_client(pg)

    with pytest.raises(
        asyncpg.CheckViolationError,
        match="oauth_authorization_requests_owner_iff_consented_check",
    ):
        await _make_request(pg, client_id, owner_id=await _make_owner(pg), state="PENDING")


async def test_a_live_code_exists_only_while_consented(pg: asyncpg.Connection) -> None:
    client_id = await _make_client(pg)
    owner_id = await _make_owner(pg)
    constraint = "oauth_authorization_requests_code_only_when_consented_check"

    with pytest.raises(asyncpg.CheckViolationError, match=constraint):
        await _make_request(
            pg, client_id, owner_id=owner_id, code_hash=None, state="CONSENTED", consented=True
        )

    with pytest.raises(asyncpg.CheckViolationError, match=constraint):
        await _make_request(pg, client_id, code_hash=_fresh_hash(), state="PENDING")


async def test_expiring_a_consented_request_burns_its_code(pg: asyncpg.Connection) -> None:
    """La bicondicional obliga al barrido de caducidad (C-58) a borrar el
    hash al pasar a EXPIRED: un codigo caducado deja de existir, no se queda
    canjeable en la fila."""
    client_id = await _make_client(pg)
    owner_id = await _make_owner(pg)
    txn_id = await _make_request(
        pg,
        client_id,
        owner_id=owner_id,
        code_hash=_fresh_hash(),
        state="CONSENTED",
        consented=True,
    )

    with pytest.raises(
        asyncpg.CheckViolationError,
        match="oauth_authorization_requests_code_only_when_consented_check",
    ):
        await pg.execute(
            "UPDATE oauth_authorization_requests SET state = 'EXPIRED' WHERE txn_id = $1", txn_id
        )

    await pg.execute(
        "UPDATE oauth_authorization_requests SET state = 'EXPIRED', code_hash = NULL "
        "WHERE txn_id = $1",
        txn_id,
    )


async def test_redeeming_a_code_is_a_single_atomic_transition(pg: asyncpg.Connection) -> None:
    client_id = await _make_client(pg)
    owner_id = await _make_owner(pg)
    code_hash = _fresh_hash()
    await _make_request(
        pg, client_id, owner_id=owner_id, code_hash=code_hash, state="CONSENTED", consented=True
    )

    redeem = """
        UPDATE oauth_authorization_requests
           SET state = 'REDEEMED', redeemed_at = now()
         WHERE code_hash = $1 AND state = 'CONSENTED'
        RETURNING txn_id
    """
    assert await pg.fetchval(redeem, code_hash) is not None
    assert await pg.fetchval(redeem, code_hash) is None


async def test_a_code_hash_is_a_hash_and_not_the_code(pg: asyncpg.Connection) -> None:
    client_id = await _make_client(pg)
    owner_id = await _make_owner(pg)

    with pytest.raises(
        asyncpg.CheckViolationError, match="oauth_authorization_requests_code_hash_check"
    ):
        await _make_request(
            pg,
            client_id,
            owner_id=owner_id,
            code_hash=base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("="),
            state="CONSENTED",
            consented=True,
        )


async def test_the_state_vocabulary_is_closed(pg: asyncpg.Connection) -> None:
    client_id = await _make_client(pg)

    with pytest.raises(
        asyncpg.CheckViolationError, match="oauth_authorization_requests_state_check"
    ):
        await _make_request(pg, client_id, state="BOGUS")


async def test_only_known_scopes_are_stored(pg: asyncpg.Connection) -> None:
    client_id = await _make_client(pg)
    txn_id = uuid.uuid4()

    with pytest.raises(
        asyncpg.CheckViolationError, match="oauth_authorization_requests_scopes_check"
    ):
        await pg.execute(
            """
            INSERT INTO oauth_authorization_requests (txn_id, client_id, redirect_uri,
                                                      redirect_uri_explicit, code_challenge,
                                                      scopes, resource, created_at, expires_at)
            VALUES ($1, $2, 'http://127.0.0.1:54321/callback', true, $3,
                    'ads:read ads:write', 'https://ads.example.com/mcp',
                    now(), now() + interval '10 minutes')
            """,
            txn_id,
            client_id,
            _code_challenge(),
        )


async def test_a_grant_keeps_at_most_one_active_token_per_kind(pg: asyncpg.Connection) -> None:
    grant_id = await _make_grant(pg)
    first = _fresh_hash()
    await pg.execute(_INSERT_ISSUED_CREDENTIAL, first, grant_id, "refresh", "ACTIVE", None)

    with pytest.raises(asyncpg.UniqueViolationError, match="ix_oauth_tokens_grant_active"):
        await pg.execute(
            _INSERT_ISSUED_CREDENTIAL, _fresh_hash(), grant_id, "refresh", "ACTIVE", None
        )

    # La rotacion correcta: el anterior queda ROTATED (nunca borrado, hace
    # falta para detectar el reuso) y el nuevo entra ACTIVE.
    await pg.execute("UPDATE oauth_tokens SET state = 'ROTATED' WHERE token_hash = $1", first)
    await pg.execute(_INSERT_ISSUED_CREDENTIAL, _fresh_hash(), grant_id, "refresh", "ACTIVE", first)


async def test_a_rotated_token_has_at_most_one_successor(pg: asyncpg.Connection) -> None:
    grant_id = await _make_grant(pg)
    rotated = _fresh_hash()
    await pg.execute(_INSERT_ISSUED_CREDENTIAL, rotated, grant_id, "refresh", "ROTATED", None)
    await pg.execute(
        _INSERT_ISSUED_CREDENTIAL, _fresh_hash(), grant_id, "refresh", "ACTIVE", rotated
    )

    with pytest.raises(asyncpg.UniqueViolationError, match="ix_oauth_tokens_rotated_from"):
        await pg.execute(
            _INSERT_ISSUED_CREDENTIAL, _fresh_hash(), grant_id, "refresh", "REVOKED", rotated
        )


async def test_a_token_is_stored_hashed_and_typed(pg: asyncpg.Connection) -> None:
    grant_id = await _make_grant(pg)

    with pytest.raises(asyncpg.CheckViolationError, match="oauth_tokens_token_hash_check"):
        await pg.execute(
            _INSERT_ISSUED_CREDENTIAL,
            base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("="),
            grant_id,
            "access",
            "ACTIVE",
            None,
        )

    with pytest.raises(asyncpg.CheckViolationError, match="oauth_tokens_kind_check"):
        await pg.execute(
            _INSERT_ISSUED_CREDENTIAL, _fresh_hash(), grant_id, "ACCESS", "ACTIVE", None
        )

    with pytest.raises(asyncpg.CheckViolationError, match="oauth_tokens_state_check"):
        await pg.execute(
            _INSERT_ISSUED_CREDENTIAL, _fresh_hash(), grant_id, "access", "EXPIRED", None
        )


async def test_tokens_die_with_their_grant(pg: asyncpg.Connection) -> None:
    grant_id = await _make_grant(pg)
    await pg.execute(_INSERT_ISSUED_CREDENTIAL, _fresh_hash(), grant_id, "access", "ACTIVE", None)
    await pg.execute(_INSERT_ISSUED_CREDENTIAL, _fresh_hash(), grant_id, "refresh", "ACTIVE", None)

    await pg.execute("DELETE FROM oauth_grants WHERE grant_id = $1", grant_id)

    assert await pg.fetchval("SELECT count(*) FROM oauth_tokens WHERE grant_id = $1", grant_id) == 0


async def test_a_grant_survives_the_pruning_of_its_authorization_request(
    pg: asyncpg.Connection,
) -> None:
    """Retencion: las solicitudes terminales se borran a los 7 dias y la
    concesion que nacio de una de ellas tiene que seguir viva (ON DELETE SET
    NULL), solo pierde el enlace."""
    client_id = await _make_client(pg)
    owner_id = await _make_owner(pg)
    txn_id = await _make_request(
        pg,
        client_id,
        owner_id=owner_id,
        code_hash=_fresh_hash(),
        state="REDEEMED",
        consented=True,
        redeemed=True,
    )
    grant_id = uuid.uuid4()
    await pg.execute(_INSERT_GRANT, grant_id, owner_id, client_id, txn_id)

    await pg.execute("DELETE FROM oauth_authorization_requests WHERE txn_id = $1", txn_id)

    row = await pg.fetchrow(
        "SELECT authorization_txn_id, revoked_at FROM oauth_grants WHERE grant_id = $1", grant_id
    )
    assert row is not None
    assert row["authorization_txn_id"] is None
    assert row["revoked_at"] is None


async def test_a_revoked_grant_always_states_why(pg: asyncpg.Connection) -> None:
    grant_id = await _make_grant(pg)

    with pytest.raises(asyncpg.CheckViolationError, match="oauth_grants_revocation_check"):
        await pg.execute("UPDATE oauth_grants SET revoked_at = now() WHERE grant_id = $1", grant_id)

    await pg.execute(
        "UPDATE oauth_grants SET revoked_at = now(), revoked_reason = $2 WHERE grant_id = $1",
        grant_id,
        "refresh_reuse",
    )
