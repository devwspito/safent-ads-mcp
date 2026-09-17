"""Casos de uso de topes duros por cuenta (spec 008 T031): consultar,
fijar y retirar el tope que el propietario gestiona desde el panel.

**Auditoria doble.** El broker ya registra `broker_account_caps_set`/
`_denied` en su propio log -- la traza que un compromiso de `ads-api` no
puede borrar. Estos casos de uso escriben ADEMAS el evento en el registro
de decisiones encadenado, que es el que da la narrativa del panel. Va aqui,
no en el router, para que el efecto y su rastro no puedan separarse: un
router futuro no puede "olvidarse" de auditar.

La decision se anexa **despues** de que el broker acepte: un cambio que el
broker rechazo no es una decision aplicada, y anotarlo como tal seria
mentir en el registro. Los rechazos que nunca llegan al broker (400, 401,
428, 409) quedan solo del lado de `ads-api`: riesgo residual ya declarado
en `plan.md` §5."""

from __future__ import annotations

from safent_ads.accounts.application.hard_caps_ports import (
    FILE_AND_PANEL,
    AccountHardCapsView,
    HardCapsBrokerPort,
    PanelCapsInput,
)
from safent_ads.audit.application.record_decision import RecordDecision
from safent_ads.audit.domain.entry import ActorKind, DecisionKind, JsonValue, PendingDecision
from safent_ads.shared.ids import BusinessId

__all__ = [
    "DeleteAccountHardCaps",
    "GetAccountHardCaps",
    "SetAccountHardCaps",
    "relaxes_the_effective_cap",
    "withdrawal_relaxes_the_effective_cap",
]


class GetAccountHardCaps:
    """Sin efectos y sin auditoria: una consulta no es una decision."""

    def __init__(self, broker: HardCapsBrokerPort) -> None:
        self._broker = broker

    async def execute(self, platform_account_id: str) -> AccountHardCapsView:
        return await self._broker.resolve_account_caps(platform_account_id)


class SetAccountHardCaps:
    def __init__(
        self,
        broker: HardCapsBrokerPort,
        record_decision: RecordDecision,
        *,
        business_id: BusinessId,
    ) -> None:
        self._broker = broker
        self._record_decision = record_decision
        self._business_id = business_id

    async def execute(
        self,
        platform_account_id: str,
        caps: PanelCapsInput,
        *,
        owner_id: str,
        request_id: str | None = None,
    ) -> AccountHardCapsView:
        view = await self._broker.set_account_caps(
            platform_account_id, caps, requested_by=owner_id, request_id=request_id
        )
        await self._record_decision.execute(
            PendingDecision(
                business_id=self._business_id,
                kind=DecisionKind.ACCOUNT_HARD_CAPS_SET,
                actor_kind=ActorKind.OWNER,
                actor_id=owner_id,
                payload=_payload(view, requested=caps),
            )
        )
        return view


class DeleteAccountHardCaps:
    def __init__(
        self,
        broker: HardCapsBrokerPort,
        record_decision: RecordDecision,
        *,
        business_id: BusinessId,
    ) -> None:
        self._broker = broker
        self._record_decision = record_decision
        self._business_id = business_id

    async def execute(
        self, platform_account_id: str, *, owner_id: str, request_id: str | None = None
    ) -> AccountHardCapsView:
        view = await self._broker.delete_account_caps(
            platform_account_id, requested_by=owner_id, request_id=request_id
        )
        await self._record_decision.execute(
            PendingDecision(
                business_id=self._business_id,
                kind=DecisionKind.ACCOUNT_HARD_CAPS_DELETED,
                actor_kind=ActorKind.OWNER,
                actor_id=owner_id,
                payload=_payload(view, requested=None),
            )
        )
        return view


def _payload(
    view: AccountHardCapsView, *, requested: PanelCapsInput | None
) -> dict[str, JsonValue]:
    """Importes y procedencia de ESTA cuenta, nunca de otra, y nunca un
    dato personal: `actor_id` ya identifica a quien lo pidio."""
    payload: dict[str, JsonValue] = {
        "platform_account_id": view.platform_account_id,
        "source": view.source,
        "writable": view.writable,
        "clamped_by": list(view.clamped_by),
        "effective": _amounts(view),
    }
    if requested is not None:
        payload["requested"] = {
            "daily_cap_minor": requested.daily_cap_minor,
            "monthly_cap_minor": requested.monthly_cap_minor,
            "ceiling_minor": requested.ceiling_minor,
            "currency": requested.currency,
        }
    return payload


def _amounts(view: AccountHardCapsView) -> dict[str, JsonValue] | None:
    if view.effective is None:
        return None
    return {
        "daily_cap_minor": view.effective.daily_cap_minor,
        "monthly_cap_minor": view.effective.monthly_cap_minor,
        "floor_minor": view.effective.floor_minor,
        "ceiling_minor": view.effective.ceiling_minor,
    }


def relaxes_the_effective_cap(current: AccountHardCapsView, requested: PanelCapsInput) -> bool:
    """Definicion NORMATIVA de «subir» (`data-model.md` §`AccountHardCap`):
    subir es cualquier cambio que deje el tope efectivo menos restrictivo en
    alguna dimension, comparado contra el **efectivo actual**
    (`fichero ∧ panel`), nunca contra el valor anterior del panel.

    - `source=none -> panel`: la cuenta pasa de denegar el 100 % de las
      escrituras a poder escribir. Es la mayor relajacion posible, aunque no
      suba ningun importe previo.
    - Si los tres importes pedidos son <= al efectivo actual, el efectivo
      resultante no puede subir: `min(pedido, fichero) <= pedido <=
      efectivo_actual`. No es subir, y bajar nunca exige re-identificacion.
    - Si alguno supera el efectivo actual, PUEDE subir (el fichero quiza lo
      recorte, quiza no). Se trata como subida: la aproximacion se hace
      siempre del lado que pide mas prueba, nunca del que pide menos."""
    if current.effective is None:
        return True
    return (
        requested.daily_cap_minor > current.effective.daily_cap_minor
        or requested.monthly_cap_minor > current.effective.monthly_cap_minor
        or requested.ceiling_minor > current.effective.ceiling_minor
    )


def withdrawal_relaxes_the_effective_cap(current: AccountHardCapsView) -> bool:
    """Retirar el tope del panel puede ser una SUBIDA (revision T027, C3):
    con entrada de fichero, el efectivo pasa de `min(fichero, panel)` a
    `fichero`. Sin entrada de fichero la cuenta queda `writable=false`,
    estrictamente mas restrictivo, y no exige nada.

    Basta con saber si hay entrada de fichero (`source=file_and_panel`):
    suponer que sube cuando fichero y panel coinciden exactamente solo pide
    una prueba de mas, nunca una de menos."""
    return current.source == FILE_AND_PANEL
