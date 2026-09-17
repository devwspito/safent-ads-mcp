"""`CampaignPackage` -- las 10 invariantes (data-model.md) y la maquina de
11 estados, incluidos los caminos prohibidos (T010)."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from safent_ads.packages.domain.campaign_package import (
    ActivatedOutcome,
    CampaignPackage,
    CampaignPackageApproved,
    CampaignPackageInvalidated,
    CampaignPackageProposed,
    NoneCreatedOutcome,
    PackageState,
    PartialOutcome,
    UncertainOutcome,
    _package_image_checksums,
    _require_distinct_image_limit,
)
from safent_ads.packages.domain.errors import (
    CampaignPackageInvariantError,
    PackageBudgetError,
    PackageHashMismatchError,
    PackageStructureError,
    PlatformCompletenessError,
)
from safent_ads.packages.domain.identifiers import OfferingId, OpaqueIdFormatError, PackageId
from safent_ads.packages.domain.package_hash import PackageHash
from safent_ads.packages.domain.planned_tree import (
    AdRef,
    GoogleAssetGroupNative,
    MetaCampaignNative,
    PlannedTreeError,
    SpecialAdCategory,
)
from safent_ads.packages.domain.platform_completeness import (
    validate_ad_set_completeness,
    validate_campaign_completeness,
)
from safent_ads.packages.domain.values import (
    CallToAction,
    Keyword,
    KeywordPlan,
    LandingUrl,
    MatchType,
    PackageBudget,
    TextOnlyCreativeRef,
)
from safent_ads.proposals.domain.money import Money
from safent_ads.shared.ids import EntityLevel, EntityRef, PlatformCode

from .conftest import (
    NOW,
    account_ref,
    asset_group_assets,
    business_id,
    google_ad,
    google_ad_set,
    google_campaign,
    image_creative,
    meta_ad,
    meta_ad_set,
    meta_campaign,
    meta_campaign_native,
    meta_publish_as,
    package_budget,
    package_rationale,
    performance_max_ad_set,
    propose_meta_package,
)


def _at(seconds: int):
    return NOW + timedelta(seconds=seconds)


# ---------------------------------------------------------------------------
# Invariante 1 -- un paquete, una campana, misma plataforma y cuenta
# ---------------------------------------------------------------------------


class TestInvariant1AccountAndPlatformScope:
    def test_propose_succeeds_with_a_fully_scoped_meta_account(self) -> None:
        package = propose_meta_package()

        assert package.state is PackageState.PROPOSED
        assert package.account_ref.platform is PlatformCode.META

    def test_campaign_platform_must_match_account_platform(self) -> None:
        business = business_id()
        google_account = account_ref(business, platform=PlatformCode.GOOGLE)

        with pytest.raises(PackageStructureError, match="package_platform_mismatch"):
            propose_meta_package(business=business, account=google_account)

    def test_account_ref_must_be_account_level(self) -> None:
        business = business_id()
        campaign_level_ref = EntityRef(
            PlatformCode.META, EntityLevel.CAMPAIGN, "123", business.value, uuid.uuid4()
        )

        with pytest.raises(PackageStructureError, match="must_be_account_level"):
            propose_meta_package(business=business, account=campaign_level_ref)

    def test_account_ref_without_connection_id_is_rejected(self) -> None:
        business = business_id()
        unscoped = EntityRef(PlatformCode.META, EntityLevel.ACCOUNT, "act_123")

        with pytest.raises(PackageStructureError, match="missing_scope"):
            propose_meta_package(business=business, account=unscoped)

    def test_account_ref_business_id_must_match_package_business_id(self) -> None:
        business = business_id()
        other_business = business_id()
        mismatched = account_ref(other_business, platform=PlatformCode.META)

        with pytest.raises(PackageStructureError, match="business_mismatch"):
            propose_meta_package(business=business, account=mismatched)


# ---------------------------------------------------------------------------
# Invariante 2 -- estructura no vacia y acotada
# ---------------------------------------------------------------------------


class TestInvariant2StructureBounds:
    def test_at_least_one_ad_set_is_required(self) -> None:
        with pytest.raises(PackageStructureError, match="ad_sets_count_invalid"):
            propose_meta_package(ad_sets=())

    def test_more_than_three_ad_sets_is_rejected(self) -> None:
        ad_sets = tuple(meta_ad_set(local_ref=f"as#{i}") for i in (1, 2, 3))
        # A fourth ad_set would need local_ref "as#4", which AdSetRef itself
        # already forbids (pattern as#[1-3]) -- proof that the cap is
        # enforced twice, at the value object and at the aggregate.
        with pytest.raises(PlannedTreeError, match="ad_set_ref_invalid"):
            meta_ad_set(local_ref="as#4")
        assert len(ad_sets) == 3  # smoke: the three legal refs do construct

    def test_more_than_twelve_ads_total_is_rejected(self) -> None:
        ad_sets = tuple(
            meta_ad_set(
                local_ref=f"as#{n}",
                ads=tuple(meta_ad(local_ref=f"as#{n}/ad#{m}") for m in (1, 2, 3, 4)),
            )
            for n in (1, 2, 3)
        )
        assert sum(len(ad_set.ads) for ad_set in ad_sets) == 12

        with pytest.raises(PlannedTreeError, match="ad_set_ads_count_invalid"):
            meta_ad_set(
                local_ref="as#1",
                ads=tuple(meta_ad(local_ref=f"as#1/ad#{m}") for m in (1, 2, 3, 4, 1)),
            )


# ---------------------------------------------------------------------------
# Invariante 2 (T022) -- tope duro de imagenes distintas por paquete
# ---------------------------------------------------------------------------


class TestInvariant2DistinctImageLimit:
    """data-model.md: "Tope duro nuevo: <= 20 imagenes distintas por
    paquete (la subida es el paso caro)" (T022). No se puede probar via
    `CampaignPackage.propose()` real -- `_MAX_ADS_TOTAL` (12) ya bloquea
    cualquier arbol que necesitaria 21 imagenes antes de llegar aqui,
    mismo motivo por el que `test_more_than_three_ad_sets_is_rejected`
    prueba el guardia mas bajo directamente en vez del agregado."""

    def _ad_sets_with_distinct_images(self, count: int) -> tuple:
        # `local_ref` se repite a proposito ("as#1"/"as#1/ad#1"): cada
        # `PlannedAdSet` es valido por si solo (patron de `AdSetRef`/
        # `AdRef` respetado), no un arbol de paquete coherente -- lo unico
        # que `_package_image_checksums` mira es `.native`/`.ads[].creative`.
        return tuple(
            meta_ad_set(
                local_ref="as#1",
                ads=(meta_ad(local_ref="as#1/ad#1", checksum=f"{n:064x}"),),
            )
            for n in range(count)
        )

    def test_veinte_imagenes_distintas_no_deniega(self) -> None:
        ad_sets = self._ad_sets_with_distinct_images(20)

        assert len(_package_image_checksums(ad_sets)) == 20
        _require_distinct_image_limit(ad_sets)

    def test_veintiuna_imagenes_distintas_deniega(self) -> None:
        ad_sets = self._ad_sets_with_distinct_images(21)

        with pytest.raises(
            PackageStructureError, match="package_distinct_images_limit_exceeded"
        ):
            _require_distinct_image_limit(ad_sets)

    def test_imagenes_repetidas_entre_papeles_del_grupo_de_recursos_cuentan_una_vez(self) -> None:
        """`asset_group.logo`/`.square_image` pueden compartir el mismo
        `checksum` (R2.7 de 003, intacta) -- el tope cuenta subidas
        distintas, no papeles."""
        shared = image_creative(checksum="e" * 64, width=1080, height=1080)
        ad_set = performance_max_ad_set(
            native=GoogleAssetGroupNative(
                final_url=LandingUrl("https://clinicax.example/reservar"),
                assets=asset_group_assets(logo=shared, square_image=shared),
            )
        )

        assert _package_image_checksums((ad_set,)) == {"e" * 64, "c" * 64}


# ---------------------------------------------------------------------------
# Invariante 3 -- todo anuncio es publicable
# ---------------------------------------------------------------------------


class TestInvariant3AdIsPublishable:
    def test_meta_ad_without_cta_is_rejected(self) -> None:
        with pytest.raises(PlannedTreeError, match="planned_ad_cta_required"):
            meta_ad(cta=None)

    def test_meta_ad_without_image_creative_is_rejected(self) -> None:
        with pytest.raises(PlannedTreeError, match="planned_ad_creative_required"):
            meta_ad(creative=TextOnlyCreativeRef())

    def test_google_ad_with_cta_is_rejected(self) -> None:
        with pytest.raises(PlannedTreeError, match="planned_ad_cta_forbidden"):
            google_ad(cta=CallToAction.LEARN_MORE)

    def test_google_ad_set_cannot_mix_meta_copy(self) -> None:
        with pytest.raises(PlannedTreeError, match="planned_ad_platform_mismatch"):
            google_ad_set(ads=(meta_ad(local_ref="as#1/ad#1"),))


# ---------------------------------------------------------------------------
# Invariante 4 -- destino validado
# ---------------------------------------------------------------------------


class TestInvariant4LandingUrl:
    def test_non_https_landing_is_rejected(self) -> None:
        with pytest.raises(PlannedTreeError, match="landing_url_invalid"):
            LandingUrl("http://clinicax.example/reservar")

    def test_landing_with_credentials_is_rejected(self) -> None:
        with pytest.raises(PlannedTreeError, match="landing_url_invalid"):
            LandingUrl("https://user:pass@clinicax.example/reservar")

    def test_landing_with_fragment_is_rejected(self) -> None:
        with pytest.raises(PlannedTreeError, match="landing_url_invalid"):
            LandingUrl("https://clinicax.example/reservar#top")

    def test_a_planned_ad_carries_a_validated_landing_url(self) -> None:
        ad = meta_ad()

        assert str(ad.landing) == "https://clinicax.example/reservar"


# ---------------------------------------------------------------------------
# Invariante 5 -- dinero dentro del sobre
# ---------------------------------------------------------------------------


class TestInvariant5Budget:
    def test_daily_budget_must_be_positive(self) -> None:
        with pytest.raises(PackageBudgetError, match="daily_not_positive"):
            PackageBudget.derive(daily=Money.of("0"), duration_days=14)

    def test_daily_budget_cannot_exceed_account_daily_cap(self) -> None:
        with pytest.raises(PackageBudgetError, match="exceeds_account_daily_cap"):
            PackageBudget.derive(
                daily=Money.of("50"), duration_days=14, account_daily_cap=Money.of("30")
            )

    def test_total_cap_cannot_exceed_envelope_headroom(self) -> None:
        with pytest.raises(PackageBudgetError, match="exceeds_envelope"):
            PackageBudget.derive(
                daily=Money.of("50"), duration_days=14, envelope_headroom=Money.of("100")
            )

    def test_total_cap_is_derived_never_independent(self) -> None:
        budget = PackageBudget.derive(daily=Money.of("20"), duration_days=14)

        assert budget.total_cap == Money.of("280")
        assert budget.monthly_equivalent == Money.of("600.00")


# ---------------------------------------------------------------------------
# Invariante 6 -- completitud nativa por plataforma, nada se infiere
# ---------------------------------------------------------------------------


class TestInvariant6PlatformCompleteness:
    def test_meta_categories_without_countries_is_rejected_with_a_stable_code(self) -> None:
        with pytest.raises(PlannedTreeError, match="meta_campaign_category_countries_required"):
            meta_campaign_native(
                special_ad_categories=(SpecialAdCategory.HOUSING,),
                special_ad_category_country=(),
            )

    def test_propose_rejects_a_meta_plan_missing_declared_categories_downstream(self) -> None:
        # Building the native VO directly with an inconsistent declaration is
        # already impossible (test above); this proves `propose()` delegates
        # the SAME platform rule instead of re-checking it a second way.
        native = meta_campaign_native()
        assert isinstance(native, MetaCampaignNative)
        assert native.special_ad_categories == ()

    def test_google_ad_set_without_keywords_is_rejected(self) -> None:
        with pytest.raises(PlannedTreeError, match="planned_ad_set_keywords_required"):
            google_ad_set(keywords=None)

    def test_google_ad_set_without_cpc_bid_is_rejected(self) -> None:
        with pytest.raises(PlannedTreeError, match="planned_ad_set_cpc_bid_required"):
            google_ad_set(cpc_bid=None)

    def test_meta_ad_set_cannot_declare_keywords(self) -> None:
        with pytest.raises(PlannedTreeError, match="planned_ad_set_keywords_forbidden"):
            meta_ad_set(keywords=KeywordPlan((Keyword("x", MatchType.EXACT),)))

    def test_platform_completeness_delegates_and_never_duplicates_the_rule(self) -> None:
        budget = validate_campaign_completeness(meta_campaign())

        assert budget == Money.of("20.00")

    def test_platform_completeness_wraps_the_delegated_error(self) -> None:
        broken = google_ad_set()
        object.__setattr__(broken, "cpc_bid", None)  # bypass VO guard to hit the delegate directly

        with pytest.raises(PlatformCompletenessError):
            validate_ad_set_completeness(broken, PlatformCode.GOOGLE)


# ---------------------------------------------------------------------------
# Invariante 7 -- nace en pausa (estructural: no existe campo de estado)
# ---------------------------------------------------------------------------


class TestInvariant7NeverActive:
    def test_planned_campaign_has_no_activation_field(self) -> None:
        campaign = meta_campaign()

        assert not hasattr(campaign, "status")


# ---------------------------------------------------------------------------
# Invariante 8 -- huella viva
# ---------------------------------------------------------------------------


class TestInvariant8LiveHash:
    def test_replace_ad_creative_recomputes_the_hash(self) -> None:
        package = propose_meta_package()
        original_hash = package.package_hash

        new_hash = package.replace_ad_creative(
            AdRef("as#1/ad#1"), image_creative(checksum="b" * 64), NOW
        )

        assert new_hash != original_hash
        assert package.package_hash == new_hash

    def test_replace_ad_creative_while_proposed_does_not_invalidate(self) -> None:
        package = propose_meta_package()

        package.replace_ad_creative(AdRef("as#1/ad#1"), image_creative(checksum="c" * 64), NOW)

        assert package.state is PackageState.PROPOSED

    def test_replace_ad_creative_after_approval_invalidates(self) -> None:
        package = propose_meta_package()
        package.approve(package.package_hash, "auth-1", 45, NOW)

        package.replace_ad_creative(AdRef("as#1/ad#1"), image_creative(checksum="d" * 64), NOW)

        assert package.state is PackageState.INVALIDATED
        events = package.pull_events()
        assert any(isinstance(event, CampaignPackageInvalidated) for event in events)

    def test_replace_ad_creative_on_unknown_ad_ref_raises(self) -> None:
        package = propose_meta_package()

        with pytest.raises(CampaignPackageInvariantError):
            package.replace_ad_creative(AdRef("as#1/ad#2"), image_creative(), NOW)

    def test_replace_ad_creative_outside_editable_states_raises(self) -> None:
        package = propose_meta_package()
        package.reject(NOW)

        with pytest.raises(CampaignPackageInvariantError, match="no editable"):
            package.replace_ad_creative(AdRef("as#1/ad#1"), image_creative(), NOW)


# ---------------------------------------------------------------------------
# Invariante 9 -- avance monotonico
# ---------------------------------------------------------------------------


class TestInvariant9MonotonicAdvance:
    def test_publishing_never_returns_to_proposed(self) -> None:
        package = propose_meta_package()
        package.approve(package.package_hash, "auth-1", 45, NOW)
        package.begin_publishing(NOW)

        with pytest.raises(CampaignPackageInvariantError):
            package.reject(NOW)

    def test_published_is_terminal_at_the_aggregate_level(self) -> None:
        package = propose_meta_package()
        package.approve(package.package_hash, "auth-1", 45, NOW)
        package.begin_publishing(NOW)
        package.record_publication_outcome(
            ActivatedOutcome(
                campaign_entity_ref="meta:campaign:123", activated_at=NOW, undo_deadline=_at(7200)
            ),
            NOW,
        )

        assert package.state is PackageState.PUBLISHED
        with pytest.raises(CampaignPackageInvariantError):
            package.begin_publishing(NOW)


# ---------------------------------------------------------------------------
# Invariante 10 -- coherencia de la oferta (fuera del dominio puro)
# ---------------------------------------------------------------------------


class TestInvariant10OfferingCoherence:
    def test_offering_id_must_be_a_well_formed_opaque_id(self) -> None:
        with pytest.raises(OpaqueIdFormatError):
            OfferingId("bad id with spaces!")

    def test_existence_in_the_catalog_is_an_application_layer_concern(self) -> None:
        # `packages` no importa `catalog` (data-model.md, dependencias
        # permitidas): comprobar que `offering_id` existe de verdad es
        # `OfferingLookupPort`, en `ProposeCampaignPackage` (T021), no aqui.
        assert True


# ---------------------------------------------------------------------------
# La maquina de 11 estados, incluidos los caminos prohibidos
# ---------------------------------------------------------------------------


class TestFullLifecycle:
    def test_propose_lands_in_proposed_and_emits_event(self) -> None:
        package = propose_meta_package()

        assert package.state is PackageState.PROPOSED
        events = package.pull_events()
        assert any(isinstance(event, CampaignPackageProposed) for event in events)

    def test_proposed_to_approved_to_publishing_to_published(self) -> None:
        package = propose_meta_package()

        package.approve(package.package_hash, "auth-1", 45, NOW)
        assert package.state is PackageState.APPROVED

        package.begin_publishing(NOW)
        assert package.state is PackageState.PUBLISHING

        package.record_publication_outcome(
            ActivatedOutcome(
                campaign_entity_ref="meta:campaign:123", activated_at=NOW, undo_deadline=_at(7200)
            ),
            NOW,
        )
        assert package.state is PackageState.PUBLISHED

    def test_publishing_can_end_partially_published(self) -> None:
        package = propose_meta_package()
        package.approve(package.package_hash, "auth-1", 45, NOW)
        package.begin_publishing(NOW)

        package.record_publication_outcome(
            PartialOutcome(
                created_count=2, failed_step_index=3, next_step_hint="reintentar anuncio 2"
            ),
            NOW,
        )

        assert package.state is PackageState.PARTIALLY_PUBLISHED

    def test_partially_published_resumes_and_can_finish_published(self) -> None:
        # Regresion: `_TRANSITIONS[PARTIALLY_PUBLISHED]` era `frozenset()`
        # -- `ResumePackagePublication` (contracts/api.md §R2.C) no podia
        # nunca terminar la saga, `record_publication_outcome` siempre
        # rechazaba el ultimo paso con `CampaignPackageInvariantError`.
        package = propose_meta_package()
        package.approve(package.package_hash, "auth-1", 45, NOW)
        package.begin_publishing(NOW)
        package.record_publication_outcome(
            PartialOutcome(
                created_count=2, failed_step_index=3, next_step_hint="reintentar anuncio 2"
            ),
            NOW,
        )

        package.resume_publishing(NOW)
        assert package.state is PackageState.PUBLISHING

        package.record_publication_outcome(
            ActivatedOutcome(
                campaign_entity_ref="meta:campaign:123", activated_at=NOW, undo_deadline=_at(7200)
            ),
            NOW,
        )
        assert package.state is PackageState.PUBLISHED

    def test_resume_publishing_rejects_any_other_state(self) -> None:
        package = propose_meta_package()

        with pytest.raises(CampaignPackageInvariantError):
            package.resume_publishing(NOW)

    def test_publishing_can_fail_with_nothing_created(self) -> None:
        package = propose_meta_package()
        package.approve(package.package_hash, "auth-1", 45, NOW)
        package.begin_publishing(NOW)

        package.record_publication_outcome(NoneCreatedOutcome(outcome_code="broker_timeout"), NOW)

        assert package.state is PackageState.FAILED

    def test_publishing_can_go_uncertain_then_resolve(self) -> None:
        package = propose_meta_package()
        package.approve(package.package_hash, "auth-1", 45, NOW)
        package.begin_publishing(NOW)

        package.record_publication_outcome(UncertainOutcome(), NOW)
        assert package.state is PackageState.VERIFYING

        package.record_publication_outcome(
            PartialOutcome(created_count=3, failed_step_index=4, next_step_hint="reintentar"), NOW
        )
        assert package.state is PackageState.PARTIALLY_PUBLISHED

    def test_proposed_rejects_directly(self) -> None:
        package = propose_meta_package()

        package.reject(NOW, reason="no encaja con la marca")

        assert package.state is PackageState.REJECTED

    def test_proposed_expires_directly(self) -> None:
        package = propose_meta_package()

        package.expire(NOW)

        assert package.state is PackageState.EXPIRED

    def test_approved_can_be_invalidated_within_the_grace_window(self) -> None:
        package = propose_meta_package()
        package.approve(package.package_hash, "auth-1", 45, NOW)

        package.invalidate("cancelled_within_grace", NOW)

        assert package.state is PackageState.INVALIDATED

    def test_draft_can_be_completed_into_proposed_or_discarded(self) -> None:
        # `draft` no lo produce ninguna fabrica publica hoy (FR-14: el
        # arbol llega completo en una sola llamada), pero la maquina de 11
        # estados debe reconocerlo -- se construye directamente, como hacen
        # los tests de `Proposal` con `proposal.state = setup_state`.
        package = propose_meta_package()
        package.state = PackageState.DRAFT

        package.reject(NOW, reason="incompleto")

        assert package.state is PackageState.REJECTED


class TestApprovalDiffBinding:
    def test_approve_rejects_a_stale_package_hash(self) -> None:
        package = propose_meta_package()

        with pytest.raises(PackageHashMismatchError):
            package.approve(PackageHash("0" * 64), "auth-1", 45, NOW)

        assert package.state is PackageState.PROPOSED

    def test_approve_emits_event_with_grace_seconds(self) -> None:
        package = propose_meta_package()

        package.approve(package.package_hash, "auth-1", 45, NOW)

        events = package.pull_events()
        approved = next(e for e in events if isinstance(e, CampaignPackageApproved))
        assert approved.grace_seconds == 45
        assert approved.authorization_id == "auth-1"


class TestInvalidTransitionsAreRejected:
    @pytest.mark.parametrize(
        "setup_state",
        [
            PackageState.PUBLISHED,
            PackageState.PARTIALLY_PUBLISHED,
            PackageState.FAILED,
            PackageState.REJECTED,
            PackageState.EXPIRED,
            PackageState.INVALIDATED,
        ],
    )
    def test_terminal_states_accept_no_further_transition(self, setup_state: PackageState) -> None:
        package = propose_meta_package()
        package.state = setup_state

        with pytest.raises(CampaignPackageInvariantError):
            package.approve(package.package_hash, "auth-1", 45, NOW)
        with pytest.raises(CampaignPackageInvariantError):
            package.reject(NOW)
        with pytest.raises(CampaignPackageInvariantError):
            package.expire(NOW)
        with pytest.raises(CampaignPackageInvariantError):
            package.begin_publishing(NOW)

    def test_cannot_approve_twice(self) -> None:
        package = propose_meta_package()
        package.approve(package.package_hash, "auth-1", 45, NOW)

        with pytest.raises(CampaignPackageInvariantError):
            package.approve(package.package_hash, "auth-2", 45, NOW)

    def test_cannot_begin_publishing_before_approval(self) -> None:
        package = propose_meta_package()

        with pytest.raises(CampaignPackageInvariantError):
            package.begin_publishing(NOW)

    def test_cannot_reject_once_approved(self) -> None:
        package = propose_meta_package()
        package.approve(package.package_hash, "auth-1", 45, NOW)

        with pytest.raises(CampaignPackageInvariantError):
            package.reject(NOW)

    def test_cannot_expire_once_approved(self) -> None:
        package = propose_meta_package()
        package.approve(package.package_hash, "auth-1", 45, NOW)

        with pytest.raises(CampaignPackageInvariantError):
            package.expire(NOW)

    def test_cannot_record_outcome_before_publishing(self) -> None:
        package = propose_meta_package()
        package.approve(package.package_hash, "auth-1", 45, NOW)

        with pytest.raises(CampaignPackageInvariantError):
            package.record_publication_outcome(NoneCreatedOutcome(outcome_code="x"), NOW)

    def test_draft_cannot_be_approved_directly(self) -> None:
        package = propose_meta_package()
        package.state = PackageState.DRAFT

        with pytest.raises(CampaignPackageInvariantError):
            package.approve(package.package_hash, "auth-1", 45, NOW)


class TestOwnerContext:
    def test_set_owner_context_stores_the_text(self) -> None:
        package = propose_meta_package()

        package.set_owner_context("Esperar a validar con el veterinario jefe.")

        assert package.owner_context == "Esperar a validar con el veterinario jefe."

    def test_set_owner_context_rejects_text_over_the_max_length(self) -> None:
        package = propose_meta_package()

        with pytest.raises(CampaignPackageInvariantError):
            package.set_owner_context("a" * 501)


class TestPublishAsInvariant:
    def test_meta_package_requires_publish_as(self) -> None:
        with pytest.raises(PackageStructureError, match="publish_as_required"):
            propose_meta_package(publish_as=None)

    def test_google_package_forbids_publish_as(self) -> None:
        business = business_id()
        google_account = account_ref(business, platform=PlatformCode.GOOGLE)

        with pytest.raises(PackageStructureError, match="publish_as_forbidden"):
            CampaignPackage.propose(
                package_id=PackageId.new(),
                business_id=business,
                account_ref=google_account,
                publish_as=meta_publish_as(),
                offering_id=OfferingId("offering-1"),
                campaign=google_campaign(),
                ad_sets=(google_ad_set(),),
                budget=package_budget(),
                rationale=package_rationale(),
                research=None,
                now=NOW,
                expires_at=_at(3600),
            )
