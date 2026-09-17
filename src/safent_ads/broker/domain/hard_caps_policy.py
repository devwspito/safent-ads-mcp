"""Politica PURA de topes duros por cuenta (spec 008 `plan.md` §5,
`data-model.md` §`AccountHardCap`). Sin framework, sin disco, sin reloj:
solo la aritmetica de la que cuelga todo el resto.

**El teorema.** El tope efectivo es `min(entrada de fichero, tope del
panel)` campo a campo, y una cuenta solo-panel sin estado queda
`writable=False`. De ahi que **ignorar el estado del panel sea siempre
igual o mas restrictivo que aplicarlo** -- y de ahi, sin ningun caso
especial, todos los modos de fallo: estado corrupto, ilegible, con version
desconocida o con el sobre retirado del fichero se resuelven cayendo al
fichero, nunca al valor del panel. Quien llame a `resolve_effective_caps`
sin estado del panel obtiene exactamente el comportamiento de hoy.

**El sobre nunca acota las entradas del fichero.** Una cuenta declarada en
`accounts:` puede estar por encima de cualquier campo del sobre: el fichero
es la autoridad y el sobre solo delimita lo que el panel puede repartir.

**El suelo no lo toca el panel** (revision T027, C2): es una cota inferior
y no admite resolucion segura en las dos direcciones -- subirlo bloquea las
bajadas defensivas de presupuesto, bajarlo relaja lo que declaro el
operador. Viene del fichero; en una cuenta solo-panel, de
`envelope.min_floor_minor`."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

__all__ = [
    "CapSource",
    "CapsResolution",
    "EffectiveAmounts",
    "FileAmountsView",
    "PanelAmounts",
    "SpendEnvelopeView",
    "resolve_effective_caps",
]

# Los TRES campos que el panel fija, en el orden en que se muestran.
PANEL_SETTABLE_FIELDS: tuple[str, ...] = (
    "daily_cap_minor",
    "monthly_cap_minor",
    "ceiling_minor",
)


class CapSource(StrEnum):
    """De donde sale el tope efectivo de una cuenta."""

    FILE = "file"
    PANEL = "panel"
    FILE_AND_PANEL = "file_and_panel"
    NONE = "none"


class SpendEnvelopeView(Protocol):
    """Lo que esta politica necesita del sobre. `SpendEnvelope`
    (`broker/infrastructure/caps_config.py`) lo satisface estructuralmente:
    el dominio no importa el modelo de configuracion."""

    @property
    def max_daily_cap_minor(self) -> int: ...
    @property
    def max_monthly_cap_minor(self) -> int: ...
    @property
    def max_ceiling_minor(self) -> int: ...
    @property
    def min_floor_minor(self) -> int: ...


class FileAmountsView(Protocol):
    """Los cuatro importes de una entrada de `accounts:`."""

    @property
    def daily_cap_minor(self) -> int: ...
    @property
    def monthly_cap_minor(self) -> int: ...
    @property
    def floor_minor(self) -> int: ...
    @property
    def ceiling_minor(self) -> int: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class PanelAmounts:
    """Los tres importes que el panel fija, y solo esos tres."""

    daily_cap_minor: int
    monthly_cap_minor: int
    ceiling_minor: int


@dataclass(frozen=True, slots=True, kw_only=True)
class EffectiveAmounts:
    """Lo que el broker aplicara, no lo que se guardo."""

    daily_cap_minor: int
    monthly_cap_minor: int
    floor_minor: int
    ceiling_minor: int


@dataclass(frozen=True, slots=True, kw_only=True)
class CapsResolution:
    platform_account_id: str
    source: CapSource
    writable: bool
    effective: EffectiveAmounts | None
    clamped_by: tuple[str, ...]
    from_file: EffectiveAmounts | None
    from_panel: PanelAmounts | None


def resolve_effective_caps(
    *,
    platform_account_id: str,
    file_amounts: FileAmountsView | None,
    panel_amounts: PanelAmounts | None,
    envelope: SpendEnvelopeView | None,
) -> CapsResolution:
    """Tope efectivo de una cuenta ya canonicalizada.

    `envelope is None` ⇒ el estado del panel se ignora ENTERO, aunque
    `panel_amounts` venga con valores: sin sobre declarado el panel no
    puede fijar nada, y un sobre retirado del fichero no puede dejar vivo
    lo que el panel fijo mientras existia."""
    from_file = _file_amounts(file_amounts)
    if envelope is None or panel_amounts is None:
        return _file_only_resolution(platform_account_id, from_file)

    clamped = _clamp_to_envelope(panel_amounts, envelope)
    if from_file is None:
        return _panel_only_resolution(platform_account_id, panel_amounts, clamped, envelope)
    return _combined_resolution(platform_account_id, panel_amounts, clamped, from_file)


def _file_only_resolution(
    platform_account_id: str, from_file: EffectiveAmounts | None
) -> CapsResolution:
    return CapsResolution(
        platform_account_id=platform_account_id,
        source=CapSource.FILE if from_file is not None else CapSource.NONE,
        writable=from_file is not None,
        effective=from_file,
        clamped_by=(),
        from_file=from_file,
        from_panel=None,
    )


def _panel_only_resolution(
    platform_account_id: str,
    panel_amounts: PanelAmounts,
    clamped: PanelAmounts,
    envelope: SpendEnvelopeView,
) -> CapsResolution:
    return CapsResolution(
        platform_account_id=platform_account_id,
        source=CapSource.PANEL,
        writable=True,
        effective=EffectiveAmounts(
            daily_cap_minor=clamped.daily_cap_minor,
            monthly_cap_minor=clamped.monthly_cap_minor,
            # El suelo de una cuenta solo-panel lo declara el operador en
            # el fichero, nunca el panel.
            floor_minor=envelope.min_floor_minor,
            ceiling_minor=clamped.ceiling_minor,
        ),
        clamped_by=_clamped_fields(panel_amounts, clamped),
        from_file=None,
        from_panel=panel_amounts,
    )


def _combined_resolution(
    platform_account_id: str,
    panel_amounts: PanelAmounts,
    clamped: PanelAmounts,
    from_file: EffectiveAmounts,
) -> CapsResolution:
    effective = PanelAmounts(
        daily_cap_minor=min(clamped.daily_cap_minor, from_file.daily_cap_minor),
        monthly_cap_minor=min(clamped.monthly_cap_minor, from_file.monthly_cap_minor),
        ceiling_minor=min(clamped.ceiling_minor, from_file.ceiling_minor),
    )
    return CapsResolution(
        platform_account_id=platform_account_id,
        source=CapSource.FILE_AND_PANEL,
        writable=True,
        effective=EffectiveAmounts(
            daily_cap_minor=effective.daily_cap_minor,
            monthly_cap_minor=effective.monthly_cap_minor,
            floor_minor=from_file.floor_minor,
            ceiling_minor=effective.ceiling_minor,
        ),
        clamped_by=_clamped_fields(panel_amounts, effective),
        from_file=from_file,
        from_panel=panel_amounts,
    )


def _file_amounts(file_amounts: FileAmountsView | None) -> EffectiveAmounts | None:
    if file_amounts is None:
        return None
    return EffectiveAmounts(
        daily_cap_minor=file_amounts.daily_cap_minor,
        monthly_cap_minor=file_amounts.monthly_cap_minor,
        floor_minor=file_amounts.floor_minor,
        ceiling_minor=file_amounts.ceiling_minor,
    )


def _clamp_to_envelope(panel: PanelAmounts, envelope: SpendEnvelopeView) -> PanelAmounts:
    """Revalidacion contra el sobre AL APLICAR, no solo al guardar: un
    fichero de estado manipulado a mano no puede superar el sobre (i2)."""
    return PanelAmounts(
        daily_cap_minor=min(panel.daily_cap_minor, envelope.max_daily_cap_minor),
        monthly_cap_minor=min(panel.monthly_cap_minor, envelope.max_monthly_cap_minor),
        ceiling_minor=min(panel.ceiling_minor, envelope.max_ceiling_minor),
    )


def _clamped_fields(stored: PanelAmounts, applied: PanelAmounts) -> tuple[str, ...]:
    return tuple(
        field for field in PANEL_SETTABLE_FIELDS if getattr(applied, field) < getattr(stored, field)
    )
