"""Dobles en memoria de los puertos de `packages.application` (T021/T022/T023)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from safent_ads.packages.application.ports import (
    AmbiguousActiveAccountForPlatformError,
    CreativeAssetSnapshot,
    PackageBudgetEnvelope,
    PackageListItem,
    PackagePublicationRecord,
    PackageStepRecord,
    ResolvedPublishAs,
    StepExecutionOutcome,
)
from safent_ads.packages.domain.approval_envelope import PackageApprovalEnvelope
from safent_ads.packages.domain.campaign_package import CampaignPackage, PackageState
from safent_ads.packages.domain.identifiers import OfferingId, PackageId
from safent_ads.packages.domain.step_binding import PackageStepBinding
from safent_ads.proposals.domain.authorization import Authorization
from safent_ads.proposals.domain.money import Money
from safent_ads.shared.ids import BusinessId, EntityRef, PlatformCode


class FakeCampaignPackageRepository:
    def __init__(self) -> None:
        self.by_id: dict[str, CampaignPackage] = {}
        self.duplicate_of: PackageId | None = None

    async def add(self, package: CampaignPackage) -> None:
        self.by_id[str(package.package_id)] = package

    async def save(self, package: CampaignPackage) -> None:
        self.by_id[str(package.package_id)] = package

    async def get(
        self, package_id: PackageId, *, business_id: BusinessId
    ) -> CampaignPackage | None:
        package = self.by_id.get(str(package_id))
        if package is None or package.business_id != business_id:
            return None
        return package

    async def find_open_duplicate(
        self,
        *,
        business_id: BusinessId,  # noqa: ARG002 - doble en memoria, valor fijo por test
        account_ref: EntityRef,  # noqa: ARG002
        offering_id: OfferingId,  # noqa: ARG002
    ) -> PackageId | None:
        return self.duplicate_of

    async def list_for_business(
        self,
        *,
        business_id: BusinessId,  # noqa: ARG002 - doble en memoria, sin listado en estos tests
        state: PackageState | None,  # noqa: ARG002
        limit: int,  # noqa: ARG002
        cursor: str | None,  # noqa: ARG002
    ) -> tuple[tuple[PackageListItem, ...], str | None]:
        return (), None


class FakeOfferingExistsPort:
    def __init__(self, *, exists: bool = True) -> None:
        self.exists_value = exists

    async def exists(
        self,
        *,
        business_id: BusinessId,  # noqa: ARG002 - doble en memoria, valor fijo por test
        offering_id: str,  # noqa: ARG002
    ) -> bool:
        return self.exists_value


@dataclass
class FakeActiveAccountLookupPort:
    account: EntityRef | None
    ambiguous: bool = False

    async def find_active_account(
        self,
        *,
        business_id: BusinessId,  # noqa: ARG002 - doble en memoria, valor fijo por test
        platform: PlatformCode,  # noqa: ARG002
        account_ref: EntityRef | None = None,  # noqa: ARG002
    ) -> EntityRef | None:
        if self.ambiguous:
            raise AmbiguousActiveAccountForPlatformError("ambiguous")
        return self.account


@dataclass
class FakeAccountDailyCapPort:
    cap: Money | None = None

    async def get_daily_cap(
        self, *, account_ref: EntityRef  # noqa: ARG002 - doble en memoria, valor fijo por test
    ) -> Money | None:
        return self.cap


@dataclass
class FakeBudgetEnvelopeReadPort:
    envelope: PackageBudgetEnvelope = field(
        default_factory=lambda: PackageBudgetEnvelope(
            monthly_cap_minor=None,
            spent_month_to_date_minor=None,
            headroom_minor=None,
            currency=None,
            reason="no_platform_account",
        )
    )

    async def get_budget_envelope(
        self, business_id: str  # noqa: ARG002 - doble en memoria, valor fijo por test
    ) -> PackageBudgetEnvelope:
        return self.envelope


@dataclass
class FakePublishAsLookupPort:
    resolved: ResolvedPublishAs | None

    async def resolve(
        self, *, account_ref: EntityRef  # noqa: ARG002 - doble en memoria, valor fijo por test
    ) -> ResolvedPublishAs | None:
        return self.resolved


@dataclass
class FakeLandingDomainPolicyPort:
    hosts: frozenset[str]

    async def allowed_hosts(
        self, *, business_id: BusinessId  # noqa: ARG002 - doble en memoria, valor fijo por test
    ) -> frozenset[str]:
        return self.hosts


@dataclass
class FakeCreativeAssetLookupPort:
    usable: dict[str, CreativeAssetSnapshot] = field(default_factory=dict)

    async def find_usable(
        self,
        *,
        business_id: BusinessId,  # noqa: ARG002 - doble en memoria, no filtra en el test
        asset_id: str,
    ) -> CreativeAssetSnapshot | None:
        return self.usable.get(asset_id)


class FakePackagePublicationRepository:
    def __init__(self) -> None:
        self.by_package_id: dict[str, PackagePublicationRecord] = {}
        self.by_id: dict[str, PackagePublicationRecord] = {}
        self.advance_calls: list[dict[str, object]] = []

    async def add(self, record: PackagePublicationRecord) -> None:
        self.by_package_id[str(record.package_id)] = record
        self.by_id[record.publication_id] = record

    async def get_by_package_id(self, package_id: PackageId) -> PackagePublicationRecord | None:
        return self.by_package_id.get(str(package_id))

    async def get_by_id(self, publication_id: str) -> PackagePublicationRecord | None:
        return self.by_id.get(publication_id)

    async def advance(
        self,
        publication_id: str,
        *,
        cursor: int,
        state: str,
        halt_reason: str | None,
        failed_step_index: int | None,
        finished_at: datetime | None,
    ) -> None:
        self.advance_calls.append(
            {
                "publication_id": publication_id,
                "cursor": cursor,
                "state": state,
                "halt_reason": halt_reason,
                "failed_step_index": failed_step_index,
                "finished_at": finished_at,
            }
        )
        current = self.by_id[publication_id]
        # M6/M2 (revision de codigo): las tres columnas que `advance()`
        # recibe (`halt_reason`/`failed_step_index`/`finished_at`) se
        # perdian aqui -- `PackagePublicationRecord` las declara con
        # default `None`, asi que un `dataclasses.replace` a medias las
        # reseteaba en silencio en vez de guardar lo que el caso de uso
        # acababa de decidir. Bug de la doble de prueba, no del SQL real
        # (`sql_publication_repository.py` si persiste las tres).
        updated = PackagePublicationRecord(
            publication_id=current.publication_id,
            package_id=current.package_id,
            authorization_id=current.authorization_id,
            state=state,
            cursor=cursor,
            envelope=current.envelope,
            envelope_hash=current.envelope_hash,
            approval_signature=current.approval_signature,
            approval_expires_at=current.approval_expires_at,
            approved_plan=current.approved_plan,
            started_at=current.started_at,
            halt_reason=halt_reason,
            failed_step_index=failed_step_index,
            finished_at=finished_at,
        )
        self.by_id[publication_id] = updated
        self.by_package_id[str(current.package_id)] = updated

    async def list_open(self) -> tuple[str, ...]:
        return tuple(
            publication_id
            for publication_id, record in self.by_id.items()
            if record.state in {"pending", "running"}
        )


class FakePackageAuthorizationRepository:
    def __init__(self) -> None:
        self.saved: list[Authorization] = []

    async def save(self, authorization: Authorization) -> None:
        self.saved.append(authorization)


class FakePackageStepRepository:
    def __init__(self) -> None:
        self.by_key: dict[tuple[str, int], PackageStepRecord] = {}

    async def get(self, publication_id: str, step_index: int) -> PackageStepRecord | None:
        return self.by_key.get((publication_id, step_index))

    async def upsert(self, record: PackageStepRecord) -> None:
        self.by_key[(record.publication_id, record.step_index)] = record


class FakePackageStepExecutorPort:
    """Devuelve un `StepExecutionOutcome` fijo por `step_index`, configurado
    por el test -- `RunPackagePublication` es quien decide que hacer con
    cada desenlace, no este doble."""

    def __init__(self, outcomes_by_step_index: dict[int, StepExecutionOutcome]) -> None:
        self._outcomes = outcomes_by_step_index
        self.calls: list[PackageStepBinding] = []
        self.existing_proposal_ids: list[str | None] = []

    async def execute_step(
        self,
        *,
        package: CampaignPackage,  # noqa: ARG002 - doble en memoria, no lo necesita
        envelope: PackageApprovalEnvelope,  # noqa: ARG002
        binding: PackageStepBinding,
        resolutions: dict[str, str],  # noqa: ARG002
        human_authorization_id: str,  # noqa: ARG002
        human_approval_signature: bytes,  # noqa: ARG002
        existing_proposal_id: str | None = None,
    ) -> StepExecutionOutcome:
        self.calls.append(binding)
        self.existing_proposal_ids.append(existing_proposal_id)
        return self._outcomes[binding.step_index]
