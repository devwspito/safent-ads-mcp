"""`ADS_CAMPAIGN_PACKAGES_ENABLED` (composition/settings.py): gate de
lanzamiento para `propose_campaign_package`/`/api/v1/packages/**` de
escritura -- la saga de publicacion del paquete todavia no existe (Meta
falla con `PLATFORM_NATIVE_INCOMPLETE`) y Companion 0.2.21 se corta desde
esta rama. `False` por defecto: el arranque de siempre no cambia."""

from __future__ import annotations

from tests.unit.composition.factories import build_api_settings


def test_campaign_packages_are_disabled_by_default() -> None:
    settings = build_api_settings()

    assert settings.campaign_packages_enabled is False


def test_campaign_packages_can_be_switched_on() -> None:
    settings = build_api_settings(campaign_packages_enabled=True)

    assert settings.campaign_packages_enabled is True
