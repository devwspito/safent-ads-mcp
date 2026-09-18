"""`GoogleAdsAdapter` (T025): implementa `AdsPlatformPort` sobre
`google-ads-python` v32 (API v25). Unico punto del broker que habla con
Google Ads (contracts/platform-port.md).

Convencion de `EntityRef.external_id` para Google: el *resource name*
propio de la API (`customers/{customer_id}/campaigns/{campaign_id}`), no un
id desnudo. Es el unico dato que ya trae el `customer_id` sin que este
sistema tenga que mantener un mapeo propio."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Protocol

from safent_ads.accounts.application.ports import (
    AccountRef,
    AdEntitySnapshot,
    AssetUploadRequest,
    EntityStateSnapshot,
    MetricFactSnapshot,
    MetricGranularity,
    MetricsRequest,
    PlatformAssetHandle,
    SignedAuthorization,
    WriteIntent,
    WriteOperation,
    WriteOutcome,
)
from safent_ads.accounts.domain.ad_entity import AdEntityStatus
from safent_ads.accounts.domain.budget import Budget, BudgetKind
from safent_ads.accounts.domain.learning_state import LearningState
from safent_ads.accounts.domain.money import Money
from safent_ads.accounts.domain.platform_state_hash import PlatformStateHash
from safent_ads.accounts.domain.refs import IdempotencyKey
from safent_ads.broker.domain.write_authorization import (
    WriteDenialCode,
    platform_account_id_from_google_resource_name,
)
from safent_ads.broker.platforms.ad_child_creation import create_paused_child
from safent_ads.broker.platforms.campaign_creation import create_paused_campaign
from safent_ads.broker.platforms.error_sanitizer import redact_sdk_error
from safent_ads.broker.platforms.errors import (
    DailyOperationBudgetExhaustedError,
    GaqlValidationError,
    PlatformCapabilityNotImplementedError,
)
from safent_ads.broker.platforms.gaql_validator import validate_gaql
from safent_ads.broker.platforms.google_asset_upload import (
    ALLOWED_IMAGE_MIME_TYPES,
    MAX_IMAGE_UPLOAD_BYTES,
    GoogleAssetUploadClient,
    asset_checksum,
    upload_image_asset,
)
from safent_ads.broker.platforms.google_reference_reader import (
    GoogleKeywordIdeaClient,
    fetch_keyword_ideas,
)
from safent_ads.broker.platforms.google_tag_manager import GoogleTagManagerClient
from safent_ads.broker.platforms.rate_limits import DailyOperationBudget
from safent_ads.broker.platforms.write_pipeline import (
    PackageUploadDeniedError,
    WriteAuthorizationPipeline,
    denial_outcome,
)
from safent_ads.shared.clock import Clock
from safent_ads.shared.errors import InfrastructureError
from safent_ads.shared.ids import EntityLevel, EntityRef, PlatformCode

_DEFAULT_DAILY_OPERATION_BUDGET: Final = 2_880  # research §3: tier Explorer
_DEFAULT_MAX_ROWS_PER_QUERY: Final = 10_000
_MICROS_PER_UNIT: Final = 1_000_000
_MICROS_PER_CENT: Final = 10_000

_ENABLED_STATUS: Final = "ENABLED"
_PAUSED_STATUS: Final = "PAUSED"
_REMOVED_STATUS: Final = "REMOVED"
_GTM_PARAMETER: Final = "native:google:gtm_change"

_RESOURCE_NAME_CUSTOMER_ID_PATTERN: Final = re.compile(r"^customers/(\d+)/")
# GAQL no tiene API de parametros vinculados (bind parameters); todo valor
# que entra en un literal de la consulta se valida contra este patron antes
# de interpolarse, para que una comilla o `;` en un `external_id` no pueda
# escapar del literal (defensa en profundidad, threat-model.md T-1).
_SAFE_RESOURCE_NAME_PATTERN: Final = re.compile(r"^customers/\d+/[a-zA-Z_]+/[\w~./\-]+$")


class GoogleAdsAdapterError(InfrastructureError):
    """Fallo irrecuperable del adaptador Google Ads, ya saneado."""


@dataclass(frozen=True, slots=True)
class GoogleAdsAdapterConfig:
    """Credenciales resueltas por `composition` desde `BrokerSettings`. El
    adaptador nunca lee variables de entorno por su cuenta (mantiene la
    frontera: solo `ads-broker` conoce credenciales de plataforma)."""

    client_id: str
    client_secret: str
    refresh_token: str
    login_customer_id: str
    daily_operation_budget: int = _DEFAULT_DAILY_OPERATION_BUDGET


class GoogleAdsSearchClient(Protocol):
    """Sub-conjunto de `GoogleAdsService` que el adaptador necesita. En
    produccion lo cablea `LiveGoogleAdsSearchClient`; en tests, un doble
    sencillo que no toca la red (T025: "SDK mocked").

    Los cuatro `mutate_*` son el unico punto por el que `execute_write`
    aplica un cambio real -- cada uno mapea 1:1 a una operacion del puerto
    (deliverable 2: "Google mutate for campaign budget/status, ad group
    criterion negatives, ad status") y devuelve el `resource_name` que la
    plataforma confirma haber tocado."""

    def search_stream(self, customer_id: str, query: str) -> Iterator[Mapping[str, Any]]: ...

    def campaign_creation_currency(self, customer_id: str) -> str: ...

    def verify_conversion_goals(self, customer_id: str, resource_names: Sequence[str]) -> None: ...

    def prepare_child(self, parent: str, plan: Mapping[str, Any]) -> None: ...

    def create_paused_child(self, parent: str, plan: Mapping[str, Any]) -> Mapping[str, Any]: ...

    def create_paused_campaign(
        self, customer_id: str, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]: ...

    def mutate_campaign_budget(
        self, customer_id: str, budget_resource_name: str, amount_micros: int
    ) -> str: ...

    def mutate_status(
        self, customer_id: str, resource_name: str, level: EntityLevel, status: str
    ) -> str: ...

    def mutate_negative_keyword(
        self, customer_id: str, ad_group_resource_name: str, keyword_text: str
    ) -> str: ...


_CAMPAIGN_FIELDS: Final = (
    "campaign.resource_name",
    "campaign.id",
    "campaign.name",
    "campaign.status",
    "campaign_budget.resource_name",
    "campaign_budget.amount_micros",
    "campaign_budget.type",
    "campaign_budget.explicitly_shared",
    "customer.currency_code",
)
_AD_GROUP_FIELDS: Final = (
    "ad_group.resource_name",
    "ad_group.id",
    "ad_group.name",
    "ad_group.status",
    "ad_group.campaign",
    "customer.currency_code",
)
_AD_FIELDS: Final = (
    "ad_group_ad.resource_name",
    "ad_group_ad.status",
    "ad_group_ad.ad.id",
    "ad_group_ad.ad_group",
    "customer.currency_code",
)
_STATUS_FIELD_BY_RESOURCE: Final = {
    "campaign": "campaign.status",
    "ad_group": "ad_group.status",
    "ad_group_ad": "ad_group_ad.status",
}
_METRIC_FIELDS: Final = (
    "campaign.resource_name",
    "segments.date",
    "metrics.cost_micros",
    "metrics.impressions",
    "metrics.clicks",
    "metrics.conversions",
    "metrics.conversions_value",
    "metrics.search_budget_lost_impression_share",
    "metrics.search_rank_lost_impression_share",
    "customer.currency_code",
)
# T162 (`_build_metrics_query`): insercion de `segments.hour` cuando la
# granularidad es horaria, ahora contra el TEXTO ya renderizado de la
# plantilla en vez de la lista `_METRIC_FIELDS` -- el ancla es unica en la
# consulta (no hay otro `segments.date,` fuera de la clausula SELECT).
_HOURLY_GRANULARITY_ANCHOR: Final = "segments.date,"
_HOURLY_GRANULARITY_REPLACEMENT: Final = "segments.date, segments.hour,"


@dataclass(frozen=True, slots=True)
class GoogleAdsQueryTemplates:
    """Las cinco consultas SELECT versionadas de `broker/platforms/gaql/*.gaql`
    (T162, `composition.gaql_templates.load_gaql_template`), inyectadas por
    `composition/broker.py`: el adaptador ya no construye estas consultas
    desde constantes Python en tiempo de ejecucion, solo les concatena el
    `WHERE`/`LIMIT` que depende de la peticion (mismo texto base, siempre)."""

    campaign_inventory: str
    ad_group_inventory: str
    ad_inventory: str
    campaign_metrics: str
    campaign_budget_lookup: str

    @classmethod
    def from_inline_constants(cls) -> GoogleAdsQueryTemplates:
        """Solo para tests/llamadores que no cablean `load_gaql_template`:
        reproduce, letra a letra, lo que hoy producen `_build_select`/las
        constantes de campos (`tests/unit/composition/test_gaql_templates.py`
        ya prueba que cada fichero `.gaql` coincide con este mismo texto).
        `composition/broker.py` construye la version real desde disco."""
        return cls(
            campaign_inventory=_build_select(_CAMPAIGN_FIELDS, "campaign"),
            ad_group_inventory=_build_select(_AD_GROUP_FIELDS, "ad_group"),
            ad_inventory=_build_select(_AD_FIELDS, "ad_group_ad"),
            campaign_metrics=f"SELECT {', '.join(_METRIC_FIELDS)} FROM campaign",  # noqa: S608
            campaign_budget_lookup="SELECT campaign_budget.resource_name FROM campaign",
        )


class GoogleAdsAdapter:
    def __init__(
        self,
        config: GoogleAdsAdapterConfig,
        search_client: GoogleAdsSearchClient,
        clock: Clock,
        *,
        max_rows_per_query: int = _DEFAULT_MAX_ROWS_PER_QUERY,
        write_pipeline: WriteAuthorizationPipeline | None = None,
        templates: GoogleAdsQueryTemplates | None = None,
        keyword_idea_client: GoogleKeywordIdeaClient | None = None,
        asset_upload_client: GoogleAssetUploadClient | None = None,
        tag_manager_client: GoogleTagManagerClient | None = None,
    ) -> None:
        self._config = config
        self._search_client = search_client
        self._clock = clock
        self._max_rows_per_query = max_rows_per_query
        self._daily_budget = DailyOperationBudget(config.daily_operation_budget, clock)
        # `None` hasta que `composition` (otra rama) cablee `ApprovalVerifier`
        # + `CapsConfig` + `WriteLedgerStore`: sin pipeline, `execute_write`
        # deniega por diseno (`WRITE_PATH_NOT_WIRED`), nunca muta a ciegas.
        self._write_pipeline = write_pipeline
        # `None` solo en llamadores que no cablean `composition/broker.py`
        # (tests unitarios de este modulo): cae en las mismas cinco
        # consultas que este adaptador construia antes de T162.
        self._templates = templates or GoogleAdsQueryTemplates.from_inline_constants()
        # `composition/broker.py::_build_google_adapter` cablea siempre un
        # `LiveGoogleKeywordIdeaClient` real sobre `KeywordPlanIdeaService`
        # (004 tasks-2.md R4); `None` solo en llamadores que no pasan por
        # esa fabrica (tests unitarios de este modulo) -- `read_reference_
        # data` falla cerrado en ese caso, nunca inventa ideas de palabras
        # clave.
        self._keyword_idea_client = keyword_idea_client
        # `None` hasta que `composition` cablee un wrapper real de
        # `AssetService` (mismo criterio que `keyword_idea_client`):
        # `upload_asset` falla cerrado con `PlatformCapabilityNotImplementedError`
        # en ese caso, nunca intenta mutar sin cliente.
        self._asset_upload_client = asset_upload_client
        self._tag_manager_client = tag_manager_client

    async def fetch_account_inventory(self, account_ref: AccountRef) -> Sequence[AdEntitySnapshot]:
        customer_id = account_ref.external_account_id
        campaign_query = self._templates.campaign_inventory
        ad_group_query = self._templates.ad_group_inventory
        campaign_rows = await self._run_gaql(customer_id, campaign_query)
        ad_group_rows = await self._run_gaql(customer_id, ad_group_query)
        ad_rows = await self._run_gaql(customer_id, self._templates.ad_inventory)
        fetched_at = self._clock.now()
        return [
            *(_campaign_row_to_snapshot(row, fetched_at) for row in campaign_rows),
            *(_ad_group_row_to_snapshot(row, fetched_at) for row in ad_group_rows),
            *(_ad_row_to_snapshot(row, fetched_at) for row in ad_rows),
        ]

    async def fetch_metrics(self, request: MetricsRequest) -> Sequence[MetricFactSnapshot]:
        customer_id = request.account_ref.external_account_id
        query = _build_metrics_query(self._templates.campaign_metrics, request)
        rows = await self._run_gaql(customer_id, query)
        return [_metric_row_to_snapshot(row) for row in rows]

    async def read_entity_state(self, entity_ref: EntityRef) -> EntityStateSnapshot:
        resource_name = _validate_resource_name(entity_ref.external_id)
        customer_id = _customer_id_from_resource_name(resource_name)
        _, resource = _fields_for_level(entity_ref.level)
        template = _template_for_level(self._templates, entity_ref.level)
        query = f"{template} AND {resource}.resource_name = '{resource_name}'"
        rows = await self._run_gaql(customer_id, query)
        if not rows:
            raise GoogleAdsAdapterError(f"entidad no encontrada: {entity_ref.level}")
        return _row_to_state_snapshot(entity_ref, rows[0], self._clock.now())

    async def upload_asset(self, request: AssetUploadRequest) -> PlatformAssetHandle:
        """Deliverable 2 / threat-model.md #17: `AssetService.mutate_assets`
        con un `ImageAsset`. Tipo y tamano comprobados ANTES de tocar el SDK
        (mismo criterio que `MetaAdsAdapter.upload_asset`) -- un payload que
        ya sabemos que Google va a rechazar nunca consume presupuesto de
        escritura. Un `package_binding`/`package_approval` (paso
        `UPLOAD_CREATIVE` firmado) va por `WriteAuthorizationPipeline.
        admit_upload`, la MISMA ruta que usa Meta (H1, 0.2.23): idempotencia
        por el libro. Una subida SUELTA (`upload_creative_asset` de MCP, sin
        paquete) nunca toca el libro -- su idempotencia por contenido es una
        consulta GAQL sobre `asset.name` (Google, a diferencia de Meta, no
        deduplica imagenes por contenido de forma gratuita)."""
        if self._asset_upload_client is None:
            raise PlatformCapabilityNotImplementedError(
                "upload_asset sin cliente de AssetService configurado"
            )
        if request.mime_type not in ALLOWED_IMAGE_MIME_TYPES:
            raise GoogleAdsAdapterError(f"tipo de imagen no admitido: {request.mime_type!r}")
        if len(request.media) > MAX_IMAGE_UPLOAD_BYTES:
            raise GoogleAdsAdapterError("la imagen supera el limite de subida de Google Ads")
        if request.width is None or request.height is None:
            raise GoogleAdsAdapterError("upload_asset de Google requiere width y height")
        customer_id = request.account_ref.external_account_id
        checksum = asset_checksum(request.media)

        async def _create_asset() -> PlatformAssetHandle:
            assert self._asset_upload_client is not None  # noqa: S101 - checked above
            return await upload_image_asset(
                self._asset_upload_client,
                request,
                customer_id=customer_id,
                checksum=checksum,
                find_existing=lambda value: self._find_asset_by_checksum(customer_id, value),
            )

        if request.package_binding is None and request.package_approval is None:
            return await _create_asset()
        if self._write_pipeline is None:
            raise PackageUploadDeniedError(WriteDenialCode.WRITE_PATH_NOT_WIRED.value)
        return await self._write_pipeline.admit_upload(
            account_ref=request.account_ref,
            media=request.media,
            mime_type=request.mime_type,
            width=request.width,
            height=request.height,
            binding=request.package_binding,
            approval=request.package_approval,
            upload=_create_asset,
            now=self._clock.now(),
        )

    async def _find_asset_by_checksum(self, customer_id: str, checksum: str) -> str | None:
        # `checksum` es siempre `hashlib.sha256(media).hexdigest()`
        # (`asset_checksum`), nunca un valor externo -- seguro para
        # interpolar en el literal GAQL sin pasar por `_safe_literal`
        # (mismo razonamiento que `_build_select` sobre sus constantes).
        query = f"SELECT asset.resource_name FROM asset WHERE asset.name = '{checksum}'"  # noqa: S608
        rows = await self._run_gaql(customer_id, query)
        if not rows:
            return None
        return str(rows[0]["asset.resource_name"])

    async def execute_write(  # noqa: PLR0911 - explicit fail-closed operation dispatch
        self,
        intent: WriteIntent,
        authorization: SignedAuthorization,
        idempotency_key: IdempotencyKey,
    ) -> WriteOutcome:
        """Los 8 controles de contracts/platform-port.md, en orden: peercred
        y esquema ya pasaron (presentation/); aqui van recompute+firma+topes
        (`WriteAuthorizationPipeline.authorize`), soporte de la operacion,
        idempotencia (justo antes de mutar), mutacion via SDK y lectura de
        confirmacion. Nunca lanza por una denegacion -- siempre un
        `WriteOutcome` tipado."""
        pipeline = self._write_pipeline
        if pipeline is None:
            return denial_outcome(WriteDenialCode.WRITE_PATH_NOT_WIRED)
        if intent.operation == WriteOperation.CREATE_CAMPAIGN:
            return await create_paused_campaign(
                pipeline=pipeline,
                intent=intent,
                authorization=authorization,
                idempotency_key=idempotency_key,
                now=self._clock.now(),
                currency=self._search_client.campaign_creation_currency,
                create=self._search_client.create_paused_campaign,
                consume_rate=self._daily_budget.try_consume,
                # T032 verification is a no-op for a plan without signed
                # `conversion_goals` (campaign_creation.py checks
                # `resource_names` before ever calling this) -- `getattr`
                # keeps that no-op true even when `search_client` (a legacy
                # Search-only double, never touched by 005) has no such
                # method, instead of an `AttributeError` turning a valid
                # Search creation into a spurious `UNKNOWN`.
                verify_conversion_goals=getattr(
                    self._search_client, "verify_conversion_goals", None
                ),
            )
        resource_name = _validate_resource_name(intent.entity_ref.external_id)
        customer_id = platform_account_id_from_google_resource_name(resource_name)
        assert customer_id is not None  # noqa: S101 - garantizado por _validate_resource_name
        now = self._clock.now()
        gate = await self._authorize_write(pipeline, intent, authorization, customer_id, now)
        if gate is not None:
            return gate
        key = str(idempotency_key)
        if intent.operation in (WriteOperation.CREATE_AD_SET, WriteOperation.CREATE_AD):
            return await create_paused_child(
                pipeline=pipeline,
                intent=intent,
                authorization=authorization,
                key=key,
                account=customer_id,
                now=now,
                prepare=self._search_client.prepare_child,
                create=self._search_client.create_paused_child,
                consume_rate=self._daily_budget.try_consume,
            )
        if intent.operation is WriteOperation.NATIVE_WRITE and (
            intent.parametro != _GTM_PARAMETER or self._tag_manager_client is None
        ):
            return denial_outcome(WriteDenialCode.OPERATION_NOT_SUPPORTED)
        if intent.operation not in _SUPPORTED_OPERATIONS:
            return denial_outcome(WriteDenialCode.OPERATION_NOT_SUPPORTED)
        replay = await pipeline.begin_admitted_write(key, intent, authorization, customer_id, now)
        if replay is not None:
            return replay
        outcome = await self._apply_write(intent, customer_id, resource_name)
        return pipeline.finalize(key, customer_id, intent, outcome, now=now)

    async def read_write_receipt(
        self,
        intent: WriteIntent,
        authorization: SignedAuthorization,
        idempotency_key: IdempotencyKey,
    ) -> WriteOutcome | None:
        if self._write_pipeline is None:
            return None
        return self._write_pipeline.read_receipt(str(idempotency_key), intent, authorization)

    async def _authorize_write(
        self,
        pipeline: WriteAuthorizationPipeline,
        intent: WriteIntent,
        authorization: SignedAuthorization,
        customer_id: str,
        now: datetime,
    ) -> WriteOutcome | None:
        try:
            remote = await self.read_entity_state(intent.entity_ref)
        except GoogleAdsAdapterError as exc:
            return WriteOutcome("FAILED", None, None, str(exc), None)
        remote_hash = PlatformStateHash.compute(remote.canonical_state).value
        return pipeline.authorize(
            intent,
            authorization,
            platform_account_id=customer_id,
            remote_state_hash=remote_hash,
            now=now,
        )

    async def _apply_write(
        self, intent: WriteIntent, customer_id: str, resource_name: str
    ) -> WriteOutcome:
        # A single pause/resume/budget/negative-keyword mutate is always
        # exactly one Google Ads operation (T035 finding 2, threat-model.md
        # D-2/AL-5) -- explicit, never the implicit default of `try_consume`.
        if not self._daily_budget.try_consume(1):
            return WriteOutcome("FAILED", None, None, "rate_limited", None)
        try:
            platform_request_id = await self._mutate(
                intent.operation, customer_id, resource_name, intent
            )
            confirmed = await self.read_entity_state(intent.entity_ref)
        except Exception:  # noqa: BLE001 - el proveedor puede haber aplicado la mutacion
            # Desde que existe un recibo IN_FLIGHT no podemos distinguir entre
            # "el SDK fallo antes de enviar" y "Google aplico el cambio pero se
            # perdio la respuesta/lectura de confirmacion". Marcarlo FAILED
            # permitiria que un consumidor reintentase una escritura incierta.
            # UNKNOWN queda duradero en el ledger y exige reconciliacion.
            return WriteOutcome("UNKNOWN", None, None, "provider_outcome_unknown", None)
        confirmed_hash = PlatformStateHash.compute(confirmed.canonical_state).value
        return WriteOutcome(
            "SUCCEEDED", intent.valor_propuesto, confirmed_hash, None, platform_request_id
        )

    async def _mutate(
        self, operation: WriteOperation, customer_id: str, resource_name: str, intent: WriteIntent
    ) -> str:
        if operation is WriteOperation.NATIVE_WRITE:
            if intent.parametro != _GTM_PARAMETER or self._tag_manager_client is None:
                raise PlatformCapabilityNotImplementedError("escritura nativa Google no soportada")
            if not isinstance(intent.valor_propuesto, Mapping):
                raise GoogleAdsAdapterError("cambio GTM invalido")
            result = await self._tag_manager_client.apply_change(
                customer_id, intent.valor_propuesto
            )
            request_id = result.get("path") or result.get("containerVersionId")
            return str(request_id or "gtm_change_applied")
        if operation in _BUDGET_OPERATIONS:
            return await self._mutate_budget(customer_id, resource_name, intent)
        status = _STATUS_BY_OPERATION.get(operation)
        if status is not None:
            return await asyncio.to_thread(
                self._search_client.mutate_status,
                customer_id,
                resource_name,
                intent.entity_ref.level,
                status,
            )
        keyword_text = _negative_keyword_text(intent.valor_propuesto)
        return await asyncio.to_thread(
            self._search_client.mutate_negative_keyword, customer_id, resource_name, keyword_text
        )

    async def _mutate_budget(
        self, customer_id: str, campaign_resource_name: str, intent: WriteIntent
    ) -> str:
        if intent.parametro != "daily_budget" or intent.entity_ref.level != EntityLevel.CAMPAIGN:
            raise GoogleAdsAdapterError("daily_budget requiere una campana Google")
        budget_resource_name = await self._campaign_budget_resource_name(
            customer_id, campaign_resource_name
        )
        amount_micros = _money_amount_micros(intent.valor_propuesto)
        return await asyncio.to_thread(
            self._search_client.mutate_campaign_budget,
            customer_id,
            budget_resource_name,
            amount_micros,
        )

    async def _campaign_budget_resource_name(
        self, customer_id: str, campaign_resource_name: str
    ) -> str:
        # `campaign_resource_name` ya paso por `_validate_resource_name`
        # (execute_write, unico llamante): el mismo patron que
        # `_build_metrics_query` para interpolar un resource_name saneado.
        query = (
            f"{self._templates.campaign_budget_lookup} "
            f"WHERE campaign.resource_name = '{campaign_resource_name}'"
        )
        rows = await self._run_gaql(customer_id, query)
        if not rows:
            raise GoogleAdsAdapterError("campana sin presupuesto propio que mutar")
        return str(rows[0]["campaign_budget.resource_name"])

    async def run_gaql(
        self, account_ref: AccountRef, query: str, *, max_rows: int
    ) -> Sequence[Mapping[str, Any]]:
        """Op `run_gaql` del broker (`broker/presentation/dispatcher.py`):
        consulta arbitraria ya validada por el llamante antes de llegar
        aqui; `_run_gaql` la revalida (defensa en profundidad, igual que
        las consultas internas de este adaptador) y resuelve la credencial
        de CLIENTE de `customer_id` desde `CredentialStorePort`
        (`LiveGoogleAdsSearchClient._resolve_credential`) -- ningun
        `customer_id` recibe la credencial de otro. `max_rows` nunca supera
        el tope de instancia, sin importar lo que pida el llamante."""
        effective_max_rows = min(max_rows, self._max_rows_per_query)
        return await self._run_gaql(
            account_ref.external_account_id, query, max_rows=effective_max_rows
        )

    async def read_reference_data(
        self, account_ref: AccountRef, *, tool: str, arguments: Mapping[str, Any]
    ) -> Sequence[Mapping[str, Any]]:
        """004 tasks-2.md R4/I1 (op `google_reference_read`): unica lectura
        que no es GAQL (`KeywordPlanIdeaService`, servicio de generacion,
        no de consulta). `list_google_conversion_actions`/
        `search_google_constants` van por `run_gaql`, ya cableado -- no
        llegan aqui."""
        if tool != "get_google_keyword_ideas" or self._keyword_idea_client is None:
            raise PlatformCapabilityNotImplementedError(f"{tool} no soportado en Google Ads")
        return await fetch_keyword_ideas(
            self._keyword_idea_client,
            account_ref.external_account_id,
            seed_keywords=tuple(str(keyword) for keyword in arguments["seed_keywords"]),
            geo_target=str(arguments["geo_target"]),
            language=str(arguments["language"]),
        )

    async def read_google_tag_manager(
        self,
        account_ref: AccountRef,
        *,
        resource: str,
        parent_path: str | None,
    ) -> Mapping[str, Any]:
        if self._tag_manager_client is None:
            raise PlatformCapabilityNotImplementedError(
                "Google Tag Manager no esta cableado en este despliegue"
            )
        return await self._tag_manager_client.read(
            account_ref.external_account_id,
            resource=resource,
            parent_path=parent_path,
        )

    async def _run_gaql(
        self, customer_id: str, query: str, *, max_rows: int | None = None
    ) -> Sequence[Mapping[str, Any]]:
        validate_gaql(query)
        # One GAQL search page is one operation against the daily budget
        # (T035 finding 2) -- explicit, never the implicit default.
        if not self._daily_budget.try_consume(1):
            raise DailyOperationBudgetExhaustedError(
                f"tope diario de operaciones agotado para {customer_id}"
            )
        try:
            return await asyncio.to_thread(
                self._collect_rows, customer_id, query, max_rows or self._max_rows_per_query
            )
        except GaqlValidationError:
            raise
        except Exception as exc:  # noqa: BLE001 - frontera con el SDK, nunca fail-open
            raise GoogleAdsAdapterError(redact_sdk_error(exc)) from None

    def _collect_rows(self, customer_id: str, query: str, max_rows: int) -> list[Mapping[str, Any]]:
        rows: list[Mapping[str, Any]] = []
        for row in self._search_client.search_stream(customer_id, query):
            rows.append(row)
            if len(rows) >= max_rows:
                break
        return rows


def _build_select(fields: Sequence[str], resource: str) -> str:
    # GAQL no expone parametros vinculados: `resource` viene de una lista
    # fija de constantes del propio modulo, nunca de un valor externo.
    return f"SELECT {', '.join(fields)} FROM {resource} WHERE {resource}.status != 'REMOVED'"  # noqa: S608


def _build_metrics_query(template: str, request: MetricsRequest) -> str:
    select_clause = _apply_hourly_granularity(template, request.granularity)
    start = request.window.start.isoformat()
    end = request.window.end.isoformat()
    where_clauses = [f"segments.date BETWEEN '{start}' AND '{end}'"]
    if request.entity_refs:
        safe_names = (_validate_resource_name(ref.external_id) for ref in request.entity_refs)
        resource_names = ", ".join(f"'{name}'" for name in safe_names)
        where_clauses.append(f"campaign.resource_name IN ({resource_names})")
    return f"{select_clause} WHERE " + " AND ".join(where_clauses)


def _apply_hourly_granularity(template: str, granularity: MetricGranularity) -> str:
    if granularity != MetricGranularity.HOURLY:
        return template
    return template.replace(_HOURLY_GRANULARITY_ANCHOR, _HOURLY_GRANULARITY_REPLACEMENT, 1)


def _validate_resource_name(resource_name: str) -> str:
    """Todo `external_id` que entra en un literal GAQL pasa por aqui
    primero: si no respeta el formato de resource name de Google Ads, se
    rechaza en vez de interpolarse (GAQL no tiene bind parameters)."""
    if not _SAFE_RESOURCE_NAME_PATTERN.match(resource_name):
        raise GoogleAdsAdapterError("resource_name con formato inesperado")
    return resource_name


def _fields_for_level(level: EntityLevel) -> tuple[Sequence[str], str]:
    if level == EntityLevel.AD_SET:
        return _AD_GROUP_FIELDS, "ad_group"
    if level == EntityLevel.AD:
        return _AD_FIELDS, "ad_group_ad"
    return _CAMPAIGN_FIELDS, "campaign"


def _template_for_level(templates: GoogleAdsQueryTemplates, level: EntityLevel) -> str:
    if level == EntityLevel.AD_SET:
        return templates.ad_group_inventory
    if level == EntityLevel.AD:
        return templates.ad_inventory
    return templates.campaign_inventory


def _customer_id_from_resource_name(resource_name: str) -> str:
    match = _RESOURCE_NAME_CUSTOMER_ID_PATTERN.match(resource_name)
    if match is None:
        raise GoogleAdsAdapterError("resource_name de Google Ads con formato invalido")
    return match.group(1)


def _campaign_row_to_snapshot(row: Mapping[str, Any], fetched_at: datetime) -> AdEntitySnapshot:
    resource_name = str(row["campaign.resource_name"])
    customer_id = _customer_id_from_resource_name(resource_name)
    canonical_state = _canonical_state(row)
    return AdEntitySnapshot(
        entity_ref=EntityRef(PlatformCode.GOOGLE, EntityLevel.CAMPAIGN, resource_name),
        parent_ref=EntityRef(PlatformCode.GOOGLE, EntityLevel.ACCOUNT, customer_id),
        name=str(row["campaign.name"]),
        status=_map_generic_status(str(row["campaign.status"])),
        is_controllable=True,
        learning_state=LearningState.NOT_APPLICABLE,
        budget=_map_budget(row),
        bid_target=None,
        shared_budget_ref=None,
        canonical_state=canonical_state,
        fetched_at=fetched_at,
    )


def _ad_group_row_to_snapshot(row: Mapping[str, Any], fetched_at: datetime) -> AdEntitySnapshot:
    resource_name = str(row["ad_group.resource_name"])
    campaign_resource_name = str(row["ad_group.campaign"])
    canonical_state = _canonical_state(row)
    return AdEntitySnapshot(
        entity_ref=EntityRef(PlatformCode.GOOGLE, EntityLevel.AD_SET, resource_name),
        parent_ref=EntityRef(PlatformCode.GOOGLE, EntityLevel.CAMPAIGN, campaign_resource_name),
        name=str(row["ad_group.name"]),
        status=_map_generic_status(str(row["ad_group.status"])),
        is_controllable=True,
        learning_state=LearningState.NOT_APPLICABLE,
        budget=None,
        bid_target=None,
        shared_budget_ref=None,
        canonical_state=canonical_state,
        fetched_at=fetched_at,
    )


def _ad_row_to_snapshot(row: Mapping[str, Any], fetched_at: datetime) -> AdEntitySnapshot:
    return AdEntitySnapshot(
        entity_ref=EntityRef(
            PlatformCode.GOOGLE, EntityLevel.AD, str(row["ad_group_ad.resource_name"])
        ),
        parent_ref=EntityRef(
            PlatformCode.GOOGLE, EntityLevel.AD_SET, str(row["ad_group_ad.ad_group"])
        ),
        name=str(row["ad_group_ad.ad.id"]),
        status=_map_generic_status(str(row["ad_group_ad.status"])),
        is_controllable=True,
        learning_state=LearningState.NOT_APPLICABLE,
        budget=None,
        bid_target=None,
        shared_budget_ref=None,
        canonical_state=_canonical_state(row),
        fetched_at=fetched_at,
    )


def _row_to_state_snapshot(
    entity_ref: EntityRef, row: Mapping[str, Any], fetched_at: datetime
) -> EntityStateSnapshot:
    _, resource = _fields_for_level(entity_ref.level)
    status_field = _STATUS_FIELD_BY_RESOURCE[resource]
    canonical_state = _canonical_state(row)
    return EntityStateSnapshot(
        entity_ref=entity_ref,
        status=_map_generic_status(str(row[status_field])),
        is_controllable=True,
        canonical_state=canonical_state,
        fetched_at=fetched_at,
    )


def _canonical_state(row: Mapping[str, Any]) -> dict[str, Any]:
    # Inventory and pre-write revalidation must hash the identical representation.
    return {key: value for key, value in row.items() if not key.endswith(".resource_name")}


def _metric_row_to_snapshot(row: Mapping[str, Any]) -> MetricFactSnapshot:
    currency = str(row["customer.currency_code"])
    stat_date_raw = row["segments.date"]
    stat_date = (
        stat_date_raw if isinstance(stat_date_raw, date) else date.fromisoformat(str(stat_date_raw))
    )
    campaign_resource_name = str(row["campaign.resource_name"])
    return MetricFactSnapshot(
        entity_ref=EntityRef(PlatformCode.GOOGLE, EntityLevel.CAMPAIGN, campaign_resource_name),
        stat_date=stat_date,
        stat_hour=_optional_int(row.get("segments.hour")),
        currency=currency,
        spend=Money(int(row["metrics.cost_micros"]) // _MICROS_PER_CENT, currency),
        impressions=int(row["metrics.impressions"]),
        clicks=int(row["metrics.clicks"]),
        reach=None,
        frequency=None,
        conversions_by_kind={"all": int(float(row["metrics.conversions"]))},
        conversion_value=Money(int(Decimal(str(row["metrics.conversions_value"])) * 100), currency),
        video_views_3s=None,
        video_views_75pct=None,
        search_lost_is_budget=_optional_float(
            row.get("metrics.search_budget_lost_impression_share")
        ),
        search_lost_is_rank=_optional_float(row.get("metrics.search_rank_lost_impression_share")),
    )


def _map_budget(row: Mapping[str, Any]) -> Budget | None:
    amount_micros = row.get("campaign_budget.amount_micros")
    if amount_micros is None:
        return None
    currency = str(row["customer.currency_code"])
    return Budget(Money(int(amount_micros) // _MICROS_PER_CENT, currency), _map_budget_kind(row))


def _map_budget_kind(row: Mapping[str, Any]) -> BudgetKind:
    explicitly_shared = row.get("campaign_budget.explicitly_shared")
    if not isinstance(explicitly_shared, bool):
        raise GoogleAdsAdapterError(
            "Google Ads no devolvio el indicador explicitly_shared del presupuesto"
        )
    if explicitly_shared:
        return BudgetKind.SHARED
    return BudgetKind.DAILY


def _map_generic_status(raw_status: str) -> AdEntityStatus:
    normalized = raw_status.upper()
    if normalized == "ENABLED":
        return AdEntityStatus.ACTIVE
    if normalized == "PAUSED":
        return AdEntityStatus.PAUSED
    if normalized == "REMOVED":
        return AdEntityStatus.REMOVED
    raise GoogleAdsAdapterError("Google Ads devolvio un estado de entidad desconocido")


def _optional_int(value: Any) -> int | None:  # noqa: ANN401 - valor crudo del SDK
    return None if value is None else int(value)


def _optional_float(value: Any) -> float | None:  # noqa: ANN401 - valor crudo del SDK
    return None if value is None else float(value)


_BUDGET_OPERATIONS: Final = frozenset({WriteOperation.LOWER_BUDGET, WriteOperation.RAISE_BUDGET})
_STATUS_BY_OPERATION: Final[dict[WriteOperation, str]] = {
    WriteOperation.PAUSE: _PAUSED_STATUS,
    WriteOperation.RESUME: _ENABLED_STATUS,
    # "Rotar fuera" una creatividad es siempre pausarla -- nunca reanuda
    # (deliverable 2: "ad status" para las dos plataformas).
    WriteOperation.ROTATE_OUT_CREATIVE: _PAUSED_STATUS,
    # Borrado irreversible (design.md §0.7): Google
    # Ads no tiene una mutacion `remove` propia -- se borra poniendo
    # `campaign.status = REMOVED` (mismo `mutate_status` que PAUSE/RESUME).
    WriteOperation.DELETE: _REMOVED_STATUS,
}
_SUPPORTED_OPERATIONS: Final = (
    _BUDGET_OPERATIONS
    | frozenset(_STATUS_BY_OPERATION)
    | {
        WriteOperation.ADD_NEGATIVE_KEYWORD,
        WriteOperation.NATIVE_WRITE,
    }
)


def _money_amount_micros(value: Any) -> int:  # noqa: ANN401 - JsonValue del WriteIntent
    """`valor_propuesto` de una operacion de presupuesto llega como
    `proposals.domain.money.Money.to_canonical()`:
    `{"amount": "70.00", "currency": "..."}` -- importe en unidades enteras
    de la divisa, no en centimos. Google Ads factura en micros."""
    if not isinstance(value, Mapping) or "amount" not in value:
        raise GoogleAdsAdapterError("valor_propuesto sin importe valido para mutar presupuesto")
    try:
        return int((Decimal(str(value["amount"])) * _MICROS_PER_UNIT).to_integral_value())
    except InvalidOperation as exc:
        raise GoogleAdsAdapterError("importe no numerico") from exc


def _negative_keyword_text(value: Any) -> str:  # noqa: ANN401 - JsonValue del WriteIntent
    if isinstance(value, str) and value:
        return value
    if isinstance(value, Sequence) and value and isinstance(value[0], str):
        return value[0]
    raise GoogleAdsAdapterError("valor_propuesto sin texto de palabra clave negativa")
