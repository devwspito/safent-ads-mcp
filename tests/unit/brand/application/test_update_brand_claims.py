"""`UpdateBrandClaims`: reemplaza entera la politica de reclamos y avisos
legales (rest-api.md §Marca, `PUT /brand/claims`); anota `decision_log` tras
un guardado con exito."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from safent_ads.brand.application.errors import BrandKitNotFoundError
from safent_ads.brand.application.update_brand_claims import (
    UpdateBrandClaims,
    UpdateBrandClaimsRequest,
)
from safent_ads.brand.domain.claims_policy import DEFAULT_FORBIDDEN_CLAIMS
from safent_ads.brand.domain.errors import AllowedClaimConflictsWithForbiddenError
from safent_ads.brand.domain.legal_disclaimer import LegalDisclaimer
from safent_ads.brand.testing.in_memory_brand_kit_repository import InMemoryBrandKitRepository
from safent_ads.brand.testing.recording_brand_claims_decision_recorder import (
    RecordingBrandClaimsDecisionRecorder,
)
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import BusinessId
from tests.unit.brand.factories import make_brand_kit

_NOW = datetime(2026, 9, 10, 9, 0, tzinfo=UTC)


def _use_case(
    *, existing_kit=None
) -> tuple[UpdateBrandClaims, InMemoryBrandKitRepository, RecordingBrandClaimsDecisionRecorder]:
    kits = InMemoryBrandKitRepository([existing_kit] if existing_kit else [])
    decision_recorder = RecordingBrandClaimsDecisionRecorder()
    use_case = UpdateBrandClaims(
        brand_kits=kits, clock=FixedClock(_NOW), decision_recorder=decision_recorder
    )
    return use_case, kits, decision_recorder


def _request(business_id: BusinessId, **overrides: object) -> UpdateBrandClaimsRequest:
    defaults: dict[str, object] = {
        "business_id": business_id,
        "actor_email": "owner@safent.example",
        "claims_allowlist": ("Envio gratis",),
        "forbidden_claims": ("mejor del mercado",),
        "legal_disclaimers": (LegalDisclaimer(text="Aviso legal nuevo"),),
    }
    defaults.update(overrides)
    return UpdateBrandClaimsRequest(**defaults)  # type: ignore[arg-type]


async def test_raises_when_no_kit_exists() -> None:
    use_case, _kits, _recorder = _use_case()

    with pytest.raises(BrandKitNotFoundError):
        await use_case.execute(_request(BusinessId.new()))


async def test_replaces_the_three_claim_fields_and_persists_the_kit() -> None:
    business_id = BusinessId.new()
    existing = make_brand_kit(business_id=business_id)
    use_case, kits, _recorder = _use_case(existing_kit=existing)

    kit = await use_case.execute(_request(business_id))

    assert kit.claims_allowlist == frozenset({"Envio gratis"})
    assert "mejor del mercado" in kit.forbidden_claims
    assert DEFAULT_FORBIDDEN_CLAIMS <= kit.forbidden_claims
    assert kit.legal_disclaimers == (LegalDisclaimer(text="Aviso legal nuevo"),)
    assert kit.updated_at == _NOW
    assert await kits.get_by_business(business_id) == kit


async def test_works_on_an_unconfirmed_draft_preview_kit() -> None:
    business_id = BusinessId.new()
    existing = make_brand_kit(business_id=business_id, is_confirmed=False)
    use_case, _kits, _recorder = _use_case(existing_kit=existing)

    kit = await use_case.execute(_request(business_id))

    assert kit.is_confirmed is False
    assert kit.claims_allowlist == frozenset({"Envio gratis"})


async def test_raises_on_conflict_between_allowlist_and_forbidden_claims() -> None:
    business_id = BusinessId.new()
    existing = make_brand_kit(business_id=business_id)
    use_case, _kits, recorder = _use_case(existing_kit=existing)

    with pytest.raises(AllowedClaimConflictsWithForbiddenError):
        await use_case.execute(
            _request(
                business_id,
                claims_allowlist=("Garantizado",),
                forbidden_claims=(),
            )
        )
    assert recorder.recorded == []


async def test_records_a_decision_after_a_successful_save() -> None:
    business_id = BusinessId.new()
    existing = make_brand_kit(business_id=business_id)
    use_case, _kits, recorder = _use_case(existing_kit=existing)

    await use_case.execute(_request(business_id))

    assert len(recorder.recorded) == 1
    assert recorder.recorded[0].business_id == business_id
    assert recorder.recorded[0].actor_email == "owner@safent.example"
