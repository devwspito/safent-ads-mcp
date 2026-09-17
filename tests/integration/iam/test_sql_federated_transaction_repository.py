"""`SqlFederatedTransactionRepository` (spec 002b T024/T025, research.md
Decision C) contra Postgres real.

Lo que se demuestra aqui no se puede demostrar con un doble en memoria: que
el consumo de un solo uso aguanta DOS vueltas simultaneas con el mismo
`state`, cada una en su propia conexion. Un `SELECT` de comprobacion
seguido de un `UPDATE` pasaria los dos; la sentencia unica del adaptador
deja ganar exactamente a uno (FR-115)."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from safent_ads.iam.application.ports import FederatedTransactionRepository
from safent_ads.iam.domain.federated_transaction import (
    FederatedLoginTransaction,
    ReferenceHash,
    TransactionPurpose,
)
from safent_ads.iam.infrastructure.sql_federated_transaction_repository import (
    SqlFederatedTransactionRepository,
)

pytestmark = pytest.mark.integration

_CLEANUP = (
    "DELETE FROM federated_login_transactions",
    "DELETE FROM oauth_authorization_requests",
    "DELETE FROM oauth_clients",
    "DELETE FROM sessions",
    "DELETE FROM owners",
)


def _fingerprint(seed: str = "") -> str:
    return hashlib.sha256((seed or uuid.uuid4().hex).encode()).hexdigest()


async def _wipe(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        for statement in _CLEANUP:
            await connection.execute(text(statement))


@pytest.fixture
async def engine(isolated_iam_database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(isolated_iam_database_url, pool_pre_ping=True)
    await _wipe(engine)
    try:
        yield engine
    finally:
        await _wipe(engine)
        await engine.dispose()


@pytest.fixture
async def db_session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    session = AsyncSession(bind=engine, expire_on_commit=False)
    try:
        yield session
    finally:
        await session.close()


async def _make_owner(session: AsyncSession) -> uuid.UUID:
    owner_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO owners (id, email, password_hash) "
            "VALUES (:id, :email, 'argon2id$fixture$not-a-real-hash')"
        ),
        {"id": str(owner_id), "email": f"owner-{owner_id.hex[:8]}@safent.example"},
    )
    await session.commit()
    return owner_id


async def _make_session_row(session: AsyncSession, owner_id: uuid.UUID) -> uuid.UUID:
    session_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO sessions (id, owner_id, token_hash, expires_at) "
            "VALUES (:id, :owner_id, :token_hash, now() + interval '30 minutes')"
        ),
        {"id": str(session_id), "owner_id": str(owner_id), "token_hash": _fingerprint()},
    )
    await session.commit()
    return session_id


def _transaction(
    *,
    state_hash: str | None = None,
    purpose: TransactionPurpose = TransactionPurpose.LOGIN,
    session_id: uuid.UUID | None = None,
    now: datetime | None = None,
    ttl: timedelta = timedelta(minutes=10),
    ip_address: str | None = None,
) -> FederatedLoginTransaction:
    created_at = now or datetime.now(UTC)
    return FederatedLoginTransaction(
        state_hash=ReferenceHash(state_hash or _fingerprint()),
        nonce_hash=ReferenceHash(_fingerprint()),
        purpose=purpose,
        session_id=session_id,
        txn_id=None,
        created_at=created_at,
        expires_at=created_at + ttl,
        ip_address=ip_address,
        consumed_at=None,
    )


def test_the_adapter_satisfies_the_port(db_session: AsyncSession) -> None:
    repository: FederatedTransactionRepository = SqlFederatedTransactionRepository(db_session)

    assert repository is not None


async def test_a_pending_transaction_is_consumed_once(db_session: AsyncSession) -> None:
    repository = SqlFederatedTransactionRepository(db_session)
    transaction = _transaction()
    await repository.create(transaction)

    consumed = await repository.consume(state_hash=transaction.state_hash, now=datetime.now(UTC))

    assert consumed is not None
    assert consumed.state_hash == transaction.state_hash
    assert consumed.nonce_hash == transaction.nonce_hash
    assert consumed.purpose is TransactionPurpose.LOGIN
    assert consumed.consumed_at is not None


async def test_consuming_the_same_state_twice_has_no_effect_the_second_time(
    db_session: AsyncSession,
) -> None:
    repository = SqlFederatedTransactionRepository(db_session)
    transaction = _transaction()
    await repository.create(transaction)
    now = datetime.now(UTC)

    assert await repository.consume(state_hash=transaction.state_hash, now=now) is not None
    assert await repository.consume(state_hash=transaction.state_hash, now=now) is None


async def test_an_unknown_state_is_rejected_without_touching_anything(
    db_session: AsyncSession,
) -> None:
    repository = SqlFederatedTransactionRepository(db_session)
    survivor = _transaction()
    await repository.create(survivor)

    unknown = ReferenceHash(_fingerprint())
    assert await repository.consume(state_hash=unknown, now=datetime.now(UTC)) is None

    still_pending = await repository.get(survivor.state_hash)
    assert still_pending is not None
    assert still_pending.consumed_at is None


async def test_an_expired_transaction_is_rejected_and_stays_unconsumed(
    db_session: AsyncSession,
) -> None:
    """Rechazada SIN efectos: ni siquiera se marca como consumida, para que
    la poda del janitor la barra por `expires_at` y no quede una fila con
    una marca que nadie puso de verdad."""
    repository = SqlFederatedTransactionRepository(db_session)
    now = datetime.now(UTC)
    expired = _transaction(now=now - timedelta(minutes=30), ttl=timedelta(minutes=10))
    await repository.create(expired)

    assert await repository.consume(state_hash=expired.state_hash, now=now) is None

    stored = await repository.get(expired.state_hash)
    assert stored is not None
    assert stored.consumed_at is None


async def test_two_simultaneous_returns_with_the_same_state_leave_exactly_one_winner(
    engine: AsyncEngine,
) -> None:
    """Dos conexiones, la misma `state`, a la vez. La segunda se queda
    bloqueada en la fila hasta que la primera confirma y entonces vuelve a
    evaluar el `WHERE`, que ya no se cumple."""
    setup = AsyncSession(bind=engine, expire_on_commit=False)
    transaction = _transaction()
    try:
        await SqlFederatedTransactionRepository(setup).create(transaction)
    finally:
        await setup.close()

    now = datetime.now(UTC)
    first = AsyncSession(bind=engine, expire_on_commit=False)
    second = AsyncSession(bind=engine, expire_on_commit=False)
    try:
        outcomes = await asyncio.gather(
            SqlFederatedTransactionRepository(first).consume(
                state_hash=transaction.state_hash, now=now
            ),
            SqlFederatedTransactionRepository(second).consume(
                state_hash=transaction.state_hash, now=now
            ),
        )
    finally:
        await first.close()
        await second.close()

    assert len([outcome for outcome in outcomes if outcome is not None]) == 1


async def test_a_reidentify_transaction_remembers_the_session_that_opened_it(
    db_session: AsyncSession,
) -> None:
    """La atadura que impide marcar frescura en una sesion ajena: el
    `session_id` vuelve del consumo, no lo pone el llamador."""
    owner_id = await _make_owner(db_session)
    session_id = await _make_session_row(db_session, owner_id)
    repository = SqlFederatedTransactionRepository(db_session)
    transaction = _transaction(purpose=TransactionPurpose.REIDENTIFY, session_id=session_id)
    await repository.create(transaction)

    consumed = await repository.consume(state_hash=transaction.state_hash, now=datetime.now(UTC))

    assert consumed is not None
    assert consumed.purpose is TransactionPurpose.REIDENTIFY
    assert consumed.session_id == session_id


async def test_the_ip_that_opened_the_jump_survives_a_round_trip(
    db_session: AsyncSession,
) -> None:
    repository = SqlFederatedTransactionRepository(db_session)
    transaction = _transaction(ip_address="203.0.113.9")
    await repository.create(transaction)

    consumed = await repository.consume(state_hash=transaction.state_hash, now=datetime.now(UTC))

    assert consumed is not None
    assert consumed.ip_address == "203.0.113.9"


async def test_count_pending_for_ip_ignores_other_ips_consumed_and_expired_rows(
    db_session: AsyncSession,
) -> None:
    """threat-model.md C-79: solo cuenta lo pendiente (ni consumido ni
    caducado) de ESA IP -- ni lo de otra IP, ni lo ya resuelto."""
    repository = SqlFederatedTransactionRepository(db_session)
    now = datetime.now(UTC)
    target_ip = "203.0.113.9"

    pending_same_ip = _transaction(ip_address=target_ip, now=now)
    await repository.create(pending_same_ip)
    other_ip = _transaction(ip_address="198.51.100.1", now=now)
    await repository.create(other_ip)
    consumed_same_ip = _transaction(ip_address=target_ip, now=now)
    await repository.create(consumed_same_ip)
    await repository.consume(state_hash=consumed_same_ip.state_hash, now=now)
    expired_same_ip = _transaction(
        ip_address=target_ip, now=now - timedelta(minutes=30), ttl=timedelta(minutes=10)
    )
    await repository.create(expired_same_ip)
    no_ip = _transaction(ip_address=None, now=now)
    await repository.create(no_ip)

    pending = await repository.count_pending_for_ip(ip_address=target_ip, now=now)

    assert pending == 1


async def test_purging_removes_only_expired_rows(db_session: AsyncSession) -> None:
    """El `DELETE` que el janitor existente suma a su barrido, no un proceso
    nuevo. Una fila consumida pero todavia dentro de su ventana NO se borra:
    es la que permite reconocer una vuelta repetida."""
    repository = SqlFederatedTransactionRepository(db_session)
    now = datetime.now(UTC)
    expired = _transaction(now=now - timedelta(minutes=30), ttl=timedelta(minutes=10))
    live = _transaction(now=now)
    consumed_but_live = _transaction(now=now)
    await repository.create(expired)
    await repository.create(live)
    await repository.create(consumed_but_live)
    await repository.consume(state_hash=consumed_but_live.state_hash, now=now)

    purged = await repository.purge_expired(now)

    assert purged == 1
    assert await repository.get(expired.state_hash) is None
    assert await repository.get(live.state_hash) is not None
    assert await repository.get(consumed_but_live.state_hash) is not None
