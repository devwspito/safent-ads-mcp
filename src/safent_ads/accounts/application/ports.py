"""Puertos de `accounts` (plan.md §5): `AdsPlatformPort` exacto de
`contracts/platform-port.md`, mas `AccountRepository`/`AdEntityRepository`.

Dos implementaciones sustituibles de `AdsPlatformPort` (LSP,
contracts/platform-port.md): `BrokerSocketClient` (api/worker, sin SDKs) y
`GoogleAdsAdapter`/`MetaAdsAdapter` (unico lugar del repo con
`google-ads-python`/`facebook-business`, en `broker/platforms/`)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Literal, Protocol

from safent_ads.accounts.domain.ad_entity import AdEntity, AdEntityStatus
from safent_ads.accounts.domain.budget import Budget
from safent_ads.accounts.domain.date_window import DateWindow
from safent_ads.accounts.domain.json_value import JsonValue
from safent_ads.accounts.domain.learning_state import LearningState
from safent_ads.accounts.domain.money import Money
from safent_ads.accounts.domain.platform_account import PlatformAccount
from safent_ads.accounts.domain.refs import AccountRef, IdempotencyKey
from safent_ads.shared.ids import BusinessId, EntityRef, PlatformCode
from safent_ads.shared.managed_ads import ManagedAdsBinding

__all__ = [
    "AccountRef",
    "AccountRepository",
    "AdEntityRepository",
    "AdEntitySnapshot",
    "AdsPlatformPort",
    "AssetUploadRequest",
    "DateWindow",
    "EntityStateSnapshot",
    "IdempotencyKey",
    "MetricFactSnapshot",
    "MetricGranularity",
    "MetricsRequest",
    "PlatformAssetHandle",
    "SignedAuthorization",
    "WriteIntent",
    "WriteOperation",
    "WriteOutcome",
]


class MetricGranularity(StrEnum):
    DAILY = "daily"
    HOURLY = "hourly"


@dataclass(frozen=True, slots=True, kw_only=True)
class MetricsRequest:
    account_ref: AccountRef
    window: DateWindow
    granularity: MetricGranularity
    entity_refs: Sequence[EntityRef] | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class MetricFactSnapshot:
    """DTO de lectura de plataforma; `metrics.application.IngestDailyMetrics`
    (fuera de este lane) lo traduce a `MetricFact` (data-model.md)."""

    entity_ref: EntityRef
    stat_date: date
    stat_hour: int | None
    currency: str
    spend: Money
    impressions: int
    clicks: int
    reach: int | None
    frequency: float | None
    conversions_by_kind: Mapping[str, int]
    conversion_value: Money | None
    video_views_3s: int | None
    video_views_75pct: int | None
    search_lost_is_budget: float | None
    search_lost_is_rank: float | None


@dataclass(frozen=True, slots=True, kw_only=True)
class AdEntitySnapshot:
    """Estado remoto completo de una `AdEntity`, tal como lo entrega el
    adaptador. `SyncAccountInventory` calcula `PlatformStateHash.compute`
    sobre `canonical_state`; el adaptador no decide la politica de hash."""

    entity_ref: EntityRef
    parent_ref: EntityRef
    name: str
    status: AdEntityStatus
    is_controllable: bool
    learning_state: LearningState
    budget: Budget | None
    bid_target: Money | None
    shared_budget_ref: EntityRef | None
    canonical_state: Mapping[str, JsonValue]
    fetched_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class EntityStateSnapshot:
    """Estado remoto minimo de una entidad, usado por `read_entity_state`
    para la precondicion de deriva (`DetectPlatformDrift`,
    contracts/platform-port.md punto 7 del broker)."""

    entity_ref: EntityRef
    status: AdEntityStatus
    is_controllable: bool
    canonical_state: Mapping[str, JsonValue]
    fetched_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class AssetUploadRequest:
    """H1 (revision de seguridad 0.2.23): `width`/`height`/`package_binding`/
    `package_approval` viajan SOLO para el paso `UPLOAD_CREATIVE` de un
    paquete -- `chokepoint_step_executor.py` los rellena desde el sobre
    firmado (`PackageStepBinding.to_canonical()`/`PackageApprovalProof.
    as_claims()`, mismo criterio que `WriteIntent.package_binding`/
    `SignedAuthorization.package_approval`); el `upload_creative_asset`
    independiente (MCP) los deja en `None`, y el broker entonces sube el
    activo sin tocar ningun libro, exactamente como antes de esta revision."""

    account_ref: AccountRef
    file_name: str
    mime_type: str
    media: bytes
    width: int | None = None
    height: int | None = None
    package_binding: Mapping[str, object] | None = None
    package_approval: Mapping[str, object] | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class PlatformAssetHandle:
    platform_asset_id: str
    preview_url: str | None


class WriteOperation(StrEnum):
    LOWER_BUDGET = "LOWER_BUDGET"
    RAISE_BUDGET = "RAISE_BUDGET"
    PAUSE = "PAUSE"
    RESUME = "RESUME"
    SET_BID_TARGET = "SET_BID_TARGET"
    SET_TARGETING = "SET_TARGETING"
    ADD_NEGATIVE_KEYWORD = "ADD_NEGATIVE_KEYWORD"
    ROTATE_OUT_CREATIVE = "ROTATE_OUT_CREATIVE"
    CREATE_CAMPAIGN = "CREATE_CAMPAIGN"
    CREATE_AD_SET = "CREATE_AD_SET"
    CREATE_AD = "CREATE_AD"
    DELETE = "DELETE"
    # 004 tasks-2.md W3 (historia 20): escritura nativa para lo que aun no
    # tiene operacion propia -- `propose_native_write`, parametro
    # `native:<platform>:<operation>`. Nunca toca presupuesto/puja/estado
    # (mcp/domain/native_write_payload.py lo rechaza en dominio puro antes
    # de llegar aqui).
    NATIVE_WRITE = "NATIVE_WRITE"


@dataclass(frozen=True, slots=True)
class WriteIntent:
    entity_ref: EntityRef
    operation: WriteOperation
    parametro: str
    valor_actual: JsonValue
    valor_propuesto: JsonValue
    diff_hash: str
    expected_state_hash: str
    business_id: str | None = None
    managed_binding: ManagedAdsBinding | None = None
    # `003-paquete-de-campana` contracts/api.md §R2.E (BL-2/BL-3): la forma
    # canonica de `packages.domain.step_binding.PackageStepBinding.
    # to_canonical()` -- `Mapping[str, object]`, nunca el tipo de dominio de
    # `packages`, porque `accounts -> packages` cerraria un ciclo (`packages
    # -> {proposals, execution, accounts, shared}`, "ninguna flecha de
    # vuelta", data-model.md "Bounded contexts"). Obligatorio si y solo si
    # `authorization.kind == "package_step"` (R1, INV-13).
    package_binding: Mapping[str, object] | None = None


@dataclass(frozen=True, slots=True)
class SignedAuthorization:
    authorization_id: str
    proposal_id: str
    # `003-paquete-de-campana` contracts/api.md §R2.E (BL-3): tercer valor,
    # la autorizacion de UN paso de publicacion. El bróker de hoy sigue
    # denegando `package_step` via `OWNER_APPROVAL_REQUIRED`
    # (`require_owner_approval`) -- admitirlo de verdad (R1-R7,
    # `SignedPackageApproval`/`WriteIntent.package_binding`) es T107/T108,
    # fuera de esta entrega; este valor solo evita que `_AUTHORIZATION_KIND`
    # (`execution/infrastructure/broker_platform.py`) reviente con un
    # `KeyError` en cuanto exista una `Authorization` de ese `kind`.
    kind: Literal["human_approval", "rule_authorization", "package_step"]
    diff_hash: str
    guardrail_verdict_hash: str
    # Firmado junto al resto (`proposals.domain.authorization.Authorization.
    # signing_payload()`, `broker.domain.write_authorization.
    # authorization_signing_payload`): identifica quien decidio -- parte del
    # significado de la auditoria, no solo metadato de acompanamiento.
    issued_by: str
    expires_at: datetime
    signature: str
    managed_binding: ManagedAdsBinding | None = None
    # `003-paquete-de-campana` contracts/api.md §R2.E (BL-2/BL-3):
    # `proposals.domain.authorization.PackageApprovalProof.as_claims()` --
    # el sobre humano COMPLETO (`envelope`, `authorization_id`, `issued_by`,
    # `expires_at`, `signature`), integro, para que el broker verifique la
    # firma humana sin preguntarle nada a `ads-api` (R2). Obligatorio si y
    # solo si `kind == "package_step"` (R1, INV-13); `Mapping[str, object]`
    # por la misma razon de aciclicidad que `WriteIntent.package_binding`.
    package_approval: Mapping[str, object] | None = None


@dataclass(frozen=True, slots=True)
class WriteOutcome:
    outcome: Literal[
        "SUCCEEDED", "FAILED", "UNKNOWN", "SKIPPED_DRIFT", "BLOCKED_HARD_CAP", "DENIED"
    ]
    applied_value: JsonValue | None
    state_hash_after: str | None
    error_code: str | None
    platform_request_id: str | None


class AdsPlatformPort(Protocol):
    """contracts/platform-port.md. `execute_write` **siempre** deniega en esta
    fase (F1/US1): el chokepoint de `execution` que autoriza escrituras reales
    llega en F2 (plan.md §10)."""

    async def fetch_account_inventory(
        self, account_ref: AccountRef
    ) -> Sequence[AdEntitySnapshot]: ...

    async def fetch_metrics(self, request: MetricsRequest) -> Sequence[MetricFactSnapshot]: ...

    async def read_entity_state(self, entity_ref: EntityRef) -> EntityStateSnapshot: ...

    async def upload_asset(self, request: AssetUploadRequest) -> PlatformAssetHandle: ...

    async def execute_write(
        self,
        intent: WriteIntent,
        authorization: SignedAuthorization,
        idempotency_key: IdempotencyKey,
    ) -> WriteOutcome: ...

    async def read_write_receipt(
        self,
        intent: WriteIntent,
        authorization: SignedAuthorization,
        idempotency_key: IdempotencyKey,
    ) -> WriteOutcome | None: ...

    async def run_gaql(
        self, account_ref: AccountRef, query: str, *, max_rows: int
    ) -> Sequence[Mapping[str, Any]]:
        """Consulta GAQL de solo lectura ya validada por el llamante
        (`broker/presentation/dispatcher.py` valida ANTES de resolver el
        adaptador). Especifica de Google Ads: las plataformas sin este
        lenguaje de consulta lanzan `PlatformCapabilityNotImplementedError`
        (mismo patron que `upload_asset` en `MetaAdsAdapter`)."""
        ...


class AccountRepository(Protocol):
    """Persistencia de `PlatformAccount` (tabla `platform_accounts`,
    data-model.md). Implementacion SQL pendiente de otro lane (T019/migraciones)."""

    async def get_by_ref(self, account_ref: AccountRef) -> PlatformAccount | None: ...

    async def list_by_business(self, business_id: BusinessId) -> Sequence[PlatformAccount]: ...

    async def save(self, account: PlatformAccount) -> None: ...

    async def exists_for_platform(self, platform: PlatformCode) -> bool:
        """Al menos una `PlatformAccount` de esta plataforma, en toda la
        instalacion (sin `business_id`, 029 T022): igual que las
        credenciales de VENDOR de `platform_apps_router.py`, el estado de
        onboarding es por instalacion, no por negocio."""
        ...


class AdEntityRepository(Protocol):
    """Persistencia de `AdEntity` (tabla `ad_entities`, data-model.md)."""

    async def get_by_ref(self, entity_ref: EntityRef) -> AdEntity | None: ...

    async def list_by_account(self, account_ref: AccountRef) -> Sequence[AdEntity]: ...

    async def save(self, entity: AdEntity) -> None: ...

    async def save_many(self, entities: Sequence[AdEntity]) -> None: ...
