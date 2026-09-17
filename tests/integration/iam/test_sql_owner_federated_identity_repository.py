"""`SqlOwnerFederatedIdentityRepository` (spec 002b T024/T025, FR-107/FR-108)
contra Postgres real, con el patron de `test_sql_owner_bridge_repository.py`.

Las tres resoluciones (`CREATED` / `BOUND` / `ALREADY_BOUND`), la carrera de
creacion con DOS conexiones -- que es la que distingue el advisory lock de
un simple `ON CONFLICT` sobre el correo -- y el rechazo de una segunda
identidad del mismo emisor, que es a la vez el edge case «cuenta recreada» y
FR-108 («esta instalacion ya tiene dueno»)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from safent_ads.iam.application.errors import OwnerBoundElsewhereError
from safent_ads.iam.application.ports import (
    OwnerFederatedIdentityRepository,
    OwnerFederatedIdentityResolution,
    ResolvedFederatedOwner,
)
from safent_ads.iam.domain.email import Email
from safent_ads.iam.domain.federated_identity import FederatedIssuer, FederatedSubject
from safent_ads.iam.infrastructure.sql_owner_bridge_repository import SqlOwnerBridgeRepository
from safent_ads.iam.infrastructure.sql_owner_federated_identity_repository import (
    SqlOwnerFederatedIdentityRepository,
)

pytestmark = pytest.mark.integration

_GOOGLE = "https://accounts.google.com"
_OWNER_EMAIL = "dueno@example.com"
_CLEANUP = (
    "DELETE FROM federated_login_transactions",
    "DELETE FROM owner_federated_identities",
    "DELETE FROM sessions",
    "DELETE FROM owners",
)


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


async def _resolve(
    session: AsyncSession,
    *,
    subject: str,
    email: str = _OWNER_EMAIL,
    now: datetime | None = None,
) -> ResolvedFederatedOwner:
    return await SqlOwnerFederatedIdentityRepository(session).resolve_for_subject(
        issuer=FederatedIssuer.parse(_GOOGLE),
        subject=FederatedSubject(subject),
        email=Email(email),
        now=now or datetime.now(UTC),
    )


# Sentencias literales, no interpolacion: el nombre de la tabla no se
# construye nunca a partir de un argumento (ruff S608).
_COUNT_SQL = {
    "owners": text("SELECT count(*) FROM owners"),
    "owner_federated_identities": text("SELECT count(*) FROM owner_federated_identities"),
}


async def _count(session: AsyncSession, table: str) -> int:
    return int((await session.execute(_COUNT_SQL[table])).scalar_one())


def test_the_adapter_satisfies_the_port(db_session: AsyncSession) -> None:
    repository: OwnerFederatedIdentityRepository = SqlOwnerFederatedIdentityRepository(db_session)

    assert repository is not None


async def test_an_empty_installation_gains_its_owner_on_the_first_entry(
    db_session: AsyncSession,
) -> None:
    """El alta TOFU de FR-107: sin consola, sin sembrar contrasenas."""
    resolved = await _resolve(db_session, subject="google-sub-001")

    assert resolved.resolution is OwnerFederatedIdentityResolution.CREATED
    assert await _count(db_session, "owners") == 1
    assert await _count(db_session, "owner_federated_identities") == 1


async def test_the_owner_created_by_google_has_an_unusable_password(
    db_session: AsyncSession,
) -> None:
    """FR-109: ni utilizable ni adivinable. El hash es Argon2id sobre bytes
    aleatorios que nadie conserva -- lo comprobable desde fuera es que no es
    un marcador ni una cadena vacia y que el correo real es el que queda."""
    resolved = await _resolve(db_session, subject="google-sub-002")

    row = (
        (
            await db_session.execute(
                text("SELECT email, password_hash FROM owners WHERE id = :id"),
                {"id": str(resolved.owner_id)},
            )
        )
        .mappings()
        .one()
    )
    assert row["email"] == _OWNER_EMAIL
    assert row["password_hash"].startswith("$argon2")


async def test_the_real_email_is_kept_as_the_audit_of_the_binding(
    db_session: AsyncSession,
) -> None:
    resolved = await _resolve(db_session, subject="google-sub-003")

    row = (
        (
            await db_session.execute(
                text(
                    "SELECT subject, email_at_binding, bound_at, last_seen_at "
                    "FROM owner_federated_identities WHERE owner_id = :id"
                ),
                {"id": str(resolved.owner_id)},
            )
        )
        .mappings()
        .one()
    )
    assert row["subject"] == "google-sub-003"
    assert row["email_at_binding"] == _OWNER_EMAIL
    assert row["last_seen_at"] == row["bound_at"]


async def test_an_owner_born_from_the_bridge_gets_bound_tofu(db_session: AsyncSession) -> None:
    """El despliegue que ya uso el puente de Safent: el dueno existe con su
    correo sintetico y la primera entrada con Google le ata la identidad sin
    crear un segundo dueno ni reescribir su correo."""
    await db_session.execute(
        text(
            "INSERT INTO owners (id, email, password_hash) VALUES (gen_random_uuid(), "
            "'bridge-owner@safent-ads.internal', 'irrelevant')"
        )
    )
    await db_session.commit()

    resolved = await _resolve(db_session, subject="google-sub-004")

    assert resolved.resolution is OwnerFederatedIdentityResolution.BOUND
    assert await _count(db_session, "owners") == 1
    email = (
        await db_session.execute(
            text("SELECT email FROM owners WHERE id = :id"), {"id": str(resolved.owner_id)}
        )
    ).scalar_one()
    assert email == "bridge-owner@safent-ads.internal"


async def test_the_second_entry_recognises_the_same_owner_and_refreshes_last_seen(
    db_session: AsyncSession,
) -> None:
    first_seen = datetime.now(UTC) - timedelta(hours=2)
    first = await _resolve(db_session, subject="google-sub-005", now=first_seen)

    second = await _resolve(
        db_session, subject="google-sub-005", now=first_seen + timedelta(hours=1)
    )

    assert second.owner_id == first.owner_id
    assert second.resolution is OwnerFederatedIdentityResolution.ALREADY_BOUND
    assert await _count(db_session, "owners") == 1
    row = (
        (
            await db_session.execute(
                text(
                    "SELECT bound_at, last_seen_at FROM owner_federated_identities "
                    "WHERE subject = :subject"
                ),
                {"subject": "google-sub-005"},
            )
        )
        .mappings()
        .one()
    )
    assert row["last_seen_at"] > row["bound_at"]


async def test_a_second_google_account_is_rejected(db_session: AsyncSession) -> None:
    """FR-108 y el edge case «cuenta recreada» son el mismo rechazo: un
    dueno no queda atado a dos identidades del mismo emisor sin
    intervencion administrativa."""
    await _resolve(db_session, subject="google-sub-006")

    with pytest.raises(OwnerBoundElsewhereError):
        await _resolve(db_session, subject="google-sub-007", email="otro@example.com")

    assert await _count(db_session, "owners") == 1
    assert await _count(db_session, "owner_federated_identities") == 1


async def test_two_simultaneous_first_entries_create_exactly_one_owner(
    engine: AsyncEngine,
) -> None:
    """La carrera que el `ON CONFLICT` del correo NO cubre: dos direcciones
    autorizadas distintas entrando a la vez en una instalacion vacia. El
    advisory lock las serializa, asi que una da de alta al dueno y la otra
    se encuentra con que la instalacion ya lo tiene."""
    first = AsyncSession(bind=engine, expire_on_commit=False)
    second = AsyncSession(bind=engine, expire_on_commit=False)
    now = datetime.now(UTC)
    try:
        outcomes = await asyncio.gather(
            _resolve(first, subject="google-sub-race-a", email="a@example.com", now=now),
            _resolve(second, subject="google-sub-race-b", email="b@example.com", now=now),
            return_exceptions=True,
        )
    finally:
        await first.close()
        await second.close()

    created = [
        outcome
        for outcome in outcomes
        if isinstance(outcome, ResolvedFederatedOwner)
        and outcome.resolution is OwnerFederatedIdentityResolution.CREATED
    ]
    rejected = [outcome for outcome in outcomes if isinstance(outcome, OwnerBoundElsewhereError)]
    assert len(created) == 1
    assert len(rejected) == 1

    audit = AsyncSession(bind=engine, expire_on_commit=False)
    try:
        assert await _count(audit, "owners") == 1
        assert await _count(audit, "owner_federated_identities") == 1
    finally:
        await audit.close()


async def test_two_simultaneous_first_entries_of_the_same_account_create_one_owner(
    engine: AsyncEngine,
) -> None:
    """El caso realista con una sola direccion autorizada (S1): dos
    pestanas, la misma cuenta. Una da de alta, la otra reconoce."""
    first = AsyncSession(bind=engine, expire_on_commit=False)
    second = AsyncSession(bind=engine, expire_on_commit=False)
    now = datetime.now(UTC)
    try:
        outcomes = await asyncio.gather(
            _resolve(first, subject="google-sub-same", now=now),
            _resolve(second, subject="google-sub-same", now=now),
        )
    finally:
        await first.close()
        await second.close()

    resolutions = sorted(outcome.resolution.value for outcome in outcomes)
    assert resolutions == ["already_bound", "created"]
    assert len({outcome.owner_id for outcome in outcomes}) == 1

    audit = AsyncSession(bind=engine, expire_on_commit=False)
    try:
        assert await _count(audit, "owners") == 1
    finally:
        await audit.close()


async def test_bridge_and_federated_racing_create_exactly_one_owner(
    engine: AsyncEngine,
) -> None:
    """C-59 (seguimiento): la carrera cruzada entre las DOS vías de alta TOFU
    -- el puente Ed25519 de Safent y el login federado de Google -- sobre una
    instalación vacía. Antes de compartir `LOCK_SOLE_OWNER_SQL`
    (`owner_singleton_lock.py`), esto creaba DOS dueños: el puente escribe el
    correo sintético fijo y el federado el correo real de Google, así que
    `owners.email UNIQUE` nunca llegaba a chocar entre ellos."""
    bridge_session = AsyncSession(bind=engine, expire_on_commit=False)
    federated_session = AsyncSession(bind=engine, expire_on_commit=False)
    try:
        outcomes = await asyncio.gather(
            SqlOwnerBridgeRepository(bridge_session).resolve_for_subject("sub-bridge-cross-race"),
            _resolve(federated_session, subject="google-sub-cross-race"),
        )
    finally:
        await bridge_session.close()
        await federated_session.close()

    owner_ids = {outcome.owner_id for outcome in outcomes}
    assert len(owner_ids) == 1

    audit = AsyncSession(bind=engine, expire_on_commit=False)
    try:
        assert await _count(audit, "owners") == 1
        assert await _count(audit, "owner_federated_identities") == 1
    finally:
        await audit.close()


async def test_a_subject_never_belongs_to_two_owners(db_session: AsyncSession) -> None:
    """La PK `(issuer, subject)` es la ultima palabra aunque alguien
    consiguiera crear un segundo dueno por otro camino."""
    resolved = await _resolve(db_session, subject="google-sub-008")
    intruder_id = uuid.uuid4()
    await db_session.execute(
        text(
            "INSERT INTO owners (id, email, password_hash) "
            "VALUES (:id, 'intruso@example.com', 'irrelevant')"
        ),
        {"id": str(intruder_id)},
    )
    await db_session.commit()

    with pytest.raises(Exception, match="owner_federated_identities_pkey"):
        await db_session.execute(
            text(
                "INSERT INTO owner_federated_identities (owner_id, issuer, subject, "
                "email_at_binding, bound_at, last_seen_at) "
                "VALUES (:owner_id, :issuer, 'google-sub-008', 'intruso@example.com', "
                "now(), now())"
            ),
            {"owner_id": str(intruder_id), "issuer": _GOOGLE},
        )
    await db_session.rollback()

    owner_of_subject = (
        await db_session.execute(
            text("SELECT owner_id FROM owner_federated_identities WHERE subject = :subject"),
            {"subject": "google-sub-008"},
        )
    ).scalar_one()
    assert owner_of_subject == resolved.owner_id
