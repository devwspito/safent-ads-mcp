"""`EffectiveCapsResolver`: une `config/caps.yaml` (la autoridad) con el
estado que el propietario fijo desde el panel, y entrega el `AccountCaps`
que la tuberia de escritura aplica (spec 008 T029).

Satisface `CapsResolverPort`, igual que `CapsConfig`, asi que
`WriteAuthorizationPipeline` no distingue uno de otro. Y esa es la prueba
de i1: **sin `panel_managed` declarado este resolutor devuelve exactamente
lo que devolveria `CapsConfig.resolve`**, byte a byte -- delega en el.

i2 (revalidacion AL APLICAR): `resolve()` vuelve a recortar contra el sobre
en CADA llamada, no solo al guardar. Un fichero de estado manipulado a mano
por encima del sobre nunca autoriza el valor manipulado.

i7 (bajada concurrente): `resolve()` relee la instantanea del almacen cada
vez. `WriteAuthorizationPipeline._reserve_write` la llama DENTRO de la
transaccion del ledger, nunca cacheada desde `authorize`, asi que una
bajada ya aplicada gana sobre una escritura en vuelo.

**La entrada del fichero se busca EXACTA, como hoy.** La canonicalizacion
solo se aplica a la clave del estado del panel. Que las dos coincidan lo
garantiza `CapsConfig`, que con el sobre declarado exige que las claves de
`accounts:` ya vengan canonicas."""

from __future__ import annotations

from safent_ads.broker.domain.account_key import (
    InvalidPlatformAccountIdError,
    canonical_platform_account_id,
)
from safent_ads.broker.domain.hard_caps_policy import (
    CapsResolution,
    FileAmountsView,
    resolve_effective_caps,
)
from safent_ads.broker.infrastructure.caps_config import (
    AccountCaps,
    CapsConfig,
    CapsConfigError,
    SpendEnvelope,
)
from safent_ads.broker.infrastructure.caps_state import CapsStateStore, PanelCapsSnapshot

__all__ = ["EffectiveCapsResolver"]

# Sin sobre declarado no hay almacen de estado que montar, y el panel no
# puede haber fijado nada: la instantanea vacia no es un fallo de lectura.
_NO_PANEL_STATE = PanelCapsSnapshot(available=True)


class EffectiveCapsResolver:
    def __init__(self, caps: CapsConfig, store: CapsStateStore | None) -> None:
        self._caps = caps
        self._store = store

    @property
    def envelope(self) -> SpendEnvelope | None:
        return self._caps.panel_managed

    @property
    def file_config(self) -> CapsConfig:
        return self._caps

    def state_snapshot(self) -> PanelCapsSnapshot:
        return _NO_PANEL_STATE if self._store is None else self._store.snapshot()

    def resolve(self, platform_account_id: str) -> AccountCaps:
        """`CapsConfigError` cuando la cuenta no tiene tope por ninguna de
        las dos vias: sin tope no se escribe, igual que hoy."""
        if self._caps.panel_managed is None:
            return self._caps.resolve(platform_account_id)
        resolution = self.resolution(platform_account_id)
        if resolution.effective is None:
            raise CapsConfigError(f"cuenta sin topes configurados: {platform_account_id!r}")
        behaviour = self._behaviour_defaults(resolution.platform_account_id)
        return AccountCaps(
            daily_cap_minor=resolution.effective.daily_cap_minor,
            monthly_cap_minor=resolution.effective.monthly_cap_minor,
            floor_minor=resolution.effective.floor_minor,
            ceiling_minor=resolution.effective.ceiling_minor,
            max_step_pct=behaviour.max_step_pct,
            max_changes_per_day=behaviour.max_changes_per_day,
            autonomy_enabled=behaviour.autonomy_enabled,
        )

    def resolution(self, platform_account_id: str) -> CapsResolution:
        """Vista completa (procedencia, recortes, sobre) para el panel y
        para `resolve_account_caps`."""
        canonical = _canonical_or_verbatim(platform_account_id)
        snapshot = self.state_snapshot()
        return resolve_effective_caps(
            platform_account_id=canonical,
            file_amounts=self._file_entry(platform_account_id, canonical),
            panel_amounts=snapshot.amounts_for(canonical) if snapshot.available else None,
            envelope=self._caps.panel_managed,
        )

    def _file_entry(self, platform_account_id: str, canonical: str) -> FileAmountsView | None:
        """Primero la forma EXACTA que escribio el operador -- un fichero
        sin sobre declarado puede tener claves no canonicas y se respeta tal
        cual -- y solo despues la canonica. `is None` explicito y no `or`:
        un modelo de pydantic siempre es verdadero, asi que el `or` solo
        parecia decidir algo."""
        verbatim = self._caps.accounts.get(platform_account_id)
        if verbatim is not None:
            return verbatim
        return self._caps.accounts.get(canonical)

    def _behaviour_defaults(self, canonical_account_id: str) -> AccountCaps:
        """`max_step_pct`, `max_changes_per_day` y `autonomy_enabled` NUNCA
        los fija el panel: salen de la entrada del fichero si existe, y si
        no de `defaults:`. La autonomia de una cuenta solo-panel es la que
        el operador declaro en su fichero, nunca una concedida desde la
        API.

        Recibe la clave CANONICA, la misma con la que se resolvio el
        efectivo (M-1): con `panel_managed` declarado, `CapsConfig` exige que
        las claves de `accounts:` ya vengan canonicas, asi que buscar por la
        forma que escribio el llamante (`123-456-7890`) no encontraria la
        entrada y la cuenta heredaria la autonomia de `defaults:` en vez de
        la suya."""
        try:
            return self._caps.resolve(canonical_account_id)
        except CapsConfigError:
            return AccountCaps(
                daily_cap_minor=0,
                monthly_cap_minor=0,
                floor_minor=0,
                ceiling_minor=0,
                max_step_pct=self._caps.defaults.max_step_pct,
                max_changes_per_day=self._caps.defaults.max_changes_per_day,
                autonomy_enabled=self._caps.defaults.autonomy_enabled,
            )


def _canonical_or_verbatim(platform_account_id: str) -> str:
    """Un id que ni siquiera tiene forma de id de cuenta no puede tener
    estado del panel (el broker nunca guardaria bajo esa clave), asi que se
    deja pasar tal cual: la resolucion caera al fichero o a "sin tope"."""
    try:
        return canonical_platform_account_id(platform_account_id)
    except InvalidPlatformAccountIdError:
        return platform_account_id
