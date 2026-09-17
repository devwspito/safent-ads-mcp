"""`PublishCredentialHealthAlert` (tasks.md T126, threat-model.md C-21):
avisa al propietario cuando una credencial de plataforma pasa a
`expiring_soon`/`expired`/`revoked`.

A diferencia de `PublishCritical` (que agrupa por hora para no repetir la
misma alerta cada ciclo), aqui la deduplicacion es por transicion real: la
clave no lleva marca de tiempo, asi que una vez enviada para un (cuenta,
salud) no se repite mientras la credencial siga en ese estado -- y si la
credencial se recupera y vuelve a degradarse, la salud cambia y la clave
cambia con ella, asi que se envia de nuevo. Quien llama solo invoca esto
cuando `CheckCredentialHealth` ya detecto una transicion real
(`orchestration/infrastructure/credential_health_step.py`)."""

from __future__ import annotations

from typing import Final

from safent_ads.notifications.application.delivery import fan_out_to_owner_chats
from safent_ads.notifications.application.dto import CredentialHealthAlertEvent
from safent_ads.notifications.application.ports import MessengerPort, NotificationOutboxPort
from safent_ads.notifications.application.rendering import render_credential_health_alert
from safent_ads.notifications.domain.notification import Notification
from safent_ads.notifications.domain.value_objects import NotificationKind
from safent_ads.shared.ids import BusinessId, IdGenerator

_DEDUPE_PREFIX: Final = "credential_health"


class PublishCredentialHealthAlert:
    def __init__(
        self,
        *,
        outbox: NotificationOutboxPort,
        messenger: MessengerPort,
        id_generator: IdGenerator,
    ) -> None:
        self._outbox = outbox
        self._messenger = messenger
        self._id_generator = id_generator

    async def execute(
        self,
        *,
        business_id: BusinessId,
        owner_chat_ids: list[int],
        event: CredentialHealthAlertEvent,
    ) -> list[Notification]:
        body = render_credential_health_alert(event)
        base_dedupe_key = f"{_DEDUPE_PREFIX}:{event.account_ref}:{event.health_code}"
        return await fan_out_to_owner_chats(
            business_id=business_id,
            kind=NotificationKind.CRITICAL,
            body=body,
            base_dedupe_key=base_dedupe_key,
            owner_chat_ids=owner_chat_ids,
            outbox=self._outbox,
            messenger=self._messenger,
            id_generator=self._id_generator,
        )
