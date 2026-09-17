"""`ProposeCampaignPackage` (tasks.md T021): valida el arbol completo,
resuelve la cuenta activa y el `publish_as` de Meta, comprueba el sobre de
presupuesto y el tope diario, deduplica por `(business_id, account_ref,
offering_id)` y persiste un `CampaignPackage` ya `proposed` (FR-14: una
sola llamada). Nunca toca una plataforma (contracts/mcp-tools.md regla 1).

`ProposeCampaignPackageRequest` recibe el arbol CASI completo: los anuncios
llegan como `PlannedAdInput` (con `creative_asset_id: str | None` en vez de
un `AdCreativeRef` ya resuelto) porque resolver la creatividad exige E/S
(`CreativeAssetLookupPort`) que no le corresponde a la capa de
presentacion -- mismo motivo por el que `landing_url` llega como `str` y
se valida aqui contra la allow-list de la marca, no antes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from urllib.parse import urlsplit

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
    PackageApplicationError,
    PackageStructureInvalidError,
    PlatformNativeIncompleteError,
)
from safent_ads.packages.application.ports import (
    AccountDailyCapPort,
    ActiveAccountLookupPort,
    AmbiguousActiveAccountForPlatformError,
    BudgetEnvelopeReadPort,
    CampaignPackageRepository,
    CreativeAssetLookupPort,
    LandingDomainPolicyPort,
    OfferingExistsPort,
    PackageBudgetEnvelope,
    PublishAsLookupPort,
)
from safent_ads.packages.domain.campaign_package import CampaignPackage
from safent_ads.packages.domain.errors import (
    PackageBudgetError,
    PackageStructureError,
    PlannedTreeError,
    PlatformCompletenessError,
)
from safent_ads.packages.domain.identifiers import OfferingId, PackageGroupId, PackageId
from safent_ads.packages.domain.planned_tree import (
    AdRef,
    AdSetNative,
    AdSetRef,
    CampaignNative,
    DeliverySchedule,
    GoogleAssetGroupNative,
    GoogleCampaignNative,
    PlannedAd,
    PlannedAdSet,
    PlannedCampaign,
)
from safent_ads.packages.domain.values import (
    AdCopyPlan,
    AdCreativeRef,
    AssetGroupAssetPlan,
    Audience,
    CallToAction,
    ImageCreativeRef,
    KeywordPlan,
    LandingUrl,
    MetaPublishAs,
    PackageBudget,
    PackageRationale,
    ResearchSummary,
    TextOnlyCreativeRef,
)
from safent_ads.proposals.domain.google_channel_spec import GoogleAdvertisingChannelType
from safent_ads.proposals.domain.money import Money
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import BusinessId, EntityLevel, EntityRef, PlatformCode

__all__ = [
    "PlannedAdInput",
    "PlannedAdSetInput",
    "PlannedAssetGroupInput",
    "ProposeCampaignPackage",
    "ProposeCampaignPackageRequest",
]

# Assumption documentada (mismo criterio que `proposals.domain.priority.
# ExpiryPolicy.normal_ttl`, spec.md pregunta 5): un paquete propuesto que
# nadie revisa no debe quedar abierto indefinidamente ni bloquear la
# deduplicacion de FR-20 para siempre.
_PACKAGE_EXPIRY_TTL = timedelta(hours=72)
_MINOR_UNITS_PER_MAJOR = Decimal(100)


@dataclass(frozen=True, kw_only=True, slots=True)
class PlannedAdInput:
    local_ref: AdRef
    name: str
    creative_asset_id: str | None
    landing_url: str
    copy: AdCopyPlan
    cta: CallToAction | None


@dataclass(frozen=True, kw_only=True, slots=True)
class PlannedAssetGroupInput:
    """El grupo de recursos de PERFORMANCE_MAX antes de resolver sus tres
    imagenes (tasks.md T028): mismo motivo que `PlannedAdInput.
    creative_asset_id` -- convertir un `asset_id` de cable en el
    `ImageCreativeRef` verificado exige `CreativeAssetLookupPort` (E/S), que
    no le corresponde al decodificador `Args -> dominio` de la capa de
    presentacion."""

    final_url: str
    headlines: tuple[str, ...]
    long_headlines: tuple[str, ...]
    descriptions: tuple[str, ...]
    business_name: str
    logo_asset_id: str
    marketing_image_asset_id: str
    square_image_asset_id: str


@dataclass(frozen=True, kw_only=True, slots=True)
class PlannedAdSetInput:
    local_ref: AdSetRef
    name: str
    audience: Audience
    native: AdSetNative | PlannedAssetGroupInput
    ads: tuple[PlannedAdInput, ...]
    schedule: DeliverySchedule | None = None
    keywords: KeywordPlan | None = None
    cpc_bid: Money | None = None


@dataclass(frozen=True, kw_only=True, slots=True)
class ProposeCampaignPackageRequest:
    business_id: BusinessId
    platform: PlatformCode
    account_ref: EntityRef | None
    offering_id: OfferingId
    campaign: PlannedCampaign
    ad_sets: tuple[PlannedAdSetInput, ...]
    rationale: PackageRationale
    research: ResearchSummary | None
    package_group_id: PackageGroupId | None = None


class ProposeCampaignPackage:
    def __init__(
        self,
        *,
        packages: CampaignPackageRepository,
        offerings: OfferingExistsPort,
        accounts: ActiveAccountLookupPort,
        daily_caps: AccountDailyCapPort,
        budget_envelope: BudgetEnvelopeReadPort,
        publish_as_lookup: PublishAsLookupPort,
        landing_policy: LandingDomainPolicyPort,
        creative_lookup: CreativeAssetLookupPort,
        clock: Clock,
        enabled_google_channels: frozenset[GoogleAdvertisingChannelType] = frozenset(
            {GoogleAdvertisingChannelType.SEARCH}
        ),
    ) -> None:
        self._packages = packages
        self._offerings = offerings
        self._accounts = accounts
        self._daily_caps = daily_caps
        self._budget_envelope = budget_envelope
        self._publish_as_lookup = publish_as_lookup
        self._landing_policy = landing_policy
        self._creative_lookup = creative_lookup
        self._clock = clock
        self._enabled_google_channels = enabled_google_channels

    async def execute(self, request: ProposeCampaignPackageRequest) -> CampaignPackage:
        # T076 (POLISH): defence in depth, before any port I/O -- the
        # presentation boundary (`mcp.presentation.package_tools`) already
        # rejects this with the same code; this repeats it in case a future
        # caller builds the request without going through that boundary.
        self._require_enabled_channel(request.campaign.native)
        if not await self._offerings.exists(
            business_id=request.business_id, offering_id=str(request.offering_id)
        ):
            raise OfferingNotFoundError(str(request.offering_id))

        account_ref = await self._resolve_account(request)
        publish_as = await self._resolve_publish_as(request, account_ref)
        daily_cap = await self._daily_caps.get_daily_cap(account_ref=account_ref)
        budget = await self._derive_budget(request, daily_cap)
        await self._require_no_open_duplicate(request, account_ref)

        allowed_hosts = await self._landing_policy.allowed_hosts(business_id=request.business_id)
        ad_sets = tuple(
            [
                await self._resolve_ad_set(ad_set, request.business_id, allowed_hosts)
                for ad_set in request.ad_sets
            ]
        )

        package = self._build_package(
            request,
            account_ref=account_ref,
            publish_as=publish_as,
            budget=budget,
            ad_sets=ad_sets,
            now=self._clock.now(),
        )
        await self._packages.add(package)
        return package

    def _require_enabled_channel(self, native: CampaignNative) -> None:
        if not isinstance(native, GoogleCampaignNative):
            return
        if native.advertising_channel_type not in self._enabled_google_channels:
            raise ChannelTypeNotEnabledError(channel=native.advertising_channel_type.value)

    async def _require_no_open_duplicate(
        self, request: ProposeCampaignPackageRequest, account_ref: EntityRef
    ) -> None:
        existing = await self._packages.find_open_duplicate(
            business_id=request.business_id,
            account_ref=account_ref,
            offering_id=request.offering_id,
        )
        if existing is not None:
            raise DuplicateOpenPackageError(package_id=str(existing))

    def _build_package(
        self,
        request: ProposeCampaignPackageRequest,
        *,
        account_ref: EntityRef,
        publish_as: MetaPublishAs | None,
        budget: PackageBudget,
        ad_sets: tuple[PlannedAdSet, ...],
        now: datetime,
    ) -> CampaignPackage:
        try:
            return CampaignPackage.propose(
                package_id=PackageId.new(),
                business_id=request.business_id,
                account_ref=account_ref,
                publish_as=publish_as,
                offering_id=request.offering_id,
                campaign=request.campaign,
                ad_sets=ad_sets,
                budget=budget,
                rationale=request.rationale,
                research=request.research,
                now=now,
                expires_at=now + _PACKAGE_EXPIRY_TTL,
                package_group_id=request.package_group_id,
            )
        except PlatformCompletenessError as exc:
            raise PlatformNativeIncompleteError(missing=(str(exc),)) from exc
        except (PackageStructureError, PlannedTreeError) as exc:
            raise PackageStructureInvalidError(str(exc)) from exc

    async def _resolve_account(self, request: ProposeCampaignPackageRequest) -> EntityRef:
        if request.account_ref is not None and (
            request.account_ref.level != EntityLevel.ACCOUNT
            or request.account_ref.platform != request.platform
            or request.account_ref.business_id != request.business_id.value
        ):
            raise NoActiveAccountForPlatformError(request.platform.value)
        try:
            account_ref = await self._accounts.find_active_account(
                business_id=request.business_id,
                platform=request.platform,
                account_ref=request.account_ref,
            )
        except AmbiguousActiveAccountForPlatformError as exc:
            raise AmbiguousAccountError(candidates=()) from exc
        if account_ref is None:
            raise NoActiveAccountForPlatformError(request.platform.value)
        return account_ref

    async def _resolve_publish_as(
        self, request: ProposeCampaignPackageRequest, account_ref: EntityRef
    ) -> MetaPublishAs | None:
        if request.platform is not PlatformCode.META:
            return None
        resolved = await self._publish_as_lookup.resolve(account_ref=account_ref)
        if resolved is None:
            raise PlatformNativeIncompleteError(missing=("publish_as",))
        return MetaPublishAs(page_id=resolved.page_id, page_name=resolved.page_name)

    async def _derive_budget(
        self, request: ProposeCampaignPackageRequest, daily_cap: Money | None
    ) -> PackageBudget:
        envelope = await self._budget_envelope.get_budget_envelope(str(request.business_id))
        headroom = _headroom_money(envelope)
        daily = request.campaign.daily_budget
        try:
            return PackageBudget.derive(
                daily=daily,
                duration_days=request.campaign.duration_days,
                account_daily_cap=daily_cap,
                envelope_headroom=headroom,
                envelope_reason=envelope.reason,
            )
        except PackageBudgetError as exc:
            if "envelope" in str(exc):
                total_cap = daily.scaled_by(Decimal(request.campaign.duration_days))
                raise BudgetEnvelopeExceededError(
                    headroom=str(headroom), requested=str(total_cap)
                ) from exc
            raise DailyBudgetExceedsCapError(str(exc)) from exc

    async def _resolve_ad_set(
        self,
        ad_set: PlannedAdSetInput,
        business_id: BusinessId,
        allowed_hosts: frozenset[str],
    ) -> PlannedAdSet:
        native = ad_set.native
        if isinstance(native, PlannedAssetGroupInput):
            native = await self._resolve_asset_group(
                native, business_id, allowed_hosts, ad_set.local_ref
            )
        ads = tuple(
            [await self._resolve_ad(ad, business_id, allowed_hosts) for ad in ad_set.ads]
        )
        return PlannedAdSet(
            local_ref=ad_set.local_ref,
            name=ad_set.name,
            audience=ad_set.audience,
            native=native,
            ads=ads,
            schedule=ad_set.schedule,
            keywords=ad_set.keywords,
            cpc_bid=ad_set.cpc_bid,
        )

    async def _resolve_asset_group(
        self,
        native: PlannedAssetGroupInput,
        business_id: BusinessId,
        allowed_hosts: frozenset[str],
        ad_set_ref: AdSetRef,
    ) -> GoogleAssetGroupNative:
        """BL-2 (threat-model.md, tasks.md T042 parcial): `final_url` ES la
        unica pagina que sirve este canal (la expansion de URL viene
        forzada a apagada), asi que pasa por la MISMA allow-list de destino
        que `landing_url`; las tres imagenes pasan por la MISMA
        `find_usable` que una creatividad de anuncio -- ninguna de las dos
        comprobaciones se reimplementa. La verificacion de aspecto (1:1,
        1.91:1) la exige `AssetGroupAssetPlan.__post_init__` (dominio),
        traducida aqui a los codigos del contrato (§5)."""
        _require_allowed_host(native.final_url, allowed_hosts, str(ad_set_ref))
        logo = await self._resolve_image(native.logo_asset_id, business_id, str(ad_set_ref))
        marketing_image = await self._resolve_image(
            native.marketing_image_asset_id, business_id, str(ad_set_ref)
        )
        square_image = await self._resolve_image(
            native.square_image_asset_id, business_id, str(ad_set_ref)
        )
        try:
            assets = AssetGroupAssetPlan(
                headlines=native.headlines,
                long_headlines=native.long_headlines,
                descriptions=native.descriptions,
                business_name=native.business_name,
                logo=logo,
                marketing_image=marketing_image,
                square_image=square_image,
            )
        except PlannedTreeError as exc:
            raise _translate_asset_group_error(exc, str(ad_set_ref)) from exc
        return GoogleAssetGroupNative(final_url=LandingUrl(native.final_url), assets=assets)

    async def _resolve_ad(
        self, ad: PlannedAdInput, business_id: BusinessId, allowed_hosts: frozenset[str]
    ) -> PlannedAd:
        _require_allowed_host(ad.landing_url, allowed_hosts, str(ad.local_ref))
        creative = await self._resolve_creative(ad, business_id)
        return PlannedAd(
            local_ref=ad.local_ref,
            name=ad.name,
            creative=creative,
            copy=ad.copy,
            landing=LandingUrl(ad.landing_url),
            cta=ad.cta,
        )

    async def _resolve_creative(self, ad: PlannedAdInput, business_id: BusinessId) -> AdCreativeRef:
        if ad.creative_asset_id is None:
            return TextOnlyCreativeRef()
        return await self._resolve_image(ad.creative_asset_id, business_id, str(ad.local_ref))

    async def _resolve_image(
        self, asset_id: str, business_id: BusinessId, local_ref: str
    ) -> ImageCreativeRef:
        snapshot = await self._creative_lookup.find_usable(
            business_id=business_id, asset_id=asset_id
        )
        if snapshot is None:
            raise CreativeNotUsableError(ad_local_ref=local_ref)
        return ImageCreativeRef(
            asset_id=AssetId.parse(snapshot.asset_id),
            checksum=snapshot.checksum,
            preview_key=snapshot.preview_key,
            mime_type=snapshot.mime_type,
            width=snapshot.width,
            height=snapshot.height,
        )


_ASSET_GROUP_ASPECT_RATIOS: dict[str, str] = {
    "asset_group_logo": "1:1",
    "asset_group_square_image": "1:1",
    "asset_group_marketing_image": "1.91:1",
}


def _asset_group_missing_field(code: str) -> str | None:
    for suffix in ("_count_invalid", "_invalid", "_duplicate"):
        if code.startswith("asset_group_") and code.endswith(suffix):
            return code.removesuffix(suffix)
    return None


def _translate_asset_group_error(
    exc: PlannedTreeError, ad_local_ref: str
) -> PackageApplicationError:
    """`AssetGroupAssetPlan.__post_init__` (dominio) ya exige recuento de
    titulares/descripciones y relacion de aspecto de imagen -- lo que
    faltaba era traducir sus codigos a `ASSET_GROUP_INCOMPLETE`/
    `CREATIVE_ASPECT_RATIO_INVALID` (contracts/mcp-tools.md §5)."""
    code = str(exc)
    for field, expected in _ASSET_GROUP_ASPECT_RATIOS.items():
        if code == f"{field}_aspect_ratio_invalid":
            return CreativeAspectRatioInvalidError(expected=expected, ad_local_ref=ad_local_ref)
    missing_field = _asset_group_missing_field(code)
    if missing_field is not None:
        return AssetGroupIncompleteError(missing=(missing_field,))
    return PackageStructureInvalidError(code)


def _headroom_money(envelope: PackageBudgetEnvelope) -> Money | None:
    if envelope.headroom_minor is None or envelope.currency is None:
        return None
    return Money(Decimal(envelope.headroom_minor) / _MINOR_UNITS_PER_MAJOR, envelope.currency)


def _require_allowed_host(
    landing_url: str, allowed_hosts: frozenset[str], ad_local_ref: str
) -> None:
    host = (urlsplit(landing_url).hostname or "").lower()
    if not allowed_hosts or not any(
        host == permitted or host.endswith(f".{permitted}") for permitted in allowed_hosts
    ):
        raise LandingUrlNotAllowedError(ad_local_ref=ad_local_ref, expected_domains=allowed_hosts)
