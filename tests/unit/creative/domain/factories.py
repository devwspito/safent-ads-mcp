"""Constructores minimos reutilizados por los tests de dominio de `creative`.
No es un fixture de pytest: son funciones puras que devuelven un objeto
valido por defecto, para que cada test solo toque el campo que le importa."""

from __future__ import annotations

from decimal import Decimal

from safent_ads.creative.domain.brand_kit import BrandKit, SafeArea
from safent_ads.creative.domain.brief import CreativeBrief
from safent_ads.creative.domain.copy import AdCopy
from safent_ads.creative.domain.enums import CallToAction, CampaignObjective
from safent_ads.creative.domain.identifiers import AssetId
from safent_ads.creative.domain.money import Money
from safent_ads.creative.domain.shot import ShotDescription
from safent_ads.shared.ids import BusinessId


def make_brand_kit() -> BrandKit:
    return BrandKit(
        primary_font="Inter",
        secondary_font="Inter",
        primary_color_hex="#1A2B3C",
        secondary_color_hex="#FFFFFF",
        logo_asset_id=AssetId.new(),
        safe_area=SafeArea.reels_default(),
    )


def make_shots(count: int = 3) -> list[ShotDescription]:
    return [ShotDescription(order=i + 1, description=f"Plano {i + 1}") for i in range(count)]


def make_brief(**overrides: object) -> CreativeBrief:
    defaults: dict[str, object] = {
        "business_id": BusinessId.new(),
        "calendar_event_id": None,
        "objective": CampaignObjective.LEAD,
        "audience_summary": "Adultos 25-45, interesados en el producto",
        "hook": "Tu plaza empieza aqui",
        "shots": make_shots(),
        "on_screen_text": ["Plazas limitadas"],
        "cta": "Apúntate ya",
        "voiceover_lines": ["Prepárate con Negocio Ejemplo."],
        "brand_kit": make_brand_kit(),
        "source_signal_id": None,
        "variant_count": 2,
    }
    defaults.update(overrides)
    return CreativeBrief(**defaults)  # type: ignore[arg-type]


def make_ad_copy(**overrides: object) -> AdCopy:
    defaults: dict[str, object] = {
        "headline": "Tu plaza empieza aquí",
        "primary_text": "Prepárate con Negocio Ejemplo y consigue tu plaza en el próximo"
        " lanzamiento.",
        "cta": CallToAction.SIGN_UP,
    }
    defaults.update(overrides)
    return AdCopy(**defaults)  # type: ignore[arg-type]


def make_money(amount: str = "0.04", currency: str = "USD") -> Money:
    return Money(Decimal(amount), currency)
