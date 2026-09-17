"""`ProposeCampaignPackage` (tasks.md T021). Cada error tipado de
`contracts/mcp-tools.md §4` sale con su codigo exacto."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from safent_ads.creative.domain.identifiers import AssetId
from safent_ads.packages.application.errors import (
    AmbiguousAccountError,
    AssetGroupIncompleteError,
    BudgetEnvelopeExceededError,
    ChannelTypeNotEnabledError,
    CreativeAspectRatioInvalidError,
    CreativeNotUsableError,
    DailyBudgetExceedsCapError,
    DuplicateOpenPackageError,
    LandingUrlNotAllowedError,
    NoActiveAccountForPlatformError,
    OfferingNotFoundError,
    PackageStructureInvalidError,
    PlatformNativeIncompleteError,
)
from safent_ads.packages.application.ports import (
    CreativeAssetSnapshot,
    PackageBudgetEnvelope,
    ResolvedPublishAs,
)
from safent_ads.packages.application.propose_campaign_package import (
    PlannedAdInput,
    PlannedAdSetInput,
    PlannedAssetGroupInput,
    ProposeCampaignPackage,
    ProposeCampaignPackageRequest,
)
from safent_ads.packages.domain.identifiers import OfferingId, PackageId
from safent_ads.packages.domain.planned_tree import AdRef, AdSetRef, GoogleAssetGroupNative
from safent_ads.packages.domain.values import (
    Audience,
    CallToAction,
    GoogleAdCopyPlan,
    MetaAdCopyPlan,
    PackageRationale,
)
from safent_ads.proposals.domain.google_channel_spec import GoogleAdvertisingChannelType
from safent_ads.proposals.domain.money import Money
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import PlatformCode

from ..domain.conftest import (
    account_ref,
    business_id,
    google_ad_set,
    google_campaign,
    meta_ad_set,
    meta_campaign,
    performance_max_campaign_native,
)
from .conftest import (
    FakeAccountDailyCapPort,
    FakeActiveAccountLookupPort,
    FakeBudgetEnvelopeReadPort,
    FakeCampaignPackageRepository,
    FakeCreativeAssetLookupPort,
    FakeLandingDomainPolicyPort,
    FakeOfferingExistsPort,
    FakePublishAsLookupPort,
)

NOW = datetime(2026, 9, 14, 10, 0, tzinfo=UTC)
_ASSET_ID = str(AssetId.new())
_LANDING = "https://clinicax.example/reservar"


def _creative_snapshot(asset_id: str = _ASSET_ID) -> CreativeAssetSnapshot:
    return CreativeAssetSnapshot(
        asset_id=asset_id,
        checksum="a" * 64,
        mime_type="image/png",
        width=1200,
        height=628,
        preview_key="creative/preview-1.png",
    )


def _meta_ad_input(local_ref: str = "as#1/ad#1", landing_url: str = _LANDING) -> PlannedAdInput:
    return PlannedAdInput(
        local_ref=AdRef(local_ref),
        name="Anuncio 1",
        creative_asset_id=_ASSET_ID,
        landing_url=landing_url,
        copy=MetaAdCopyPlan(
            primary_text="Reserva tu cita hoy mismo, sin esperas.",
            headline="Reserva ya",
            description="Citas veterinarias fuera de horario",
        ),
        cta=CallToAction.LEARN_MORE,
    )


def _meta_ad_set_input(local_ref: str = "as#1") -> PlannedAdSetInput:
    built = meta_ad_set(local_ref=local_ref)
    return PlannedAdSetInput(
        local_ref=AdSetRef(local_ref),
        name=built.name,
        audience=built.audience,
        native=built.native,
        ads=(_meta_ad_input(local_ref=f"{local_ref}/ad#1"),),
    )


class Fixture:
    def __init__(self) -> None:
        self.business = business_id()
        self.account = account_ref(self.business, platform=PlatformCode.META)
        self.repo = FakeCampaignPackageRepository()
        self.offerings = FakeOfferingExistsPort()
        self.accounts = FakeActiveAccountLookupPort(account=self.account)
        self.daily_caps = FakeAccountDailyCapPort()
        self.budget_envelope = FakeBudgetEnvelopeReadPort()
        self.publish_as_lookup = FakePublishAsLookupPort(
            resolved=ResolvedPublishAs(page_id="1234567890", page_name="Clinica X")
        )
        self.landing_policy = FakeLandingDomainPolicyPort(hosts=frozenset({"clinicax.example"}))
        self.creative_lookup = FakeCreativeAssetLookupPort(usable={_ASSET_ID: _creative_snapshot()})
        self.clock = FixedClock(NOW)
        # T076 (POLISH): this suite exercises the channel's business logic,
        # not the ADS_GOOGLE_CHANNELS_ENABLED gate itself (see
        # TestChannelNotEnabled below) -- all four rows stay on here so the
        # DISPLAY/DEMAND_GEN/PERFORMANCE_MAX tests above keep exercising
        # what they always tested.
        self.enabled_google_channels = frozenset(GoogleAdvertisingChannelType)

    def use_case(self) -> ProposeCampaignPackage:
        return ProposeCampaignPackage(
            packages=self.repo,
            offerings=self.offerings,
            accounts=self.accounts,
            daily_caps=self.daily_caps,
            budget_envelope=self.budget_envelope,
            publish_as_lookup=self.publish_as_lookup,
            landing_policy=self.landing_policy,
            creative_lookup=self.creative_lookup,
            clock=self.clock,
            enabled_google_channels=self.enabled_google_channels,
        )

    def request(self, **overrides: object) -> ProposeCampaignPackageRequest:
        defaults: dict[str, object] = {
            "business_id": self.business,
            "platform": PlatformCode.META,
            "account_ref": None,
            "offering_id": OfferingId("offering-1"),
            "campaign": meta_campaign(),
            "ad_sets": (_meta_ad_set_input(),),
            "rationale": _rationale(),
            "research": None,
        }
        defaults.update(overrides)
        return ProposeCampaignPackageRequest(**defaults)  # type: ignore[arg-type]


def _rationale() -> PackageRationale:
    return PackageRationale(
        owner_request="Hazme una campaña de reserva de citas veterinarias",
        why="Tus clientes buscan cita fuera de horario y no hay campaña que los recoja",
    )


class TestHappyPath:
    async def test_meta_package_is_proposed_and_saved(self) -> None:
        fixture = Fixture()

        package = await fixture.use_case().execute(fixture.request())

        assert str(package.package_id) in fixture.repo.by_id
        assert package.publish_as is not None
        assert package.publish_as.page_id == "1234567890"

    async def test_google_never_calls_publish_as_lookup(self) -> None:
        fixture = Fixture()
        fixture.account = account_ref(fixture.business, platform=PlatformCode.GOOGLE)
        fixture.accounts = FakeActiveAccountLookupPort(account=fixture.account)
        fixture.publish_as_lookup.resolved = None  # would raise if ever called
        built = google_ad_set()
        google_input = PlannedAdSetInput(
            local_ref=AdSetRef("as#1"),
            name=built.name,
            audience=built.audience,
            native=built.native,
            keywords=built.keywords,
            cpc_bid=built.cpc_bid,
            ads=(
                PlannedAdInput(
                    local_ref=AdRef("as#1/ad#1"),
                    name="Anuncio 1",
                    creative_asset_id=None,
                    landing_url=_LANDING,
                    copy=GoogleAdCopyPlan(
                        headlines=("Reserva ya", "Cita veterinaria", "Atencion 24h"),
                        descriptions=(
                            "Reserva tu cita en minutos",
                            "Atencion profesional cercana",
                        ),
                    ),
                    cta=None,
                ),
            ),
        )

        package = await fixture.use_case().execute(
            fixture.request(
                platform=PlatformCode.GOOGLE,
                campaign=google_campaign(),
                ad_sets=(google_input,),
            )
        )

        assert package.publish_as is None


class TestTypedErrors:
    async def test_offering_not_found(self) -> None:
        fixture = Fixture()
        fixture.offerings = FakeOfferingExistsPort(exists=False)

        with pytest.raises(OfferingNotFoundError):
            await fixture.use_case().execute(fixture.request())

    async def test_ambiguous_account(self) -> None:
        fixture = Fixture()
        fixture.accounts = FakeActiveAccountLookupPort(account=None, ambiguous=True)

        with pytest.raises(AmbiguousAccountError):
            await fixture.use_case().execute(fixture.request())

    async def test_no_active_account(self) -> None:
        fixture = Fixture()
        fixture.accounts = FakeActiveAccountLookupPort(account=None)

        with pytest.raises(NoActiveAccountForPlatformError):
            await fixture.use_case().execute(fixture.request())

    async def test_daily_budget_exceeds_cap(self) -> None:
        fixture = Fixture()
        fixture.daily_caps = FakeAccountDailyCapPort(cap=Money.of("10.00"))

        with pytest.raises(DailyBudgetExceedsCapError):
            await fixture.use_case().execute(fixture.request())

    async def test_budget_envelope_exceeded(self) -> None:
        fixture = Fixture()
        fixture.budget_envelope = FakeBudgetEnvelopeReadPort(
            envelope=PackageBudgetEnvelope(
                monthly_cap_minor=10000,
                spent_month_to_date_minor=9950,
                headroom_minor=50,
                currency="EUR",
                reason=None,
            )
        )

        with pytest.raises(BudgetEnvelopeExceededError):
            await fixture.use_case().execute(fixture.request())

    async def test_duplicate_open_package(self) -> None:
        fixture = Fixture()
        fixture.repo.duplicate_of = PackageId.new()

        with pytest.raises(DuplicateOpenPackageError):
            await fixture.use_case().execute(fixture.request())

    async def test_creative_not_usable(self) -> None:
        fixture = Fixture()
        fixture.creative_lookup = FakeCreativeAssetLookupPort(usable={})

        with pytest.raises(CreativeNotUsableError):
            await fixture.use_case().execute(fixture.request())

    async def test_landing_url_not_allowed(self) -> None:
        fixture = Fixture()
        fixture.landing_policy = FakeLandingDomainPolicyPort(
            hosts=frozenset({"notclinicax.example"})
        )

        with pytest.raises(LandingUrlNotAllowedError):
            await fixture.use_case().execute(fixture.request())

    async def test_platform_native_incomplete_when_no_meta_page(self) -> None:
        fixture = Fixture()
        fixture.publish_as_lookup = FakePublishAsLookupPort(resolved=None)

        with pytest.raises(PlatformNativeIncompleteError):
            await fixture.use_case().execute(fixture.request())

    async def test_package_structure_invalid_on_duplicate_ad_set_ref(self) -> None:
        fixture = Fixture()
        duplicate = (_meta_ad_set_input("as#1"), _meta_ad_set_input("as#1"))

        with pytest.raises(PackageStructureInvalidError):
            await fixture.use_case().execute(fixture.request(ad_sets=duplicate))


class TestChannelNotEnabled:
    """tasks.md T076 (POLISH): defence in depth at `ProposeCampaignPackage.
    execute` -- the presentation boundary (`mcp.presentation.package_tools`)
    already rejects this before any port I/O; this covers the use case on
    its own, in case a future caller builds the request without going
    through that boundary."""

    async def test_performance_max_denied_when_only_search_is_enabled(self) -> None:
        fixture = Fixture()
        fixture.enabled_google_channels = frozenset({GoogleAdvertisingChannelType.SEARCH})
        fixture.account = account_ref(fixture.business, platform=PlatformCode.GOOGLE)
        fixture.accounts = FakeActiveAccountLookupPort(account=fixture.account)

        with pytest.raises(ChannelTypeNotEnabledError) as excinfo:
            await fixture.use_case().execute(
                fixture.request(
                    platform=PlatformCode.GOOGLE,
                    campaign=google_campaign(native=performance_max_campaign_native()),
                    ad_sets=(_asset_group_ad_set_input(),),
                )
            )
        assert excinfo.value.channel == "PERFORMANCE_MAX"

    async def test_search_stays_allowed_when_only_search_is_enabled(self) -> None:
        fixture = Fixture()
        fixture.enabled_google_channels = frozenset({GoogleAdvertisingChannelType.SEARCH})
        fixture.account = account_ref(fixture.business, platform=PlatformCode.GOOGLE)
        fixture.accounts = FakeActiveAccountLookupPort(account=fixture.account)
        built = google_ad_set()
        ad_set = PlannedAdSetInput(
            local_ref=AdSetRef("as#1"),
            name=built.name,
            audience=built.audience,
            native=built.native,
            keywords=built.keywords,
            cpc_bid=built.cpc_bid,
            ads=(
                PlannedAdInput(
                    local_ref=AdRef("as#1/ad#1"),
                    name="Anuncio 1",
                    creative_asset_id=None,
                    landing_url=_LANDING,
                    copy=GoogleAdCopyPlan(
                        headlines=("Reserva ya", "Cita veterinaria", "Atencion 24h"),
                        descriptions=(
                            "Reserva tu cita en minutos",
                            "Atencion profesional cercana",
                        ),
                    ),
                    cta=None,
                ),
            ),
        )

        package = await fixture.use_case().execute(
            fixture.request(
                platform=PlatformCode.GOOGLE, campaign=google_campaign(), ad_sets=(ad_set,)
            )
        )

        assert package.publish_as is None


_ASSET_GROUP_LANDING = "https://clinicax.example/reservar"
_ASSET_GROUP_LOGO_ID = str(AssetId.new())
_ASSET_GROUP_MARKETING_ID = str(AssetId.new())
_ASSET_GROUP_SQUARE_ID = str(AssetId.new())


def _asset_group_snapshot(asset_id: str, *, width: int, height: int) -> CreativeAssetSnapshot:
    return CreativeAssetSnapshot(
        asset_id=asset_id,
        checksum=asset_id.replace("-", "").ljust(64, "0")[:64],
        mime_type="image/png",
        width=width,
        height=height,
        preview_key=f"creative/{asset_id}.png",
    )


def _asset_group_input(**overrides: object) -> PlannedAssetGroupInput:
    defaults: dict[str, object] = {
        "final_url": _ASSET_GROUP_LANDING,
        "headlines": ("Reserva ya", "Cita veterinaria", "Atencion 24h"),
        "long_headlines": ("Reserva tu cita veterinaria en minutos",),
        "descriptions": ("Reserva tu cita en minutos", "Atencion profesional cercana"),
        "business_name": "Clinica X",
        "logo_asset_id": _ASSET_GROUP_LOGO_ID,
        "marketing_image_asset_id": _ASSET_GROUP_MARKETING_ID,
        "square_image_asset_id": _ASSET_GROUP_SQUARE_ID,
    }
    defaults.update(overrides)
    return PlannedAssetGroupInput(**defaults)  # type: ignore[arg-type]


def _asset_group_ad_set_input(**overrides: object) -> PlannedAdSetInput:
    defaults: dict[str, object] = {
        "local_ref": AdSetRef("as#1"),
        "name": "Grupo de recursos 1",
        "audience": Audience(
            plain="Mujeres y hombres de 25 a 65 años en Valencia",
            native={"targeting_mode": "INHERIT_CAMPAIGN"},
        ),
        "native": _asset_group_input(),
        "ads": (),
    }
    defaults.update(overrides)
    return PlannedAdSetInput(**defaults)  # type: ignore[arg-type]


class TestAssetGroupResolution:
    """T028 hand-off: el decodificador de `mcp.presentation.package_tools`
    se detiene en `PlannedAssetGroupInput` (sin resolver); esta clase cubre
    la resolucion que completa `_resolve_ad_set` en esta misma unidad de
    aplicacion -- reutiliza la MISMA allow-list de destino
    (`_require_allowed_host`) y la MISMA `find_usable` que un anuncio
    corriente (T042 parcial, threat-model.md BL-2)."""

    def _fixture_for_pmax(self) -> tuple[Fixture, ProposeCampaignPackageRequest]:
        fixture = Fixture()
        fixture.account = account_ref(fixture.business, platform=PlatformCode.GOOGLE)
        fixture.accounts = FakeActiveAccountLookupPort(account=fixture.account)
        fixture.creative_lookup = FakeCreativeAssetLookupPort(
            usable={
                _ASSET_GROUP_LOGO_ID: _asset_group_snapshot(
                    _ASSET_GROUP_LOGO_ID, width=1080, height=1080
                ),
                _ASSET_GROUP_MARKETING_ID: _asset_group_snapshot(
                    _ASSET_GROUP_MARKETING_ID, width=1200, height=628
                ),
                _ASSET_GROUP_SQUARE_ID: _asset_group_snapshot(
                    _ASSET_GROUP_SQUARE_ID, width=1080, height=1080
                ),
            }
        )
        request = fixture.request(
            platform=PlatformCode.GOOGLE,
            campaign=google_campaign(native=performance_max_campaign_native()),
            ad_sets=(_asset_group_ad_set_input(),),
        )
        return fixture, request

    async def test_grupo_de_recursos_se_resuelve_y_el_paquete_se_propone(self) -> None:
        fixture, request = self._fixture_for_pmax()

        package = await fixture.use_case().execute(request)

        native = package.ad_sets[0].native
        assert isinstance(native, GoogleAssetGroupNative)
        assert str(native.final_url) == _ASSET_GROUP_LANDING
        assert native.assets.business_name == "Clinica X"

    async def test_imagen_de_grupo_de_recursos_inexistente_da_creative_not_usable(self) -> None:
        fixture, request = self._fixture_for_pmax()
        fixture.creative_lookup = FakeCreativeAssetLookupPort(usable={})

        with pytest.raises(CreativeNotUsableError):
            await fixture.use_case().execute(request)

    async def test_final_url_de_grupo_de_recursos_fuera_de_allowlist_deniega(self) -> None:
        fixture, request = self._fixture_for_pmax()
        fixture.landing_policy = FakeLandingDomainPolicyPort(
            hosts=frozenset({"notclinicax.example"})
        )

        with pytest.raises(LandingUrlNotAllowedError):
            await fixture.use_case().execute(request)

    async def test_final_url_de_grupo_de_recursos_en_subdominio_permitido_no_deniega(self) -> None:
        fixture, request = self._fixture_for_pmax()
        subdomain_ad_set = _asset_group_ad_set_input(
            native=_asset_group_input(final_url="https://www.clinicax.example/reservar")
        )
        request = fixture.request(
            platform=PlatformCode.GOOGLE,
            campaign=google_campaign(native=performance_max_campaign_native()),
            ad_sets=(subdomain_ad_set,),
        )

        package = await fixture.use_case().execute(request)

        assert str(package.package_id) in fixture.repo.by_id

    async def test_imagen_de_grupo_de_recursos_de_otro_negocio_y_inexistente_dan_el_mismo_error(
        self,
    ) -> None:
        # "Ajena" y "inexistente" comparten camino en `find_usable` (ME-7):
        # ambas son, sencillamente, ausencia de la clave en el registro --
        # nunca dos codigos distintos para el mismo hecho de cara al dueño.
        without_marketing_image, request = self._fixture_for_pmax()
        without_marketing_image.creative_lookup = FakeCreativeAssetLookupPort(
            usable={
                _ASSET_GROUP_LOGO_ID: _asset_group_snapshot(
                    _ASSET_GROUP_LOGO_ID, width=1080, height=1080
                ),
                _ASSET_GROUP_SQUARE_ID: _asset_group_snapshot(
                    _ASSET_GROUP_SQUARE_ID, width=1080, height=1080
                ),
            }
        )
        with pytest.raises(CreativeNotUsableError) as foreign:
            await without_marketing_image.use_case().execute(request)

        nothing_usable, request = self._fixture_for_pmax()
        nothing_usable.creative_lookup = FakeCreativeAssetLookupPort(usable={})
        with pytest.raises(CreativeNotUsableError) as nonexistent:
            await nothing_usable.use_case().execute(request)

        assert foreign.value.reason == nonexistent.value.reason == "creative_not_usable"
        assert foreign.value.ad_local_ref == nonexistent.value.ad_local_ref == "as#1"

    async def test_grupo_de_recursos_con_pocos_titulares_da_asset_group_incomplete(self) -> None:
        """`ASSET_GROUP_INCOMPLETE` (contracts/mcp-tools.md §5): el dominio
        (`AssetGroupAssetPlan.__post_init__`) ya exige un minimo de tres
        titulares -- lo que faltaba era traducir su codigo."""
        fixture, request = self._fixture_for_pmax()
        request = fixture.request(
            platform=PlatformCode.GOOGLE,
            campaign=google_campaign(native=performance_max_campaign_native()),
            ad_sets=(
                _asset_group_ad_set_input(
                    native=_asset_group_input(headlines=("Reserva ya", "Cita veterinaria"))
                ),
            ),
        )

        with pytest.raises(AssetGroupIncompleteError) as excinfo:
            await fixture.use_case().execute(request)
        assert excinfo.value.code == "ASSET_GROUP_INCOMPLETE"
        assert excinfo.value.missing == ("asset_group_headline",)

    async def test_grupo_de_recursos_con_logo_no_cuadrado_da_aspect_ratio_invalid(self) -> None:
        """`CREATIVE_ASPECT_RATIO_INVALID` (contracts/mcp-tools.md §5): el
        logo debe ser 1:1 -- lo exige el dominio, lo traduce esta capa."""
        fixture, request = self._fixture_for_pmax()
        fixture.creative_lookup = FakeCreativeAssetLookupPort(
            usable={
                _ASSET_GROUP_LOGO_ID: _asset_group_snapshot(
                    _ASSET_GROUP_LOGO_ID, width=1200, height=628
                ),
                _ASSET_GROUP_MARKETING_ID: _asset_group_snapshot(
                    _ASSET_GROUP_MARKETING_ID, width=1200, height=628
                ),
                _ASSET_GROUP_SQUARE_ID: _asset_group_snapshot(
                    _ASSET_GROUP_SQUARE_ID, width=1080, height=1080
                ),
            }
        )

        with pytest.raises(CreativeAspectRatioInvalidError) as excinfo:
            await fixture.use_case().execute(request)
        assert excinfo.value.code == "CREATIVE_ASPECT_RATIO_INVALID"
        assert excinfo.value.expected == "1:1"
        assert excinfo.value.ad_local_ref == "as#1"
