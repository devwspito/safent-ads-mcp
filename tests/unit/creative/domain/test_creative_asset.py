"""`CreativeAsset`: ciclo de vida DRAFT->READY->PROPOSED->APPROVED->PUBLISHED,
mas la regla invariable de `creative-port.md`: un activo con
`PolicyVerdict.FAIL` no puede proponerse — el agregado lo rechaza."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from safent_ads.creative.domain.creative_asset import (
    CreativeAsset,
    CreativeAssetError,
    InvalidCreativeAssetTransitionError,
    Provenance,
    UnpublishableCreativeAssetError,
)
from safent_ads.creative.domain.enums import (
    CreativeAssetState,
    CreativeOutcome,
    Format,
    GenerationStatus,
    MediaKind,
    PolicySeverity,
    PolicyVerdictResult,
    RendererName,
)
from safent_ads.creative.domain.identifiers import AssetId, BriefId, SignalId
from safent_ads.creative.domain.money import Money
from safent_ads.creative.domain.policy import PolicyFinding, PolicyVerdict
from safent_ads.creative.domain.storage import StorageUri
from safent_ads.shared.ids import BusinessId

_PASSING_VERDICT = PolicyVerdict(verdict=PolicyVerdictResult.PASS_, findings=())
_FAILING_VERDICT = PolicyVerdict(
    verdict=PolicyVerdictResult.FAIL,
    findings=(PolicyFinding(code="X", severity=PolicySeverity.FAIL, human_message="x"),),
)


def _make_asset(**overrides: object) -> CreativeAsset:
    defaults: dict[str, object] = {
        "asset_id": AssetId.new(),
        "business_id": BusinessId.new(),
        "media_kind": MediaKind.IMAGE,
        "format": Format.SQUARE_1080,
        "duration_seconds": None,
        "storage_uri": StorageUri("img/x.png"),
        "checksum": "a" * 64,
        "cost_estimate": Money(Decimal("0.04"), "USD"),
        "provenance": Provenance(
            renderer_used=RendererName.QWEN_IMAGE_2512,
            model_name="qwen-image-2512-lightning-4step",
            seed=42,
            brief_id=BriefId.new(),
            source_signal_id=SignalId(uuid.uuid4()),
            generation_status=GenerationStatus.MODEL_GENERATED,
            generated_at=datetime(2026, 1, 1, tzinfo=UTC),
        ),
    }
    defaults.update(overrides)
    return CreativeAsset(**defaults)  # type: ignore[arg-type]


def test_starts_in_draft() -> None:
    asset = _make_asset()

    assert asset.state == CreativeAssetState.DRAFT


def test_rejects_non_sha256_checksum() -> None:
    with pytest.raises(CreativeAssetError):
        _make_asset(checksum="not-a-checksum")


def test_mark_ready_with_passing_verdict_transitions_to_ready() -> None:
    asset = _make_asset()

    asset.mark_ready(_PASSING_VERDICT)

    assert asset.state == CreativeAssetState.READY


def test_mark_ready_with_failing_verdict_transitions_to_rejected() -> None:
    asset = _make_asset()

    asset.mark_ready(_FAILING_VERDICT)

    assert asset.state == CreativeAssetState.REJECTED


def test_propose_without_policy_check_raises() -> None:
    asset = _make_asset()

    with pytest.raises(UnpublishableCreativeAssetError):
        asset.propose()


def test_propose_after_failing_verdict_raises() -> None:
    asset = _make_asset()
    asset.mark_ready(_FAILING_VERDICT)

    with pytest.raises(UnpublishableCreativeAssetError):
        asset.propose()


def test_full_lifecycle_to_published() -> None:
    asset = _make_asset()

    asset.mark_ready(_PASSING_VERDICT)
    asset.propose()
    asset.approve()
    asset.mark_published()

    assert asset.state == CreativeAssetState.PUBLISHED


def test_reject_from_proposed() -> None:
    asset = _make_asset()
    asset.mark_ready(_PASSING_VERDICT)
    asset.propose()

    asset.reject()

    assert asset.state == CreativeAssetState.REJECTED


def test_cannot_publish_before_approval() -> None:
    asset = _make_asset()
    asset.mark_ready(_PASSING_VERDICT)
    asset.propose()

    with pytest.raises(InvalidCreativeAssetTransitionError):
        asset.mark_published()


def test_record_outcome_before_published_raises() -> None:
    asset = _make_asset()

    with pytest.raises(InvalidCreativeAssetTransitionError):
        asset.record_outcome(CreativeOutcome.WINNER)


def test_record_outcome_after_published() -> None:
    asset = _make_asset()
    asset.mark_ready(_PASSING_VERDICT)
    asset.propose()
    asset.approve()
    asset.mark_published()

    asset.record_outcome(CreativeOutcome.WINNER)

    assert asset.outcome == CreativeOutcome.WINNER


def test_traceability_carries_signal_brief_and_asset() -> None:
    asset = _make_asset()

    trace = asset.traceability

    assert trace.asset_id == asset.asset_id
    assert trace.brief_id == asset.provenance.brief_id
    assert trace.outcome == CreativeOutcome.PENDING


def test_rejected_is_terminal() -> None:
    asset = _make_asset()
    asset.mark_ready(_FAILING_VERDICT)

    with pytest.raises(UnpublishableCreativeAssetError):
        asset.propose()

    with pytest.raises(InvalidCreativeAssetTransitionError):
        asset.approve()


def test_generated_at_field_on_provenance_not_required_here() -> None:
    # provenance carries model/seed/brief/signal; generated_at lives on
    # RenderedAsset (creative-port.md), not duplicated on CreativeAsset.
    asset = _make_asset()

    assert asset.provenance.model_name == "qwen-image-2512-lightning-4step"
    assert asset.provenance.seed == 42
