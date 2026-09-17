"""`CreativeJob`: QUEUED -> RENDERING -> COMPOSING -> CHECKING -> READY |
FAILED | FALLBACK_CLOUD (data-model.md), mas la clave de idempotencia
`brief_hash + variant_index` (T098 `test_creative_job_idempotent`)."""

from __future__ import annotations

import pytest

from safent_ads.creative.domain.creative_job import (
    CreativeJob,
    CreativeJobError,
    CreativeJobIdempotencyKey,
    InvalidCreativeJobTransitionError,
)
from safent_ads.creative.domain.enums import CreativeJobState
from safent_ads.creative.domain.identifiers import AssetId, BriefId, JobId
from safent_ads.shared.ids import BusinessId


def _make_job() -> CreativeJob:
    return CreativeJob(
        job_id=JobId.new(),
        business_id=BusinessId.new(),
        brief_id=BriefId.new(),
        idempotency_key=CreativeJobIdempotencyKey(brief_hash="a" * 64, variant_index=0),
    )


def test_idempotency_key_string_form() -> None:
    key = CreativeJobIdempotencyKey(brief_hash="abc", variant_index=1)

    assert str(key) == "abc:1"


def test_idempotency_key_rejects_negative_variant_index() -> None:
    with pytest.raises(CreativeJobError):
        CreativeJobIdempotencyKey(brief_hash="abc", variant_index=-1)


def test_two_jobs_same_brief_hash_and_variant_share_idempotency_key() -> None:
    key_a = CreativeJobIdempotencyKey(brief_hash="same", variant_index=0)
    key_b = CreativeJobIdempotencyKey(brief_hash="same", variant_index=0)

    assert key_a == key_b


def test_starts_queued() -> None:
    job = _make_job()

    assert job.state == CreativeJobState.QUEUED
    assert not job.is_terminal


def test_full_happy_path_to_ready() -> None:
    job = _make_job()
    asset_ids = (AssetId.new(),)

    job.start_rendering()
    job.start_composing()
    job.start_checking()
    job.mark_ready(asset_ids)

    assert job.state == CreativeJobState.READY
    assert job.asset_ids == asset_ids
    assert job.progress == 1.0
    assert job.is_terminal


def test_checking_can_fall_back_to_cloud() -> None:
    job = _make_job()
    job.start_rendering()
    job.start_composing()
    job.start_checking()

    job.mark_fallback_cloud((AssetId.new(),))

    assert job.state == CreativeJobState.FALLBACK_CLOUD


def test_cannot_skip_rendering() -> None:
    job = _make_job()

    with pytest.raises(InvalidCreativeJobTransitionError):
        job.start_composing()


def test_mark_failed_from_any_non_terminal_state() -> None:
    job = _make_job()
    job.start_rendering()

    job.mark_failed("timeout de ComfyUI")

    assert job.state == CreativeJobState.FAILED
    assert job.failure_reason == "timeout de ComfyUI"


def test_cancel_marks_failed() -> None:
    job = _make_job()
    job.start_rendering()

    job.cancel()

    assert job.state == CreativeJobState.FAILED
    assert job.failure_reason == "cancelado"


def test_terminal_job_rejects_further_transitions() -> None:
    job = _make_job()
    job.start_rendering()
    job.mark_failed("x")

    with pytest.raises(InvalidCreativeJobTransitionError):
        job.start_composing()


def test_update_progress_rejects_out_of_range() -> None:
    job = _make_job()

    with pytest.raises(CreativeJobError):
        job.update_progress(1.5)


def test_update_progress_on_terminal_job_raises() -> None:
    job = _make_job()
    job.start_rendering()
    job.mark_failed("x")

    with pytest.raises(InvalidCreativeJobTransitionError):
        job.update_progress(0.5)


def test_mark_ready_requires_at_least_one_asset() -> None:
    job = _make_job()
    job.start_rendering()
    job.start_composing()
    job.start_checking()

    with pytest.raises(CreativeJobError):
        job.mark_ready(())
