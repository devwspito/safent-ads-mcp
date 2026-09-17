"""`PackageStepBinding` -- lo que viaja firmado con cada paso (T102,
data-model.md "Revision 2" §R2.3). Determinismo de `derive_step_binding` y
que todo campo salvo `parent_entity_ref` sea derivable **solo** del sobre
firmado (sin volver a consultar el paquete vivo)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from safent_ads.packages.domain.approval_envelope import StepKind, build_approval_envelope
from safent_ads.packages.domain.step_binding import StepBindingDerivationError, derive_step_binding
from safent_ads.shared.ids import EntityLevel, EntityRef, PlatformCode

from .conftest import NOW, meta_ad, meta_ad_set, propose_meta_package


def _envelope(package):
    return build_approval_envelope(
        package=package,
        publication_id="pub-1",
        approved_by="owner-1",
        approved_at=NOW,
        approval_expires_at=NOW + timedelta(minutes=30),
    )


def _entity_ref(external_id: str) -> EntityRef:
    return EntityRef(PlatformCode.META, EntityLevel.CAMPAIGN, external_id)


def _index_of(envelope, kind: StepKind) -> int:
    return next(i for i, step in enumerate(envelope.step_plan) if step.step_kind is kind)


class TestDerivationIsPure:
    def test_same_envelope_and_index_produce_the_same_binding(self) -> None:
        package = propose_meta_package()
        envelope = _envelope(package)
        campaign_ref = _entity_ref("123")

        first = derive_step_binding(envelope, "envelope-hash-1", 1, campaign_ref)
        second = derive_step_binding(envelope, "envelope-hash-1", 1, campaign_ref)

        assert first == second

    def test_binding_does_not_depend_on_the_live_package_only_on_the_envelope(self) -> None:
        # Dos paquetes distintos que produzcan el mismo sobre (mismo hash,
        # mismo plan) deben derivar el mismo binding -- la firma es lo
        # unico que cuenta, nunca un segundo vistazo al arbol vivo.
        package = propose_meta_package()
        envelope_a = _envelope(package)
        envelope_b = _envelope(package)

        binding_a = derive_step_binding(envelope_a, "hash", 0, None)
        binding_b = derive_step_binding(envelope_b, "hash", 0, None)

        assert binding_a == binding_b


class TestFieldsAreDerivedFromTheSignedEnvelope:
    def test_upload_creative_step_has_no_parent(self) -> None:
        package = propose_meta_package()
        envelope = _envelope(package)
        upload_index = _index_of(envelope, StepKind.UPLOAD_CREATIVE)

        binding = derive_step_binding(envelope, "hash", upload_index, None)

        assert binding.parent_local_ref is None
        assert binding.parent_step_index is None
        assert binding.parent_entity_ref is None

    def test_create_ad_set_step_parent_is_the_create_campaign_step(self) -> None:
        package = propose_meta_package()
        envelope = _envelope(package)
        campaign_ref = _entity_ref("123")
        ad_set_index = _index_of(envelope, StepKind.CREATE_AD_SET)
        campaign_index = _index_of(envelope, StepKind.CREATE_CAMPAIGN)

        binding = derive_step_binding(envelope, "hash", ad_set_index, campaign_ref)

        assert binding.parent_step_index == campaign_index
        assert binding.parent_entity_ref == campaign_ref

    def test_parent_receipt_state_hash_is_none_by_default(self) -> None:
        """T123/AL-4: sin argumento explicito, el binding no inventa una
        precondicion -- `parent_receipt_state_hash` queda `None`, y
        `ChokepointStepExecutor` es quien falla cerrado con eso."""
        package = propose_meta_package()
        envelope = _envelope(package)
        ad_set_index = _index_of(envelope, StepKind.CREATE_AD_SET)

        binding = derive_step_binding(envelope, "hash", ad_set_index, _entity_ref("123"))

        assert binding.parent_receipt_state_hash is None

    def test_parent_receipt_state_hash_is_carried_from_the_caller(self) -> None:
        """T123/AL-4: llega tal cual, del MISMO recibo confirmado del que
        sale `parent_entity_ref` -- nunca derivado del sobre."""
        package = propose_meta_package()
        envelope = _envelope(package)
        ad_set_index = _index_of(envelope, StepKind.CREATE_AD_SET)

        binding = derive_step_binding(
            envelope, "hash", ad_set_index, _entity_ref("123"), "state-after"
        )

        assert binding.parent_receipt_state_hash == "state-after"

    def test_create_ad_step_carries_its_upload_creative_dependency(self) -> None:
        ad = meta_ad(checksum="a" * 64)
        package = propose_meta_package(ad_sets=(meta_ad_set(ads=(ad,)),))
        envelope = _envelope(package)
        ad_index = _index_of(envelope, StepKind.CREATE_AD)

        binding = derive_step_binding(envelope, "hash", ad_index, _entity_ref("as-1"))

        assert len(binding.creative_sources) == 1
        upload_ref = binding.creative_sources[0]
        assert any(
            step.local_ref == upload_ref and step.step_kind is StepKind.UPLOAD_CREATIVE
            for step in envelope.step_plan
        )

    def test_activate_campaign_expects_every_planned_ad(self) -> None:
        package = propose_meta_package()
        envelope = _envelope(package)
        activate_index = len(envelope.step_plan) - 1

        binding = derive_step_binding(envelope, "hash", activate_index, _entity_ref("123"))

        assert binding.expected_done_steps == 1

    def test_out_of_range_step_index_is_rejected(self) -> None:
        package = propose_meta_package()
        envelope = _envelope(package)

        with pytest.raises(StepBindingDerivationError):
            derive_step_binding(envelope, "hash", len(envelope.step_plan), None)

    def test_to_canonical_stringifies_optional_refs(self) -> None:
        package = propose_meta_package()
        envelope = _envelope(package)

        binding = derive_step_binding(envelope, "hash", 0, None)
        canonical = binding.to_canonical()

        assert canonical["parent_entity_ref"] is None
        assert canonical["parent_receipt_state_hash"] is None
        assert canonical["account_ref"] == str(envelope.account_ref)
