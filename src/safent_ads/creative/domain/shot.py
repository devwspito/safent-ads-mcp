"""`ShotDescription` (creative-port.md: `CreativeBrief.shots: Sequence[ShotDescription]  # 3-5`)."""

from __future__ import annotations

from dataclasses import dataclass

_MAX_SHOT_DESCRIPTION_LEN = 280


class ShotDescriptionError(ValueError):
    """Descripcion de plano invalida."""


@dataclass(frozen=True, slots=True)
class ShotDescription:
    """Un plano dentro del guion visual del brief."""

    order: int
    description: str
    duration_s: int | None = None

    def __post_init__(self) -> None:
        if self.order < 1:
            raise ShotDescriptionError(f"order debe ser >= 1: {self.order!r}")
        if not self.description.strip():
            raise ShotDescriptionError("description vacia")
        if len(self.description) > _MAX_SHOT_DESCRIPTION_LEN:
            raise ShotDescriptionError(
                f"description supera {_MAX_SHOT_DESCRIPTION_LEN} caracteres"
            )
        if self.duration_s is not None and self.duration_s <= 0:
            raise ShotDescriptionError(f"duration_s debe ser positivo: {self.duration_s!r}")
