"""`SqlNotificationOutbox` contra Postgres real (0010_notifications).

Regresion: `_UPSERT_NOTIFICATION` omitia la columna `message_id` y `save()`
nunca pasaba `notification.platform_message_id` -- toda notificacion
marcada SENT violaba `notifications_sent_is_traceable_check` ("SENT exige
sent_at Y message_id") al persistirse contra Postgres real. Sin esta
cobertura, `SqlNotificationOutbox` no aparecia en ningun test existente."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.notifications.domain.notification import Notification
from safent_ads.notifications.domain.value_objects import (
    Channel,
    DedupeKey,
    NotificationKind,
    Severity,
)
from safent_ads.notifications.infrastructure.sql_repositories import SqlNotificationOutbox
from safent_ads.shared.ids import BusinessId
from tests.contracts.sql_fixtures import campaign_ref, seed_entity

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 9, 9, 14, 0, tzinfo=UTC)


def _notification(business_id: BusinessId, *, dedupe_suffix: str) -> Notification:
    return Notification(
        notification_id=uuid.uuid4(),
        business_id=business_id,
        channel=Channel.TELEGRAM,
        severity=Severity.INFO,
        kind=NotificationKind.TICKER,
        dedupe_key=DedupeKey(f"ticker:{business_id}:{dedupe_suffix}"),
        body="ticker de prueba",
    )


async def _seed_business(session: AsyncSession, *, external_id: str) -> BusinessId:
    entity_ref = campaign_ref(external_id, platform_value="google")
    return BusinessId(await seed_entity(session, entity_ref))


async def test_sent_notification_persists_message_id(db_session: AsyncSession) -> None:
    business_id = await _seed_business(db_session, external_id="outbox-sent")
    notification = _notification(business_id, dedupe_suffix="sent")
    notification.mark_sent(platform_message_id=777)

    await SqlNotificationOutbox(db_session).save(notification)

    row = (
        await db_session.execute(
            text(
                "SELECT delivery_state, message_id, sent_at FROM notifications "
                "WHERE id = :id"
            ),
            {"id": notification.notification_id},
        )
    ).mappings().one()
    assert row["delivery_state"] == "SENT"
    assert row["message_id"] == 777
    assert row["sent_at"] is not None


async def test_pending_notification_leaves_message_id_null(db_session: AsyncSession) -> None:
    """`message_id IS NULL` es valido mientras `delivery_state <> 'SENT'`
    (`notifications_sent_is_traceable_check`) -- una notificacion que aun no
    se ha intentado entregar no tiene id de plataforma que guardar."""
    business_id = await _seed_business(db_session, external_id="outbox-pending")
    notification = _notification(business_id, dedupe_suffix="pending")

    await SqlNotificationOutbox(db_session).save(notification)

    row = (
        await db_session.execute(
            text("SELECT delivery_state, message_id FROM notifications WHERE id = :id"),
            {"id": notification.notification_id},
        )
    ).mappings().one()
    assert row["delivery_state"] == "PENDING"
    assert row["message_id"] is None


async def test_reservation_is_persisted_before_delivery_and_is_single_use(
    db_session: AsyncSession,
) -> None:
    business_id = await _seed_business(db_session, external_id="outbox-reservation")
    notification = _notification(business_id, dedupe_suffix="reservation")
    outbox = SqlNotificationOutbox(db_session)

    assert await outbox.try_reserve(notification) is True
    assert await outbox.try_reserve(notification) is False

    row = (
        await db_session.execute(
            text(
                "SELECT delivery_state, message_id FROM notifications "
                "WHERE dedupe_key = :dedupe_key"
            ),
            {"dedupe_key": notification.dedupe_key.value},
        )
    ).mappings().one()
    assert row == {"delivery_state": "PENDING", "message_id": None}


async def test_resending_updates_message_id_instead_of_duplicating(
    db_session: AsyncSession,
) -> None:
    """Reintentar tras un fallo (`FAILED` -> nuevo intento SENT) actualiza la
    misma fila por `dedupe_key` (NFR-6) con el `message_id` mas reciente."""
    business_id = await _seed_business(db_session, external_id="outbox-retry")
    outbox = SqlNotificationOutbox(db_session)
    failed = _notification(business_id, dedupe_suffix="retry")
    failed.mark_failed(attempts=1)
    await outbox.save(failed)

    retried = _notification(business_id, dedupe_suffix="retry")
    retried.mark_sent(platform_message_id=999)
    await outbox.save(retried)

    rows = (
        await db_session.execute(
            text(
                "SELECT delivery_state, message_id FROM notifications "
                "WHERE dedupe_key = :dedupe_key"
            ),
            {"dedupe_key": failed.dedupe_key.value},
        )
    ).mappings().all()
    assert len(rows) == 1
    assert rows[0]["delivery_state"] == "SENT"
    assert rows[0]["message_id"] == 999
