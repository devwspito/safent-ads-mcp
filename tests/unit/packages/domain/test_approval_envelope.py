"""`PackageApprovalEnvelope` -- lo que el dueño firma una vez al aprobar
(T102, data-model.md "Revision 2" §R2.2). Determinismo de
`build_approval_envelope`/`compute_envelope_hash` y las cotas de forma del
sobre (denso, `ACTIVATE_CAMPAIGN` siempre el ultimo y unico)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from safent_ads.packages.domain.approval_envelope import (
    PackageApprovalEnvelope,
    StepKind,
    StepTemplate,
    build_approval_envelope,
    compute_envelope_hash,
    derive_step_plan,
    image_local_ref,
    project_step_template,
)
from safent_ads.packages.domain.errors import PackageDomainError
from safent_ads.packages.domain.planned_tree import GoogleAssetGroupNative
from safent_ads.packages.domain.values import LandingUrl
from safent_ads.shared.ids import PlatformCode

from .conftest import (
    NOW,
    asset_group_assets,
    google_campaign,
    image_creative,
    meta_ad,
    meta_ad_set,
    performance_max_ad_set,
    performance_max_campaign_native,
    propose_google_package,
    propose_meta_package,
)


def _envelope(package, **overrides):
    defaults = dict(
        package=package,
        publication_id="pub-1",
        approved_by="owner-1",
        approved_at=NOW,
        approval_expires_at=NOW + timedelta(minutes=30),
    )
    defaults.update(overrides)
    return build_approval_envelope(**defaults)


class TestBuildApprovalEnvelopeIsPure:
    def test_same_package_and_inputs_produce_the_same_envelope_hash(self) -> None:
        package = propose_meta_package()

        first = _envelope(package)
        second = _envelope(package)

        assert compute_envelope_hash(first) == compute_envelope_hash(second)

    def test_same_package_and_inputs_produce_an_identical_step_plan(self) -> None:
        package = propose_meta_package()

        first = _envelope(package)
        second = _envelope(package)

        assert first.step_plan == second.step_plan

    def test_a_different_publication_id_changes_the_envelope_hash_but_not_the_plan(self) -> None:
        package = propose_meta_package()

        first = _envelope(package, publication_id="pub-1")
        second = _envelope(package, publication_id="pub-2")

        assert first.step_plan == second.step_plan
        assert compute_envelope_hash(first) != compute_envelope_hash(second)

    def test_envelope_step_plan_matches_derive_step_plan_directly(self) -> None:
        package = propose_meta_package()

        envelope = _envelope(package)

        assert envelope.step_plan == derive_step_plan(package)

    def test_envelope_carries_the_packages_own_identity(self) -> None:
        package = propose_meta_package()

        envelope = _envelope(package)

        assert envelope.package_id == package.package_id
        assert envelope.package_hash == package.package_hash.value
        assert envelope.business_id == package.business_id
        assert envelope.platform is PlatformCode.META
        assert envelope.account_ref == package.account_ref
        assert envelope.envelope_version == 2


def _build_envelope_with_plan(package, step_plan):
    return PackageApprovalEnvelope(
        package_id=package.package_id,
        package_hash=package.package_hash.value,
        business_id=package.business_id,
        platform=PlatformCode.META,
        account_ref=package.account_ref,
        publication_id="pub-1",
        approved_by="owner-1",
        approved_at=NOW,
        approval_expires_at=NOW + timedelta(minutes=30),
        step_plan=step_plan,
    )


def _reindexed(steps):
    return tuple(
        StepTemplate(
            step_index=index,
            step_kind=step.step_kind,
            local_ref=step.local_ref,
            parent_local_ref=step.parent_local_ref,
            payload_template_hash=step.payload_template_hash,
            expected_done_steps=step.expected_done_steps,
            depends_on=step.depends_on,
        )
        for index, step in enumerate(steps)
    )


class TestStepPlanShapeInvariants:
    def test_step_plan_must_be_dense_from_zero(self) -> None:
        package = propose_meta_package()
        plan = list(derive_step_plan(package))
        tampered = tuple(plan[:1] + plan[2:])  # hueco: falta el indice 1

        with pytest.raises(PackageDomainError, match="not_dense"):
            _build_envelope_with_plan(package, tampered)

    def test_activate_campaign_must_be_last(self) -> None:
        package = propose_meta_package()
        plan = list(derive_step_plan(package))
        reordered = _reindexed([plan[-1], *plan[:-1]])

        with pytest.raises(PackageDomainError, match="activate_not_last"):
            _build_envelope_with_plan(package, reordered)


class TestToCanonicalIsExplicit:
    def test_to_canonical_lists_every_step(self) -> None:
        package = propose_meta_package()
        envelope = _envelope(package)

        canonical = envelope.to_canonical()

        assert len(canonical["step_plan"]) == len(envelope.step_plan)
        assert canonical["step_plan"][-1]["step_kind"] == StepKind.ACTIVATE_CAMPAIGN.value


def _package_with_asset_group():
    campaign = google_campaign(native=performance_max_campaign_native())
    return propose_google_package(campaign=campaign, ad_sets=(performance_max_ad_set(),))


class TestAssetGroupStepTemplate:
    """T024: la plantilla del grupo de recursos no introduce un tercer
    tipo de hueco -- sigue habiendo solo dos (R2.2): el `entity_ref` (nunca
    un campo de la carga) y `{creative_of:<local_ref>}`."""

    def test_plantilla_de_grupo_de_recursos_tiene_los_mismos_dos_huecos(self) -> None:
        package = _package_with_asset_group()

        template = project_step_template(package, StepKind.CREATE_AD_SET, "as#1")

        assert template["native"]["kind"] == "ASSET_GROUP"
        assert "entity_ref" not in template
        assert "{parent}" not in str(template)

    def test_las_tres_imagenes_del_grupo_de_recursos_generan_upload_creative(self) -> None:
        package = _package_with_asset_group()
        assets = package.ad_sets[0].native.assets

        plan = derive_step_plan(package)

        upload_refs = {
            step.local_ref for step in plan if step.step_kind is StepKind.UPLOAD_CREATIVE
        }
        expected_refs = {image_local_ref(image.checksum) for image in assets.images}
        assert expected_refs <= upload_refs

    def test_el_paso_create_ad_set_depende_de_sus_tres_imagenes(self) -> None:
        package = _package_with_asset_group()
        assets = package.ad_sets[0].native.assets

        plan = derive_step_plan(package)

        create_ad_set = next(step for step in plan if step.step_kind is StepKind.CREATE_AD_SET)
        expected_refs = {image_local_ref(image.checksum) for image in assets.images}
        assert set(create_ad_set.depends_on) == expected_refs


class TestExpectedDoneStepsNeverZero:
    """T043/BL-3: `expected_done_steps` cuenta los nodos que SON el
    anuncio -- un `PlannedAd` normal, o el propio grupo de recursos en
    Maximo Rendimiento (0 `PlannedAd` por diseño, `ads_per_node=(0, 0)`).
    Antes de esta tarea, `total_ads = sum(len(ad_set.ads) ...)` daba 0
    para cualquier paquete de Maximo Rendimiento -- la puerta «no se
    activa hasta que existe toda la estructura» (003 AL-4) quedaba
    vacia justo en ese canal."""

    def test_expected_done_steps_nunca_es_cero(self) -> None:
        package = _package_with_asset_group()

        plan = derive_step_plan(package)

        assert plan[-1].step_kind is StepKind.ACTIVATE_CAMPAIGN
        assert plan[-1].expected_done_steps == 1

    def test_expected_done_steps_cuenta_un_grupo_de_recursos_por_ad_set(self) -> None:
        campaign = google_campaign(native=performance_max_campaign_native())
        ad_set_two = performance_max_ad_set(
            local_ref="as#2",
            native=GoogleAssetGroupNative(
                final_url=LandingUrl("https://clinicax.example/reservar"),
                assets=asset_group_assets(
                    logo=image_creative(checksum="h" * 64, width=1080, height=1080),
                    marketing_image=image_creative(checksum="i" * 64, width=1200, height=628),
                    square_image=image_creative(checksum="j" * 64, width=1080, height=1080),
                ),
            ),
        )
        package = propose_google_package(
            campaign=campaign, ad_sets=(performance_max_ad_set(), ad_set_two)
        )

        plan = derive_step_plan(package)

        assert plan[-1].expected_done_steps == 2


def _distinct_meta_ad_set(index: int):
    ads = tuple(
        meta_ad(local_ref=f"as#{index}/ad#{ad_index}", checksum=f"meta-checksum-{index}-{ad_index}")
        for ad_index in range(1, 5)
    )
    return meta_ad_set(local_ref=f"as#{index}", ads=ads)


def _distinct_performance_max_ad_set(index: int):
    return performance_max_ad_set(
        local_ref=f"as#{index}",
        native=GoogleAssetGroupNative(
            final_url=LandingUrl("https://clinicax.example/reservar"),
            assets=asset_group_assets(
                logo=image_creative(checksum=f"pmax-logo-{index}", width=1080, height=1080),
                marketing_image=image_creative(
                    checksum=f"pmax-marketing-{index}", width=1200, height=628
                ),
                square_image=image_creative(
                    checksum=f"pmax-square-{index}", width=1080, height=1080
                ),
            ),
        ),
    )


class TestStructuralMaximumProducesAValidEnvelope:
    """T044/D-4/AL-7: el paquete al limite estructural de cada familia
    sigue produciendo un sobre valido -- `_MAX_STEP_PLAN_LENGTH` no se
    quedo corto al sumar los pasos de subida del grupo de recursos."""

    def test_meta_al_limite_estructural_produce_un_sobre_valido(self) -> None:
        ad_sets = tuple(_distinct_meta_ad_set(index) for index in range(1, 4))
        package = propose_meta_package(ad_sets=ad_sets)

        envelope = _envelope(package)

        assert len(envelope.step_plan) == 29
        assert envelope.step_plan[-1].expected_done_steps == 12

    def test_maximo_rendimiento_al_limite_estructural_produce_un_sobre_valido(self) -> None:
        campaign = google_campaign(native=performance_max_campaign_native())
        ad_sets = tuple(_distinct_performance_max_ad_set(index) for index in range(1, 4))
        package = propose_google_package(campaign=campaign, ad_sets=ad_sets)

        envelope = _envelope(package)

        assert len(envelope.step_plan) == 14
        assert envelope.step_plan[-1].expected_done_steps == 3
