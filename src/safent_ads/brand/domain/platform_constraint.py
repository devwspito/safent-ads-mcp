"""`PlatformConstraint`: limites de marca especificos de una plataforma
(p.ej. Google Display exige mas margen de seguridad de texto que Meta
Stories). Distinto de `run_creative_policy_check`
(tool-surface.md §2.2, fuera de este lane): eso valida una pieza ya
compuesta contra las normas de la plataforma; esto declara la preferencia
de marca por plataforma antes de componer nada."""

from __future__ import annotations

from dataclasses import dataclass

from safent_ads.shared.ids import PlatformCode


@dataclass(frozen=True, slots=True, kw_only=True)
class PlatformConstraint:
    platform: PlatformCode
    max_headline_chars: int | None = None
    requires_disclaimer: bool = False
    notes: str = ""
