"""`LiveCredentialHealthStep` (tasks.md T126, threat-model.md C-21) contra
Postgres real: el ciclo actualiza `credential_refs`, deja una entrada de
`decision_log` y envia una unica alerta por transicion. El broker es el
doble compartido de `accounts/application` (`FakeOAuthBrokerPort`): nunca
abre un socket real, nunca contacta Google/Meta."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from safent_ads.accounts.application.connect_ports import CredentialStatusResult
from safent_ads.accounts.application.list_platform_accounts import ListPlatformAccounts
from safent_ads.accounts.domain.platform_credential import CredentialStatus
from safent_ads.accounts.domain.refs import AccountRef
from safent_ads.accounts.infrastructure.sql_connect_repositories import SqlCredentialRepository
from safent_ads.accounts.infrastructure.sql_repositories import SqlAccountRepository
from safent_ads.accounts.presentation.platform_accounts_rest import platform_account_to_json
from safent_ads.notifications.application.publish_credential_health_alert import (
    PublishCredentialHealthAlert,
)
from safent_ads.notifications.domain.notification import Notification
from safent_ads.notifications.infrastructure.sql_repositories import SqlNotificationOutbox
from safent_ads.notifications.testing.fakes import FakeMessenger
from safent_ads.orchestration.infrastructure.credential_health_step import LiveCredentialHealthStep
from safent_ads.shared.ids import BusinessId, EntityRef, UuidIdGenerator
from tests.contracts.sql_fixtures import account_external_id, campaign_ref, seed_entity
from tests.unit.accounts.application.conftest import FakeOAuthBrokerPort

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 9, 10, 8, 0, tzinfo=UTC)


@pytest.fixture
async def session_factory(
    isolated_database_url: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(isolated_database_url, pool_pre_ping=True)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


class _SessionScopedOutbox:
    """Mismo patron que `orchestration.infrastructure.runtime.
    _PerCallNotificationOutbox`: una sesion (y su propio commit, dentro de
    `SqlNotificationOutbox.save`) por llamada."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def try_reserve(self, notification: Notification) -> bool:
        async with self._session_factory() as session:
            return await SqlNotificationOutbox(session).try_reserve(notification)

    async def save(self, notification: Notification) -> None:
        async with self._session_factory() as session:
            await SqlNotificationOutbox(session).save(notification)


def _step(
    session_factory: async_sessionmaker[AsyncSession],
    broker: FakeOAuthBrokerPort,
    messenger: FakeMessenger,
) -> LiveCredentialHealthStep:
    publish_alert = PublishCredentialHealthAlert(
        outbox=_SessionScopedOutbox(session_factory),
        messenger=messenger,
        id_generator=UuidIdGenerator(),
    )
    return LiveCredentialHealthStep(session_factory, broker, publish_alert, (111,))


async def test_cycle_persists_the_health_check_on_credential_refs(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    entity_ref = campaign_ref(f"cred-health-{id(session_factory)}", platform_value="google")
    async with session_factory() as session:
        business_id = BusinessId(await seed_entity(session, entity_ref))
        await session.commit()
        account = await SqlAccountRepository(session).get_by_ref(
            _account_ref(entity_ref)
        )
    assert account is not None

    broker = FakeOAuthBrokerPort(
        status_result=CredentialStatusResult(
            status=CredentialStatus.EXPIRED,
            scopes=frozenset({"adwords"}),
            expires_at=_NOW - timedelta(hours=1),
            last_validated_at=None,
        )
    )
    messenger = FakeMessenger()
    step = _step(session_factory, broker, messenger)

    await step.run(business_id, "cycle-1", _NOW)

    async with session_factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT status, checked_at, last_error_code, expires_at "
                    "FROM credential_refs WHERE id = :id"
                ),
                {"id": account.credential_ref_id.value},
            )
        ).one()
    assert row.status == "INVALID"
    assert row.checked_at == _NOW
    assert row.last_error_code == "TOKEN_EXPIRED"
    assert row.expires_at == _NOW - timedelta(hours=1)


async def test_platform_accounts_view_reflects_the_new_health(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    entity_ref = campaign_ref(f"cred-health-view-{id(session_factory)}", platform_value="google")
    async with session_factory() as session:
        business_id = BusinessId(await seed_entity(session, entity_ref))
        await session.commit()

    broker = FakeOAuthBrokerPort(
        status_result=CredentialStatusResult(
            status=CredentialStatus.REVOKED,
            scopes=frozenset({"adwords"}),
            expires_at=None,
            last_validated_at=None,
        )
    )
    step = _step(session_factory, broker, FakeMessenger())
    await step.run(business_id, "cycle-1", _NOW)

    async with session_factory() as session:
        views = await ListPlatformAccounts(
            SqlAccountRepository(session), SqlCredentialRepository(session)
        ).execute(business_id)
    assert len(views) == 1
    body = platform_account_to_json(views[0], now=_NOW)
    assert body["token"]["health"] == "revoked"
    assert body["last_error_code"] == "CREDENTIAL_REVOKED"


async def test_one_notification_and_one_decision_log_entry_per_transition(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    entity_ref = campaign_ref(f"cred-health-alert-{id(session_factory)}", platform_value="google")
    async with session_factory() as session:
        business_id = BusinessId(await seed_entity(session, entity_ref))
        await session.commit()

    broker = FakeOAuthBrokerPort(
        status_result=CredentialStatusResult(
            status=CredentialStatus.REVOKED,
            scopes=frozenset({"adwords"}),
            expires_at=None,
            last_validated_at=None,
        )
    )
    messenger = FakeMessenger()
    step = _step(session_factory, broker, messenger)

    # Dos vueltas seguidas, mismo estado reportado por el broker: la
    # segunda no debe repetir ni la alerta ni la entrada de decision_log
    # (deduplicado por (cuenta, salud) hasta que cambia, tasks.md T126).
    await step.run(business_id, "cycle-1", _NOW)
    await step.run(business_id, "cycle-2", _NOW + timedelta(hours=6))

    assert len(messenger.sent) == 1
    assert "revocada" in messenger.sent[0].text
    assert "CREDENTIAL_REVOKED" in messenger.sent[0].text

    async with session_factory() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT payload FROM decision_log "
                    "WHERE business_id = :business_id AND event_type = 'credential_health_changed'"
                ),
                {"business_id": business_id.value},
            )
        ).all()
    assert len(rows) == 1
    payload = json.loads(rows[0].payload) if isinstance(rows[0].payload, str) else rows[0].payload
    assert payload["previous_health"] == "ok"
    assert payload["current_health"] == "revoked"
    assert payload["error_code"] == "CREDENTIAL_REVOKED"


def _account_ref(entity_ref: EntityRef) -> AccountRef:
    return AccountRef(entity_ref.platform, account_external_id(entity_ref))
