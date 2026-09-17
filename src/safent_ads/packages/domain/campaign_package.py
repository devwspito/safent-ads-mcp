"""`CampaignPackage` -- agregado raiz del contexto `packages` (data-model.md).
Une campana + conjuntos + anuncios + dinero + porque en la unidad que el
dueño revisa y aprueba de una sola vez (FR-01, FR-14). T015.

`publication_plan()` (data-model.md "Comportamiento") queda **fuera** de
este fichero a proposito: devuelve `tuple[PublicationStepSpec, ...]`, un
tipo que solo existira cuando `step_projection.py` (T016) aterrice -- esa
tarea depende del veredicto de amenaza y esta siendo redisenada
(threat-model.md BL-2). `begin_publishing`/`record_publication_outcome` no
necesitan ese tipo: transicionan el estado con lo que ya vive aqui."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from safent_ads.packages.domain.errors import (
    CampaignPackageInvariantError,
    PackageHashMismatchError,
    PackageStructureError,
)
from safent_ads.packages.domain.identifiers import OfferingId, PackageGroupId, PackageId
from safent_ads.packages.domain.package_hash import (
    PackageHash,
    compute_package_hash,
    package_tree_payload,
)
from safent_ads.packages.domain.planned_tree import (
    AdRef,
    GoogleAdGroupNative,
    GoogleAssetGroupNative,
    PlannedAd,
    PlannedAdSet,
    PlannedCampaign,
)
from safent_ads.packages.domain.platform_completeness import (
    validate_ad_set_completeness,
    validate_campaign_completeness,
)
from safent_ads.packages.domain.values import (
    AdCreativeRef,
    ImageCreativeRef,
    MetaPublishAs,
    PackageBudget,
    PackageRationale,
    ResearchSummary,
)
from safent_ads.shared.events import DomainEvent
from safent_ads.shared.ids import BusinessId, EntityLevel, EntityRef

_MIN_AD_SETS = 1
_MAX_AD_SETS = 3
_MAX_ADS_TOTAL = 12
# data-model.md ("Invariante": tope duro nuevo, T022) -- la subida es el
# paso caro, no el numero de anuncios: un checksum distinto es una subida
# distinta, se comparta o no entre varios papeles (`asset_group.logo` y
# `.square_image` pueden compartir bytes, R2.7 de 003 intacta).
_MAX_DISTINCT_PACKAGE_IMAGES = 20
MAX_OWNER_CONTEXT_LENGTH = 500


class PackageState(StrEnum):
    """data-model.md "Estado / ciclo de vida": 11 estados."""

    DRAFT = "draft"
    PROPOSED = "proposed"
    APPROVED = "approved"
    PUBLISHING = "publishing"
    VERIFYING = "verifying"
    PUBLISHED = "published"
    PARTIALLY_PUBLISHED = "partially_published"
    FAILED = "failed"
    REJECTED = "rejected"
    EXPIRED = "expired"
    INVALIDATED = "invalidated"


_TRANSITIONS: dict[PackageState, frozenset[PackageState]] = {
    PackageState.DRAFT: frozenset({PackageState.PROPOSED, PackageState.REJECTED}),
    PackageState.PROPOSED: frozenset(
        {PackageState.APPROVED, PackageState.REJECTED, PackageState.EXPIRED}
    ),
    PackageState.APPROVED: frozenset({PackageState.PUBLISHING, PackageState.INVALIDATED}),
    PackageState.PUBLISHING: frozenset(
        {
            PackageState.PUBLISHED,
            PackageState.PARTIALLY_PUBLISHED,
            PackageState.VERIFYING,
            PackageState.FAILED,
        }
    ),
    PackageState.VERIFYING: frozenset({PackageState.PUBLISHED, PackageState.PARTIALLY_PUBLISHED}),
    PackageState.PUBLISHED: frozenset(),
    # `ResumePackagePublication` (contracts/api.md §R2.C): "reanudar es
    # decidir otra vez" -- no un estado terminal si el sobre humano sigue
    # vivo. `RunPackagePublication` sigue devolviendo aqui via
    # `record_publication_outcome` (invariante 8 de la maquina de estados).
    PackageState.PARTIALLY_PUBLISHED: frozenset({PackageState.PUBLISHING}),
    PackageState.FAILED: frozenset(),
    PackageState.REJECTED: frozenset(),
    PackageState.EXPIRED: frozenset(),
    PackageState.INVALIDATED: frozenset(),
}

_EDITABLE_STATES = frozenset({PackageState.PROPOSED, PackageState.APPROVED})


# ---------------------------------------------------------------------------
# PublicationOutcome -- lo que la saga devuelve al agregado (sum type, no
# flags: cada variante lleva exactamente los datos que su evento necesita)
# ---------------------------------------------------------------------------


class PublicationOutcomeKind(StrEnum):
    ACTIVATED = "activated"
    PARTIAL = "partial"
    NONE_CREATED = "none_created"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class ActivatedOutcome:
    campaign_entity_ref: str
    activated_at: datetime
    undo_deadline: datetime
    kind: PublicationOutcomeKind = field(default=PublicationOutcomeKind.ACTIVATED, init=False)


@dataclass(frozen=True, slots=True)
class PartialOutcome:
    created_count: int
    failed_step_index: int
    next_step_hint: str
    kind: PublicationOutcomeKind = field(default=PublicationOutcomeKind.PARTIAL, init=False)


@dataclass(frozen=True, slots=True)
class NoneCreatedOutcome:
    outcome_code: str
    kind: PublicationOutcomeKind = field(default=PublicationOutcomeKind.NONE_CREATED, init=False)


@dataclass(frozen=True, slots=True)
class UncertainOutcome:
    kind: PublicationOutcomeKind = field(default=PublicationOutcomeKind.UNCERTAIN, init=False)


PublicationOutcome = ActivatedOutcome | PartialOutcome | NoneCreatedOutcome | UncertainOutcome

_OUTCOME_TARGET: dict[PublicationOutcomeKind, PackageState] = {
    PublicationOutcomeKind.ACTIVATED: PackageState.PUBLISHED,
    PublicationOutcomeKind.PARTIAL: PackageState.PARTIALLY_PUBLISHED,
    PublicationOutcomeKind.NONE_CREATED: PackageState.FAILED,
    PublicationOutcomeKind.UNCERTAIN: PackageState.VERIFYING,
}


# ---------------------------------------------------------------------------
# Domain events
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class CampaignPackageProposed(DomainEvent):
    package_id: str
    platform: str
    account_ref: str
    package_hash: str
    ads_count: int
    daily_budget: str


@dataclass(frozen=True, kw_only=True)
class CampaignPackageApproved(DomainEvent):
    package_id: str
    package_hash: str
    authorization_id: str
    grace_seconds: int


@dataclass(frozen=True, kw_only=True)
class CampaignPackageRejected(DomainEvent):
    package_id: str
    reason: str | None = None


@dataclass(frozen=True, kw_only=True)
class CampaignPackageExpired(DomainEvent):
    package_id: str


@dataclass(frozen=True, kw_only=True)
class CampaignPackageInvalidated(DomainEvent):
    package_id: str
    reason: str


@dataclass(frozen=True, kw_only=True)
class CampaignPackagePublished(DomainEvent):
    package_id: str
    campaign_entity_ref: str
    activated_at: str
    undo_deadline: str


@dataclass(frozen=True, kw_only=True)
class CampaignPackagePartiallyPublished(DomainEvent):
    package_id: str
    created_count: int
    failed_step_index: int
    next_step_hint: str


@dataclass(frozen=True, kw_only=True)
class CampaignPackageFailed(DomainEvent):
    package_id: str
    outcome_code: str


# ---------------------------------------------------------------------------
# Structural invariants checked once, at `propose`
# ---------------------------------------------------------------------------


def _require_account_scope(business_id: BusinessId, account_ref: EntityRef) -> None:
    """threat-model.md BL-1: la huella solo ata de verdad la cuenta si
    `account_ref` viaja con `business_id`/`connection_id` -- un `EntityRef`
    sin alcance (ambos `None`) se rechaza aqui, igual que la regla de
    admision 5 del paso firmado rechazara un padre sin alcance."""
    if account_ref.level is not EntityLevel.ACCOUNT:
        raise PackageStructureError("package_account_ref_must_be_account_level")
    if account_ref.business_id is None or account_ref.connection_id is None:
        raise PackageStructureError("package_account_ref_missing_scope")
    if account_ref.business_id != business_id.value:
        raise PackageStructureError("package_account_ref_business_mismatch")


def _require_campaign_platform_matches_account(
    campaign: PlannedCampaign, account_ref: EntityRef
) -> None:
    if campaign.platform != account_ref.platform:
        raise PackageStructureError("package_platform_mismatch")


def _require_publish_as_matches_platform(
    campaign: PlannedCampaign, publish_as: MetaPublishAs | None
) -> None:
    """data-model.md Revision 2 R2.1: `publish_as` es la pagina Meta ya
    existente desde la que se publica -- obligatoria en Meta (el dueño la
    ve: «Se publicará como «Clínica X»»), inexistente en Google."""
    is_meta = campaign.platform.value == "meta"
    if is_meta and publish_as is None:
        raise PackageStructureError("package_publish_as_required")
    if not is_meta and publish_as is not None:
        raise PackageStructureError("package_publish_as_forbidden")


def _require_tree_bounds(ad_sets: tuple[PlannedAdSet, ...]) -> None:
    if not (_MIN_AD_SETS <= len(ad_sets) <= _MAX_AD_SETS):
        raise PackageStructureError("package_ad_sets_count_invalid")
    if sum(len(ad_set.ads) for ad_set in ad_sets) > _MAX_ADS_TOTAL:
        raise PackageStructureError("package_ads_total_invalid")


def _package_image_checksums(ad_sets: tuple[PlannedAdSet, ...]) -> frozenset[str]:
    checksums: set[str] = set()
    for ad_set in ad_sets:
        if isinstance(ad_set.native, GoogleAssetGroupNative):
            assets = ad_set.native.assets
            checksums.update(
                (
                    assets.logo.checksum,
                    assets.marketing_image.checksum,
                    assets.square_image.checksum,
                )
            )
        checksums.update(
            ad.creative.checksum for ad in ad_set.ads if isinstance(ad.creative, ImageCreativeRef)
        )
    return frozenset(checksums)


def _require_distinct_image_limit(ad_sets: tuple[PlannedAdSet, ...]) -> None:
    if len(_package_image_checksums(ad_sets)) > _MAX_DISTINCT_PACKAGE_IMAGES:
        raise PackageStructureError("package_distinct_images_limit_exceeded")


def _require_positional_ad_set_refs(ad_sets: tuple[PlannedAdSet, ...]) -> None:
    """threat-model.md BL-2: la posicion en la tupla debe coincidir siempre
    con el ordinal declarado, para que un futuro `step_index` se derive del
    arbol sin ambiguedad (`planned_tree.parent_local_ref`)."""
    for position, ad_set in enumerate(ad_sets, start=1):
        if ad_set.local_ref.value != f"as#{position}":
            raise PackageStructureError("package_ad_set_ref_out_of_position")


def _require_ad_set_platform_matches_campaign(
    campaign: PlannedCampaign, ad_sets: tuple[PlannedAdSet, ...]
) -> None:
    for ad_set in ad_sets:
        is_google_ad_set = isinstance(ad_set.native, GoogleAdGroupNative | GoogleAssetGroupNative)
        is_google_campaign = campaign.platform.value == "google"
        if is_google_ad_set != is_google_campaign:
            raise PackageStructureError("package_ad_set_platform_mismatch")


def _validate_planned_tree(
    business_id: BusinessId,
    account_ref: EntityRef,
    publish_as: MetaPublishAs | None,
    campaign: PlannedCampaign,
    ad_sets: tuple[PlannedAdSet, ...],
) -> None:
    _require_account_scope(business_id, account_ref)
    _require_campaign_platform_matches_account(campaign, account_ref)
    _require_publish_as_matches_platform(campaign, publish_as)
    _require_tree_bounds(ad_sets)
    _require_distinct_image_limit(ad_sets)
    _require_positional_ad_set_refs(ad_sets)
    _require_ad_set_platform_matches_campaign(campaign, ad_sets)
    validate_campaign_completeness(campaign, account_ref)
    for ad_set in ad_sets:
        validate_ad_set_completeness(ad_set, account_ref.platform)


def _require_ad_exists(ad_sets: tuple[PlannedAdSet, ...], ad_ref: AdRef) -> None:
    for ad_set in ad_sets:
        if ad_set.local_ref.value == ad_ref.ad_set_ref:
            if any(ad.local_ref.value == ad_ref.value for ad in ad_set.ads):
                return
            break
    raise CampaignPackageInvariantError(f"anuncio no encontrado para {ad_ref}")


def _with_creative(ad: PlannedAd, creative: AdCreativeRef) -> PlannedAd:
    return PlannedAd(
        local_ref=ad.local_ref,
        name=ad.name,
        creative=creative,
        copy=ad.copy,
        landing=ad.landing,
        cta=ad.cta,
    )


def _replace_ad_in_set(
    ad_set: PlannedAdSet, ad_ref: AdRef, creative: AdCreativeRef
) -> PlannedAdSet:
    new_ads = tuple(
        _with_creative(ad, creative) if ad.local_ref.value == ad_ref.value else ad
        for ad in ad_set.ads
    )
    return PlannedAdSet(
        local_ref=ad_set.local_ref,
        name=ad_set.name,
        audience=ad_set.audience,
        native=ad_set.native,
        ads=new_ads,
        schedule=ad_set.schedule,
        keywords=ad_set.keywords,
        cpc_bid=ad_set.cpc_bid,
    )


def _replace_ad_creative(
    ad_sets: tuple[PlannedAdSet, ...], ad_ref: AdRef, creative: AdCreativeRef
) -> tuple[PlannedAdSet, ...]:
    target_ad_set_ref = ad_ref.ad_set_ref
    return tuple(
        _replace_ad_in_set(ad_set, ad_ref, creative)
        if ad_set.local_ref.value == target_ad_set_ref
        else ad_set
        for ad_set in ad_sets
    )


# ---------------------------------------------------------------------------
# Aggregate root
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CampaignPackage:
    """Agregado raiz -- la unidad de revision y aprobacion humana
    (FR-01, data-model.md)."""

    package_id: PackageId
    business_id: BusinessId
    account_ref: EntityRef
    publish_as: MetaPublishAs | None
    offering_id: OfferingId
    campaign: PlannedCampaign
    ad_sets: tuple[PlannedAdSet, ...]
    budget: PackageBudget
    rationale: PackageRationale
    research: ResearchSummary | None
    created_at: datetime
    expires_at: datetime
    package_hash: PackageHash
    state: PackageState = PackageState.PROPOSED
    package_group_id: PackageGroupId | None = None
    owner_context: str | None = None
    _events: list[DomainEvent] = field(default_factory=list)

    @classmethod
    def propose(
        cls,
        *,
        package_id: PackageId,
        business_id: BusinessId,
        account_ref: EntityRef,
        publish_as: MetaPublishAs | None,
        offering_id: OfferingId,
        campaign: PlannedCampaign,
        ad_sets: tuple[PlannedAdSet, ...],
        budget: PackageBudget,
        rationale: PackageRationale,
        research: ResearchSummary | None,
        now: datetime,
        expires_at: datetime,
        package_group_id: PackageGroupId | None = None,
    ) -> CampaignPackage:
        """Valida el arbol completo de una sola vez y nace ya `PROPOSED`
        (FR-14: `propose_campaign_package` acepta el arbol completo en una
        sola llamada; no hay un `draft` observable desde esa herramienta)."""
        _validate_planned_tree(business_id, account_ref, publish_as, campaign, ad_sets)
        package_hash = compute_package_hash(
            package_tree_payload(
                business_id=business_id,
                platform=account_ref.platform,
                account_ref=account_ref,
                publish_as=publish_as,
                offering_id=offering_id,
                campaign=campaign,
                ad_sets=ad_sets,
                daily_budget=budget.daily,
                rationale=rationale,
                research=research,
            )
        )
        package = cls(
            package_id=package_id,
            business_id=business_id,
            account_ref=account_ref,
            publish_as=publish_as,
            offering_id=offering_id,
            campaign=campaign,
            ad_sets=ad_sets,
            budget=budget,
            rationale=rationale,
            research=research,
            created_at=now,
            expires_at=expires_at,
            package_hash=package_hash,
            package_group_id=package_group_id,
        )
        package._events.append(
            CampaignPackageProposed(
                business_id=business_id,
                occurred_at=now,
                package_id=str(package_id),
                platform=account_ref.platform.value,
                account_ref=str(account_ref),
                package_hash=package_hash.value,
                ads_count=sum(len(ad_set.ads) for ad_set in ad_sets),
                daily_budget=str(campaign.daily_budget.amount),
            )
        )
        return package

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------

    def reject(self, now: datetime, reason: str | None = None) -> None:
        self._require_state(PackageState.DRAFT, PackageState.PROPOSED)
        self._transition_to(PackageState.REJECTED)
        self._events.append(
            CampaignPackageRejected(
                business_id=self.business_id,
                occurred_at=now,
                package_id=str(self.package_id),
                reason=reason,
            )
        )

    def expire(self, now: datetime) -> None:
        self._require_state(PackageState.PROPOSED)
        self._transition_to(PackageState.EXPIRED)
        self._events.append(
            CampaignPackageExpired(
                business_id=self.business_id, occurred_at=now, package_id=str(self.package_id)
            )
        )

    def approve(
        self, authorized_hash: PackageHash, authorization_id: str, grace_seconds: int, now: datetime
    ) -> None:
        """PROPOSED -> APPROVED. Rechaza si la huella autorizada no
        coincide con la huella viva (invariante 8)."""
        self._require_state(PackageState.PROPOSED)
        if authorized_hash != self.package_hash:
            raise PackageHashMismatchError(
                f"authorized package_hash {authorized_hash.value!r} != "
                f"live {self.package_hash.value!r}"
            )
        self._transition_to(PackageState.APPROVED)
        self._events.append(
            CampaignPackageApproved(
                business_id=self.business_id,
                occurred_at=now,
                package_id=str(self.package_id),
                package_hash=self.package_hash.value,
                authorization_id=authorization_id,
                grace_seconds=grace_seconds,
            )
        )

    def invalidate(self, reason: str, now: datetime) -> None:
        """APPROVED -> INVALIDATED: gracia de 45 s cancelada (FR-08) o
        cualquier otra deriva que invalide una aprobacion viva."""
        self._require_state(PackageState.APPROVED)
        self._transition_to(PackageState.INVALIDATED)
        self._events.append(
            CampaignPackageInvalidated(
                business_id=self.business_id,
                occurred_at=now,
                package_id=str(self.package_id),
                reason=reason,
            )
        )

    def replace_ad_creative(
        self, ad_ref: AdRef, creative: AdCreativeRef, now: datetime
    ) -> PackageHash:
        """Invariante 8: editar cualquier campo en `proposed`/`approved`
        recalcula la huella; si habia aprobacion viva, la invalida."""
        if self.state not in _EDITABLE_STATES:
            raise CampaignPackageInvariantError(f"paquete no editable en estado {self.state}")
        _require_ad_exists(self.ad_sets, ad_ref)  # 404 semantico si el local_ref no existe
        self.ad_sets = _replace_ad_creative(self.ad_sets, ad_ref, creative)
        self.package_hash = compute_package_hash(
            package_tree_payload(
                business_id=self.business_id,
                platform=self.account_ref.platform,
                account_ref=self.account_ref,
                publish_as=self.publish_as,
                offering_id=self.offering_id,
                campaign=self.campaign,
                ad_sets=self.ad_sets,
                daily_budget=self.budget.daily,
                rationale=self.rationale,
                research=self.research,
            )
        )
        if self.state is PackageState.APPROVED:
            self._transition_to(PackageState.INVALIDATED)
            self._events.append(
                CampaignPackageInvalidated(
                    business_id=self.business_id,
                    occurred_at=now,
                    package_id=str(self.package_id),
                    reason="creative_replaced_after_approval",
                )
            )
        return self.package_hash

    def begin_publishing(self, now: datetime) -> None:  # noqa: ARG002 - `now` reservado para T016
        self._require_state(PackageState.APPROVED)
        self._transition_to(PackageState.PUBLISHING)

    def resume_publishing(self, now: datetime) -> None:  # noqa: ARG002 - simetrico a `begin_publishing`
        """PARTIALLY_PUBLISHED -> PUBLISHING: `ResumePackagePublication`
        reabre el agregado para que `RunPackagePublication` pueda volver a
        llamar `record_publication_outcome` sobre el mismo paquete."""
        self._require_state(PackageState.PARTIALLY_PUBLISHED)
        self._transition_to(PackageState.PUBLISHING)

    def record_publication_outcome(self, outcome: PublicationOutcome, now: datetime) -> None:
        self._require_state(PackageState.PUBLISHING, PackageState.VERIFYING)
        self._transition_to(_OUTCOME_TARGET[outcome.kind])
        event = self._build_outcome_event(outcome, now)
        if event is not None:
            self._events.append(event)

    def set_owner_context(self, text: str) -> None:
        if len(text) > MAX_OWNER_CONTEXT_LENGTH:
            raise CampaignPackageInvariantError(
                f"owner_context supera el maximo de {MAX_OWNER_CONTEXT_LENGTH} caracteres"
            )
        self.owner_context = text

    def pull_events(self) -> list[DomainEvent]:
        events, self._events = self._events, []
        return events

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _build_outcome_event(
        self, outcome: PublicationOutcome, now: datetime
    ) -> DomainEvent | None:
        if isinstance(outcome, ActivatedOutcome):
            return CampaignPackagePublished(
                business_id=self.business_id,
                occurred_at=now,
                package_id=str(self.package_id),
                campaign_entity_ref=outcome.campaign_entity_ref,
                activated_at=outcome.activated_at.isoformat(),
                undo_deadline=outcome.undo_deadline.isoformat(),
            )
        if isinstance(outcome, PartialOutcome):
            return CampaignPackagePartiallyPublished(
                business_id=self.business_id,
                occurred_at=now,
                package_id=str(self.package_id),
                created_count=outcome.created_count,
                failed_step_index=outcome.failed_step_index,
                next_step_hint=outcome.next_step_hint,
            )
        if isinstance(outcome, NoneCreatedOutcome):
            return CampaignPackageFailed(
                business_id=self.business_id,
                occurred_at=now,
                package_id=str(self.package_id),
                outcome_code=outcome.outcome_code,
            )
        return None

    def _transition_to(self, target: PackageState) -> None:
        allowed = _TRANSITIONS.get(self.state, frozenset())
        if target not in allowed:
            raise CampaignPackageInvariantError(f"transicion invalida {self.state} -> {target}")
        self.state = target

    def _require_state(self, *states: PackageState) -> None:
        if self.state not in states:
            raise CampaignPackageInvariantError(f"se esperaba estado en {states}, es {self.state}")
