from __future__ import annotations

import pytest

from safent_ads.creative.domain.shot import ShotDescription, ShotDescriptionError


def test_valid_shot() -> None:
    shot = ShotDescription(order=1, description="Plano general del aula", duration_s=3)

    assert shot.order == 1


def test_rejects_order_below_one() -> None:
    with pytest.raises(ShotDescriptionError):
        ShotDescription(order=0, description="x")


def test_rejects_empty_description() -> None:
    with pytest.raises(ShotDescriptionError):
        ShotDescription(order=1, description="   ")


def test_rejects_non_positive_duration() -> None:
    with pytest.raises(ShotDescriptionError):
        ShotDescription(order=1, description="x", duration_s=0)
