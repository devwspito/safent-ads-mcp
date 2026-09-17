"""`Typography`: familia, pesos y **la nota de licencia** (tool-surface.md
§6: "tipografias con licencia" es lo que el propietario debe aportar).
`licence_note` es obligatoria a proposito: una tipografia sin licencia
registrada no es segura de usar en anuncios comerciales."""

from __future__ import annotations

from dataclasses import dataclass

from safent_ads.brand.domain.errors import BlankFieldError


@dataclass(frozen=True, slots=True, kw_only=True)
class Typography:
    primary_family: str
    licence_note: str
    secondary_family: str | None = None
    weights: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.primary_family.strip():
            raise BlankFieldError("primary_family vacio")
        if not self.licence_note.strip():
            raise BlankFieldError("licence_note vacio: toda tipografia declara su licencia")
