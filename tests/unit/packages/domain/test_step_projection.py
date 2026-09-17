"""`approval_envelope.project_step_template`/`project_step` (T011,
data-model.md "Revision 2" §R2.2). Determinismo, proyeccion exacta por
tipo de paso, y la garantia central: **ningun** campo del paso proyectado
puede venir de fuera del arbol firmado, salvo el `entity_ref` del padre --
que ademas nunca viaja dentro de la carga (viaja en `WriteIntent.entity_ref`,
fuera de este payload), asi que ningun payload de esta capa debe contener
jamas el simbolo `"{parent}"`.

Nota de alcance: el diseño original de T011 pedia probar `project_step`
"marcado xfail hasta que T016 aterrice". El veredicto del modelo de
amenaza redisenó T016 en `data-model.md §R2.2/R2.3` (`PackageApprovalEnvelope`,
`StepTemplate`, `project_step_template`) y el coordinador pidio absorber
esa parte pura ahora (T102). Estas pruebas ya no son `xfail`: el modulo
que prueban existe y es completo para lo que le corresponde a `packages`."""

from __future__ import annotations

import json

import pytest

from safent_ads.packages.domain.approval_envelope import (
    StepKind,
    UnresolvableStepTemplateError,
    compute_payload_template_hash,
    creative_hole,
    derive_step_plan,
    image_local_ref,
    project_step,
    project_step_template,
    reopen_holes,
    substitute_holes,
)
from safent_ads.packages.domain.planned_tree import CAMPAIGN_LOCAL_REF
from safent_ads.packages.domain.platform_completeness import (
    ad_set_wire_plan,
    ad_wire_plan,
    campaign_wire_plan,
)

from .conftest import meta_ad, meta_ad_set, propose_google_package, propose_meta_package


class TestDeterminism:
    def test_deriving_the_plan_twice_from_the_same_package_is_byte_identical(self) -> None:
        package = propose_meta_package()

        first = derive_step_plan(package)
        second = derive_step_plan(package)

        assert first == second
        assert [step.payload_template_hash for step in first] == [
            step.payload_template_hash for step in second
        ]

    def test_projecting_the_same_step_twice_gives_the_same_hash(self) -> None:
        package = propose_meta_package()

        first = compute_payload_template_hash(
            project_step_template(package, StepKind.CREATE_CAMPAIGN, CAMPAIGN_LOCAL_REF)
        )
        second = compute_payload_template_hash(
            project_step_template(package, StepKind.CREATE_CAMPAIGN, CAMPAIGN_LOCAL_REF)
        )

        assert first == second


class TestOrderIsFixedAndVerifiable:
    def test_single_ad_set_plan_is_dense_and_ends_with_activation(self) -> None:
        package = propose_meta_package()

        plan = derive_step_plan(package)

        assert [step.step_index for step in plan] == list(range(len(plan)))
        assert plan[-1].step_kind is StepKind.ACTIVATE_CAMPAIGN
        assert sum(1 for step in plan if step.step_kind is StepKind.ACTIVATE_CAMPAIGN) == 1

    def test_order_is_upload_then_campaign_then_ad_sets_then_ads_then_activation(self) -> None:
        ad_set_one = meta_ad_set(local_ref="as#1", ads=(meta_ad(local_ref="as#1/ad#1"),))
        ad_set_two = meta_ad_set(
            local_ref="as#2", ads=(meta_ad(local_ref="as#2/ad#1", checksum="b" * 64),)
        )
        package = propose_meta_package(ad_sets=(ad_set_one, ad_set_two))

        plan = derive_step_plan(package)
        kinds = [step.step_kind for step in plan]

        assert kinds == [
            StepKind.UPLOAD_CREATIVE,
            StepKind.UPLOAD_CREATIVE,
            StepKind.CREATE_CAMPAIGN,
            StepKind.CREATE_AD_SET,
            StepKind.CREATE_AD_SET,
            StepKind.CREATE_AD,
            StepKind.CREATE_AD,
            StepKind.ACTIVATE_CAMPAIGN,
        ]

    def test_upload_creative_steps_are_sorted_by_checksum_and_deduplicated(self) -> None:
        shared_checksum = "c" * 64
        ad_set = meta_ad_set(
            local_ref="as#1",
            ads=(
                meta_ad(local_ref="as#1/ad#1", checksum=shared_checksum),
            ),
        )
        another_ad_set = meta_ad_set(
            local_ref="as#2", ads=(meta_ad(local_ref="as#2/ad#1", checksum=shared_checksum),)
        )
        package = propose_meta_package(ad_sets=(ad_set, another_ad_set))

        plan = derive_step_plan(package)
        uploads = [step for step in plan if step.step_kind is StepKind.UPLOAD_CREATIVE]

        assert len(uploads) == 1
        assert uploads[0].local_ref == image_local_ref(shared_checksum)

    def test_activate_campaign_expects_every_planned_ad_done(self) -> None:
        ad_set_one = meta_ad_set(local_ref="as#1", ads=(meta_ad(local_ref="as#1/ad#1"),))
        ad_set_two = meta_ad_set(
            local_ref="as#2", ads=(meta_ad(local_ref="as#2/ad#1", checksum="e" * 64),)
        )
        package = propose_meta_package(ad_sets=(ad_set_one, ad_set_two))

        plan = derive_step_plan(package)

        assert plan[-1].expected_done_steps == 2


class TestExactProjectionPerStepKind:
    def test_upload_creative_is_only_checksum_and_media_facts(self) -> None:
        ad = meta_ad(checksum="f" * 64)
        package = propose_meta_package(ad_sets=(meta_ad_set(ads=(ad,)),))

        local_ref = image_local_ref(ad.creative.checksum)
        template = project_step_template(package, StepKind.UPLOAD_CREATIVE, local_ref)

        assert template == {
            "checksum": ad.creative.checksum,
            "mime_type": ad.creative.mime_type,
            "width": ad.creative.width,
            "height": ad.creative.height,
        }

    def test_create_campaign_matches_the_wire_plan_used_for_completeness(self) -> None:
        package = propose_meta_package()

        template = project_step_template(package, StepKind.CREATE_CAMPAIGN, CAMPAIGN_LOCAL_REF)

        assert template == campaign_wire_plan(package.campaign)

    def test_create_ad_set_matches_the_wire_plan_used_for_completeness(self) -> None:
        package = propose_meta_package()
        ad_set = package.ad_sets[0]

        template = project_step_template(package, StepKind.CREATE_AD_SET, ad_set.local_ref.value)

        assert template == ad_set_wire_plan(ad_set, package.account_ref.platform)

    def test_create_ad_embeds_the_creative_of_hole_for_a_meta_image_ad(self) -> None:
        """`ad_wire_plan` (`platform_completeness.py`) es la forma exacta
        que `ad_child_creation.validate_child_payload` exige -- el hueco
        `{creative_of:X}` vive en `link_data.image_hash`, sin resolver
        (T112/R2.7: nunca `picture`, ninguna URL de imagen)."""
        ad = meta_ad(checksum="a" * 64)
        package = propose_meta_package(ad_sets=(meta_ad_set(ads=(ad,)),))

        template = project_step_template(package, StepKind.CREATE_AD, ad.local_ref.value)

        assert template == ad_wire_plan(
            ad,
            package.account_ref.platform,
            page_id=package.publish_as.page_id,  # type: ignore[union-attr]
            image_hash=creative_hole(image_local_ref(ad.creative.checksum)),
        )
        link_data = template["native"]["creative_inline"]["object_story_spec"]["link_data"]  # type: ignore[index]
        assert link_data["image_hash"] == creative_hole(image_local_ref(ad.creative.checksum))
        assert link_data["link"] == str(ad.landing)
        assert "picture" not in json.dumps(template)

    def test_create_ad_has_no_creative_hole_for_a_google_text_only_ad(self) -> None:
        package = propose_google_package()
        ad = package.ad_sets[0].ads[0]

        template = project_step_template(package, StepKind.CREATE_AD, ad.local_ref.value)

        assert template == ad_wire_plan(
            ad, package.account_ref.platform, page_id=None, image_hash=None
        )
        assert "image_hash" not in json.dumps(template)

    def test_activate_campaign_is_a_fixed_minimal_payload(self) -> None:
        package = propose_meta_package()

        template = project_step_template(package, StepKind.ACTIVATE_CAMPAIGN, CAMPAIGN_LOCAL_REF)

        assert template == {"status": "ACTIVE"}

    def test_unknown_local_ref_raises_instead_of_inventing_a_hole(self) -> None:
        package = propose_meta_package()

        with pytest.raises(UnresolvableStepTemplateError):
            project_step_template(package, StepKind.CREATE_AD_SET, "as#9")


class TestNoFieldComesFromOutsideTheSignedTree:
    def test_no_template_ever_embeds_the_literal_parent_symbol(self) -> None:
        # El padre viaja en WriteIntent.entity_ref, nunca dentro de la carga
        # (R2.2): si algun payload contuviera "{parent}" seria una fuga de
        # un campo resuelto en ejecucion dentro del contenido firmado.
        package = propose_meta_package(
            ad_sets=(
                meta_ad_set(
                    local_ref="as#1",
                    ads=(
                        meta_ad(local_ref="as#1/ad#1"),
                        meta_ad(local_ref="as#1/ad#2", checksum="b" * 64),
                    ),
                ),
            )
        )
        plan = derive_step_plan(package)

        for step in plan:
            template = project_step_template(package, step.step_kind, step.local_ref)
            serialized = json.dumps(template)
            assert "{parent}" not in serialized

    def test_only_two_hole_shapes_appear_and_both_reference_this_packages_own_ads(self) -> None:
        package = propose_meta_package()
        plan = derive_step_plan(package)
        checksums = {
            ad.creative.checksum
            for ad_set in package.ad_sets
            for ad in ad_set.ads
            if hasattr(ad.creative, "checksum")
        }

        for step in plan:
            template = project_step_template(package, step.step_kind, step.local_ref)
            for value in _walk_strings(template):
                if value.startswith("{creative_of:"):
                    referenced_checksum_prefix = value.removeprefix("{creative_of:img#").rstrip("}")
                    assert any(
                        checksum.startswith(referenced_checksum_prefix) for checksum in checksums
                    )


def _walk_strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _walk_strings(v)]
    if isinstance(value, list):
        return [s for item in value for s in _walk_strings(item)]
    return []


class TestSubstituteAndReopenHoles:
    def test_substitute_replaces_only_matching_holes(self) -> None:
        payload = {"a": "{hole}", "b": ["{hole}", "kept"], "c": {"d": "{other}"}}

        resolved = substitute_holes(payload, {"{hole}": "resolved"})

        assert resolved == {"a": "resolved", "b": ["resolved", "kept"], "c": {"d": "{other}"}}

    def test_reopen_is_the_inverse_of_substitute(self) -> None:
        payload = {"handle": "{creative_of:img#abc123456789}"}
        resolutions = {"{creative_of:img#abc123456789}": "meta:image_hash:deadbeef"}

        resolved = substitute_holes(payload, resolutions)
        reopened = reopen_holes(resolved, resolutions)

        assert reopened == payload

    def test_project_step_then_reopen_holes_reproduces_the_template_hash(self) -> None:
        """INV-12: reopen_holes(project_step(plan, i, r), r) == project_step_template(plan, i)."""
        ad = meta_ad(checksum="a" * 64)
        package = propose_meta_package(ad_sets=(meta_ad_set(ads=(ad,)),))
        hole = creative_hole(image_local_ref(ad.creative.checksum))
        resolutions = {hole: "meta:image_hash:deadbeef"}

        template = project_step_template(package, StepKind.CREATE_AD, ad.local_ref.value)
        resolved = project_step(package, StepKind.CREATE_AD, ad.local_ref.value, resolutions)
        reopened = reopen_holes(resolved, resolutions)

        assert reopened == template
        assert compute_payload_template_hash(reopened) == compute_payload_template_hash(template)

    def test_project_step_without_resolutions_is_identical_to_the_template(self) -> None:
        package = propose_meta_package()

        template = project_step_template(package, StepKind.CREATE_CAMPAIGN, CAMPAIGN_LOCAL_REF)
        projected = project_step(package, StepKind.CREATE_CAMPAIGN, CAMPAIGN_LOCAL_REF, {})

        assert projected == template
