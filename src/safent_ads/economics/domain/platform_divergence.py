"""`PlatformDivergence` (profitability-engine.md §2): el CRM es la verdad;
la plataforma sirve para pujar y como senal temprana. `delta_hat` traduce,
nunca corrige gasto -- un salto es anomalia de medicion (nodo 1 de
diagnostico), no caida de rendimiento."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

DEFAULT_SHRINKAGE_M = 10
SANITY_BAND_LOW = 0.6
SANITY_BAND_HIGH = 1.6
JUMP_ANOMALY_RELATIVE_THRESHOLD = 0.30
UNATTRIBUTED_SHARE_BLOCK_THRESHOLD = 0.35
_CLOSED_WINDOW_WEEKS = 8


_DEFAULT_WINDOW_START = date(1970, 1, 1)
_DEFAULT_WINDOW_END = date(1970, 1, 2)


@dataclass(frozen=True, slots=True)
class PlatformDivergence:
    """`delta_hat = (crm_business_conversions + m) / (platform_conversions + m)`,
    encogido hacia 1,0 por cuenta sobre 8 semanas cerradas
    (`window_start`/`window_end`)."""

    crm_conversions: int
    platform_conversions: int
    shrinkage_m: int
    value: float
    window_start: date = _DEFAULT_WINDOW_START
    window_end: date = _DEFAULT_WINDOW_END

    def __post_init__(self) -> None:
        if self.crm_conversions < 0 or self.platform_conversions < 0:
            raise ValueError("crm_conversions y platform_conversions deben ser >= 0")
        if self.shrinkage_m <= 0:
            raise ValueError(f"shrinkage_m debe ser > 0: {self.shrinkage_m}")
        if self.window_start >= self.window_end:
            raise ValueError(f"window_start {self.window_start} >= window_end {self.window_end}")

    @classmethod
    def compute(
        cls,
        *,
        crm_conversions: int,
        platform_conversions: int,
        shrinkage_m: int = DEFAULT_SHRINKAGE_M,
        window_start: date = _DEFAULT_WINDOW_START,
        window_end: date = _DEFAULT_WINDOW_END,
    ) -> PlatformDivergence:
        value = (crm_conversions + shrinkage_m) / (platform_conversions + shrinkage_m)
        return cls(
            crm_conversions=crm_conversions,
            platform_conversions=platform_conversions,
            shrinkage_m=shrinkage_m,
            value=value,
            window_start=window_start,
            window_end=window_end,
        )

    @property
    def is_outside_sanity_band(self) -> bool:
        """Nodo 1 de diagnostico: fuera de `[0.6, 1.6]` -- congelar BUY."""
        return not (SANITY_BAND_LOW <= self.value <= SANITY_BAND_HIGH)

    def is_jump_anomaly(
        self, previous: PlatformDivergence, *, threshold: float = JUMP_ANOMALY_RELATIVE_THRESHOLD
    ) -> bool:
        """Salto > 30% frente al periodo anterior: anomalia de medicion,
        no caida de rendimiento (profitability-engine.md §2)."""
        if previous.value == 0:
            return self.value != 0
        return abs(self.value - previous.value) / previous.value > threshold


def closed_eight_week_window(today: date) -> tuple[date, date]:
    """Ventana de reconciliacion (profitability-engine.md §2: 'escalada por
    cuenta sobre las 8 semanas cerradas'): termina en el lunes de la semana
    en curso (excluyente) para no mezclar dias todavia sin cerrar."""
    window_end = today - timedelta(days=today.weekday())
    window_start = window_end - timedelta(weeks=_CLOSED_WINDOW_WEEKS)
    return window_start, window_end


def exceeds_unattributed_share_threshold(
    unattributed_share: float, *, threshold: float = UNATTRIBUTED_SHARE_BLOCK_THRESHOLD
) -> bool:
    """`AGGREGATE` por encima de 0,35 bloquea las subidas (profitability-
    engine.md §2)."""
    return unattributed_share > threshold
