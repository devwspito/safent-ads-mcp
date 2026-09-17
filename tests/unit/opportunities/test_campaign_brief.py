"""`CampaignBrief` (tasks.md T113/T114, FR-35/FR-36): un brief invalido no
debe poder representarse."""

from __future__ import annotations

import pytest

from safent_ads.opportunities.domain.campaign_brief import (
    CampaignBrief,
    CampaignBriefInvariantError,
)
from safent_ads.proposals.domain.money import Money
from safent_ads.shared.ids import PlatformCode


def _brief(**overrides: object) -> CampaignBrief:
    defaults: dict[str, object] = {
        "objective": "Cubrir demanda de Producto X",
        "platform": PlatformCode.GOOGLE,
        "offering_id": "offering-1",
        "daily_budget": Money.of("20.00", "EUR"),
        "duration_days": 7,
        "success_criterion": "CPL <= objetivo 3 dias seguidos",
        "kill_criterion": "Cero conversiones en 5 dias a 3x el CPL objetivo",
        "angle": "Cobertura de temporada",
        "targeting_seed": "producto x",
    }
    defaults.update(overrides)
    return CampaignBrief(**defaults)  # type: ignore[arg-type]


def test_valid_brief_is_accepted() -> None:
    brief = _brief()

    assert brief.duration_days == 7
    assert brief.daily_budget == Money.of("20.00", "EUR")


def test_rejects_budget_below_the_test_floor() -> None:
    with pytest.raises(CampaignBriefInvariantError, match="minimo de prueba"):
        _brief(daily_budget=Money.of("19.99", "EUR"))


def test_rejects_non_eur_budget() -> None:
    with pytest.raises(CampaignBriefInvariantError, match="EUR"):
        _brief(daily_budget=Money.of("50.00", "USD"))


@pytest.mark.parametrize("duration_days", [6, 15])
def test_rejects_duration_outside_bounds(duration_days: int) -> None:
    with pytest.raises(CampaignBriefInvariantError, match="duration_days"):
        _brief(duration_days=duration_days)


@pytest.mark.parametrize(
    "field_name", ["objective", "success_criterion", "kill_criterion", "angle", "targeting_seed"]
)
def test_rejects_blank_required_text(field_name: str) -> None:
    with pytest.raises(CampaignBriefInvariantError, match=field_name):
        _brief(**{field_name: "   "})
