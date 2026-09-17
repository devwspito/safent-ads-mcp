"""`PublishCredentialHealthAlert` (tasks.md T126, threat-model.md C-21):
deduplicacion por (cuenta, salud) -- sin marca de tiempo, a diferencia de
`PublishCritical` -- hasta que la salud cambia."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from safent_ads.notifications.application.dto import CredentialHealthAlertEvent
from safent_ads.notifications.application.publish_credential_health_alert import (
    PublishCredentialHealthAlert,
)
from safent_ads.notifications.testing.fakes import FakeMessenger, FakeNotificationOutbox
from safent_ads.shared.ids import BusinessId, UuidIdGenerator

_NOW = datetime(2026, 9, 10, 9, 0, tzinfo=UTC)


def _event(**overrides: object) -> CredentialHealthAlertEvent:
    defaults: dict[str, object] = {
        "business_name": "Negocio Ejemplo",
        "account_ref": "google:1234567890",
        "account_label": "Google Ads (1234567890)",
        "health_code": "expired",
        "health_label": "caducada",
        "error_code": "TOKEN_EXPIRED",
        "occurred_at": _NOW,
    }
    defaults.update(overrides)
    return CredentialHealthAlertEvent(**defaults)  # type: ignore[arg-type]


async def test_sends_one_notification_with_the_reason_in_the_body() -> None:
    business_id = BusinessId.new()
    messenger = FakeMessenger()
    use_case = PublishCredentialHealthAlert(
        outbox=FakeNotificationOutbox(), messenger=messenger, id_generator=UuidIdGenerator()
    )

    sent = await use_case.execute(business_id=business_id, owner_chat_ids=[111], event=_event())

    assert len(sent) == 1
    assert sent[0].kind.value == "critical"
    assert "🔑 CREDENCIAL · Negocio Ejemplo" in sent[0].body
    assert "Google Ads (1234567890): caducada (TOKEN_EXPIRED)" in sent[0].body


async def test_repeated_alert_for_the_same_account_and_health_is_deduplicated() -> None:
    business_id = BusinessId.new()
    outbox = FakeNotificationOutbox()
    use_case = PublishCredentialHealthAlert(
        outbox=outbox, messenger=FakeMessenger(), id_generator=UuidIdGenerator()
    )

    first = await use_case.execute(business_id=business_id, owner_chat_ids=[111], event=_event())
    # Mismo (cuenta, salud) mucho mas tarde: a diferencia de `PublishCritical`,
    # aqui no hay ventana horaria -- sigue deduplicado mientras no cambie.
    later = _event(occurred_at=_NOW + timedelta(days=3))
    second = await use_case.execute(business_id=business_id, owner_chat_ids=[111], event=later)

    assert len(first) == 1
    assert second == []


async def test_a_different_health_for_the_same_account_notifies_again() -> None:
    business_id = BusinessId.new()
    outbox = FakeNotificationOutbox()
    use_case = PublishCredentialHealthAlert(
        outbox=outbox, messenger=FakeMessenger(), id_generator=UuidIdGenerator()
    )

    expired = await use_case.execute(
        business_id=business_id, owner_chat_ids=[111], event=_event()
    )
    revoked_event = _event(
        health_code="revoked", health_label="revocada", error_code="CREDENTIAL_REVOKED"
    )
    revoked = await use_case.execute(
        business_id=business_id, owner_chat_ids=[111], event=revoked_event
    )

    assert len(expired) == 1
    assert len(revoked) == 1


async def test_a_different_account_is_never_deduplicated_against_another() -> None:
    business_id = BusinessId.new()
    outbox = FakeNotificationOutbox()
    use_case = PublishCredentialHealthAlert(
        outbox=outbox, messenger=FakeMessenger(), id_generator=UuidIdGenerator()
    )

    first_account = await use_case.execute(
        business_id=business_id, owner_chat_ids=[111], event=_event()
    )
    second_account = await use_case.execute(
        business_id=business_id,
        owner_chat_ids=[111],
        event=_event(account_ref="meta:999", account_label="Meta Ads (999)"),
    )

    assert len(first_account) == 1
    assert len(second_account) == 1
