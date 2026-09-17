"""`CreativeBrief`: campos y limites exactos de `creative-port.md`
(shots 3-5), mas `content_hash()` para la idempotencia de `CreativeJob`
(data-model.md: UNIQUE `(brief_hash, variant_index)`)."""

from __future__ import annotations

import pytest

from safent_ads.creative.domain.brief import CreativeBriefError
from safent_ads.creative.domain.shot import ShotDescription
from safent_ads.shared.ids import BusinessId
from tests.unit.creative.domain.factories import make_brief, make_shots


def test_valid_brief_builds() -> None:
    brief = make_brief()

    assert brief.variant_count == 2


@pytest.mark.parametrize("shot_count", [0, 1, 2, 6, 10])
def test_shots_outside_3_to_5_raises(shot_count: int) -> None:
    with pytest.raises(CreativeBriefError):
        make_brief(shots=make_shots(shot_count))


@pytest.mark.parametrize("shot_count", [3, 4, 5])
def test_shots_within_3_to_5_is_valid(shot_count: int) -> None:
    brief = make_brief(shots=make_shots(shot_count))

    assert len(brief.shots) == shot_count


def test_empty_audience_summary_raises() -> None:
    with pytest.raises(CreativeBriefError):
        make_brief(audience_summary="   ")


def test_empty_cta_raises() -> None:
    with pytest.raises(CreativeBriefError):
        make_brief(cta="")


@pytest.mark.parametrize("variant_count", [0, -1, 9, 100])
def test_variant_count_out_of_bounds_raises(variant_count: int) -> None:
    with pytest.raises(CreativeBriefError):
        make_brief(variant_count=variant_count)


def test_on_screen_text_over_limit_raises() -> None:
    with pytest.raises(CreativeBriefError):
        make_brief(on_screen_text=["x" * 61])


def test_content_hash_is_stable_for_same_content() -> None:
    business_id = BusinessId.new()
    brief_a = make_brief(business_id=business_id)
    brief_b = make_brief(business_id=business_id)

    assert brief_a.content_hash() == brief_b.content_hash()


def test_content_hash_changes_with_content() -> None:
    business_id = BusinessId.new()
    brief_a = make_brief(business_id=business_id, hook="Gancho A")
    brief_b = make_brief(business_id=business_id, hook="Gancho B")

    assert brief_a.content_hash() != brief_b.content_hash()


def test_content_hash_is_sha256_hex() -> None:
    brief = make_brief()

    digest = brief.content_hash()

    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)


def test_shots_order_is_part_of_content() -> None:
    business_id = BusinessId.new()
    reordered = [
        ShotDescription(order=2, description="Plano 2"),
        ShotDescription(order=1, description="Plano 1"),
        ShotDescription(order=3, description="Plano 3"),
    ]
    brief_a = make_brief(business_id=business_id, shots=make_shots(3))
    brief_b = make_brief(business_id=business_id, shots=reordered)

    assert brief_a.content_hash() != brief_b.content_hash()
