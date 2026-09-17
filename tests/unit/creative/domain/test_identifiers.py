"""`AssetId`/`BriefId`/`JobId` son ULID: nombres de fichero aleatorios y
ordenables, nunca derivados del contenido (threat-model.md C-28)."""

from __future__ import annotations

import pytest

from safent_ads.creative.domain.identifiers import (
    AssetId,
    AssetIdFormatError,
    AssetRef,
    BriefId,
    JobId,
)


def test_new_asset_id_is_26_char_ulid() -> None:
    asset_id = AssetId.new()

    assert len(asset_id.value) == 26


def test_two_new_asset_ids_are_different() -> None:
    assert AssetId.new() != AssetId.new()


def test_parse_roundtrips_str() -> None:
    original = AssetId.new()

    parsed = AssetId.parse(str(original))

    assert parsed == original


@pytest.mark.parametrize("bad_value", ["", "not-a-ulid", "../../etc/passwd", "a" * 26])
def test_parse_rejects_non_ulid(bad_value: str) -> None:
    with pytest.raises(AssetIdFormatError):
        AssetId.parse(bad_value)


def test_asset_ref_is_asset_id() -> None:
    assert AssetRef is AssetId


def test_brief_id_and_job_id_are_ulid_too() -> None:
    assert len(BriefId.new().value) == 26
    assert len(JobId.new().value) == 26
