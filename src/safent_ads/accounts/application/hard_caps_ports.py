"""Puerto hacia los tres `op` de topes del broker (spec 008 T031,
`contracts/broker-set-account-caps.schema.json`): `set_account_caps`,
`delete_account_caps` y `resolve_account_caps`.

`ads-api` **no escribe** el estado de topes, y **tampoco lo lee**: no tiene
el directorio montado. Todo lo que sabe de topes llega por este puerto, y
quien decide al otro lado es `ads-broker`. Por eso ninguna forma de este
modulo puede representar "un tope que el broker no ha aceptado": lo que
vuelve es siempre el efectivo ya resuelto y ya recortado."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal, Protocol

from safent_ads.shared.ids import BusinessId

__all__ = [
    "FILE_AND_PANEL",
    "AccountHardCapsView",
    "CapAmountsView",
    "CapSourceName",
    "EffectiveAmountsView",
    "EnvelopeView",
    "HardCapsBrokerPort",
    "PanelCapsInput",
    "PlatformAccountDirectoryPort",
]

# El vocabulario de `source` del contrato, en un tipo y no en cadenas
# sueltas: una comparacion con un valor que el broker nunca manda deja de
# compilar en vez de ser siempre falsa -- y siempre falsa, aqui, significa
# "no pedir la prueba de presencia que tocaba".
type CapSourceName = Literal["file", "panel", "file_and_panel", "none"]

# El unico `source` en el que retirar el tope del panel SUBE el efectivo:
# queda la entrada del fichero, que es mayor o igual (revision T027, C3).
FILE_AND_PANEL: Final[CapSourceName] = "file_and_panel"


@dataclass(frozen=True, slots=True, kw_only=True)
class PanelCapsInput:
    """Los TRES importes que el panel fija, mas la divisa de confirmacion.
    `floor_minor` no esta aqui a proposito (revision T027, C2): es una cota
    inferior sin resolucion segura en las dos direcciones."""

    daily_cap_minor: int
    monthly_cap_minor: int
    ceiling_minor: int
    currency: str


@dataclass(frozen=True, slots=True, kw_only=True)
class CapAmountsView:
    """Los TRES importes que el panel fija, tal como los declara cada
    fuente: sin recortar y sin resolver. La divisa no va aqui -- es una sola
    para toda la vista (`envelope.currency`), y repetirla por fuente
    insinuaria que pueden diferir."""

    daily_cap_minor: int
    monthly_cap_minor: int
    ceiling_minor: int


@dataclass(frozen=True, slots=True, kw_only=True)
class EffectiveAmountsView:
    daily_cap_minor: int
    monthly_cap_minor: int
    floor_minor: int
    ceiling_minor: int


@dataclass(frozen=True, slots=True, kw_only=True)
class EnvelopeView:
    max_daily_cap_minor: int
    max_monthly_cap_minor: int
    max_ceiling_minor: int
    min_floor_minor: int
    max_accounts: int
    accounts_used: int
    max_cap_changes_per_day: int
    cap_changes_today: int
    currency: str


@dataclass(frozen=True, slots=True, kw_only=True)
class AccountHardCapsView:
    """Lo que el broker responde: el efectivo, de donde viene y que quedo
    recortado. `envelope is None` significa que el despliegue no declara
    `panel_managed` -- el panel no puede fijar nada."""

    platform_account_id: str
    source: CapSourceName
    writable: bool
    effective: EffectiveAmountsView | None
    clamped_by: tuple[str, ...]
    panel_state_available: bool
    envelope: EnvelopeView | None
    # Lo GUARDADO a cada lado, que no siempre es lo que se aplica: con los
    # dos, la pantalla puede decir «guardado X, en vigor Y» en un campo
    # recortado en vez de dejar al dueno adivinando cual de los dos ve.
    from_file: CapAmountsView | None = None
    from_panel: CapAmountsView | None = None


class HardCapsBrokerPort(Protocol):
    async def resolve_account_caps(self, platform_account_id: str) -> AccountHardCapsView: ...

    async def set_account_caps(
        self,
        platform_account_id: str,
        caps: PanelCapsInput,
        *,
        requested_by: str,
        request_id: str | None = None,
    ) -> AccountHardCapsView: ...

    async def delete_account_caps(
        self, platform_account_id: str, *, requested_by: str, request_id: str | None = None
    ) -> AccountHardCapsView: ...


class PlatformAccountDirectoryPort(Protocol):
    """Alcance de la sesion: `None` significa "esta sesion no llega a esa
    cuenta", y la respuesta es **404, nunca 403** -- la regla vigente del
    repositorio para no filtrar existencia
    (`iam/presentation/dependencies.py`). El negocio que devuelve es el que
    firma la entrada del registro de decisiones, para que el cambio aparezca
    en el historial al que pertenece."""

    async def business_of(self, platform_account_id: str) -> BusinessId | None: ...
