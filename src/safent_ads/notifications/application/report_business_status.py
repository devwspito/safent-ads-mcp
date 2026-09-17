"""`ReportBusinessStatus` (este branch, `/estado`): un resumen por cada
negocio del propietario emparejado (contracts/telegram.md: "sin fila
verificada el canal avisa pero no decide" -- aqui no hay decision que
tomar, solo lectura, pero el emparejamiento sigue siendo obligatorio antes
de responder nada mas que "Sin emparejar")."""

from __future__ import annotations

from safent_ads.notifications.application.ports import BusinessStatusPort, TelegramPairingGuardPort
from safent_ads.notifications.application.rendering import render_business_status_card
from safent_ads.shared.ids import BusinessId

_UNPAIRED_REPLY = "Sin emparejar"
_NO_BUSINESSES_REPLY = "No hay negocios activos."


class ReportBusinessStatus:
    def __init__(
        self, *, status: BusinessStatusPort, pairing_guard: TelegramPairingGuardPort
    ) -> None:
        self._status = status
        self._pairing_guard = pairing_guard

    async def execute(self, *, chat_id: int) -> tuple[str, ...]:
        if not await self._pairing_guard.is_chat_paired(chat_id):
            return (_UNPAIRED_REPLY,)
        businesses = await self._status.list_businesses()
        if not businesses:
            return (_NO_BUSINESSES_REPLY,)
        cards: list[str] = []
        for business in businesses:
            status = await self._status.get_status(BusinessId.parse(business.business_id))
            cards.append(render_business_status_card(business_name=business.name, status=status))
        return tuple(cards)
