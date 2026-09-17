"""`MetaAdsAdapter` (T026): implementa `AdsPlatformPort` sobre
`facebook-business` v26 (Marketing API v26). Unico punto del broker que
habla con Meta (contracts/platform-port.md).

Convencion de `EntityRef.external_id`: `act_<cuenta>/<nodo>` para entidades,
`act_<cuenta>` para cuentas. El contexto viaja tambien en el diff firmado.
"""

from __future__ import annotations

import asyncio
import random
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from ipaddress import ip_address
from typing import Any, Final, Protocol, TypeVar
from urllib.parse import urlsplit

from safent_ads.accounts.application.errors import EntityNotFoundError
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
from safent_ads.accounts.domain.date_window import DateWindow
from safent_ads.accounts.domain.learning_state import LearningState
from safent_ads.accounts.domain.money import Money
from safent_ads.accounts.domain.platform_state_hash import PlatformStateHash
from safent_ads.accounts.domain.refs import IdempotencyKey
from safent_ads.broker.domain.write_authorization import WriteDenialCode
from safent_ads.broker.platforms.ad_child_creation import create_paused_child
from safent_ads.broker.platforms.campaign_creation import create_paused_campaign
from safent_ads.broker.platforms.error_sanitizer import redact_sdk_error
from safent_ads.broker.platforms.errors import PlatformCapabilityNotImplementedError
from safent_ads.broker.platforms.meta_ad_library import MetaAdLibraryClient, search_meta_ads_archive
from safent_ads.broker.platforms.meta_native_write import apply_native_write
from safent_ads.broker.platforms.meta_scope import scoped_meta_id, split_meta_scope
from safent_ads.broker.platforms.rate_limits import WriteBudgetWindow
from safent_ads.broker.platforms.write_pipeline import (
    PackageUploadDeniedError,
    WriteAuthorizationPipeline,
    denial_outcome,
)
from safent_ads.shared.clock import Clock
from safent_ads.shared.errors import InfrastructureError
from safent_ads.shared.ids import EntityLevel, EntityRef, PlatformCode

_MINOR_UNITS_PER_MAJOR: Final = 100
_META_ACCOUNT_PREFIX: Final = "act_"
_ACTIVE_STATUS: Final = "ACTIVE"
_PAUSED_STATUS: Final = "PAUSED"
_DELETED_STATUS: Final = "DELETED"

_DEFAULT_WRITE_BUDGET_MAX_CALLS: Final = 20  # research §3: tier Limited ~20/5min
_DEFAULT_WRITE_BUDGET_WINDOW: Final = timedelta(minutes=5)

# Deliverable 2 (BL-6): tipos y tamaño que Meta `adimages` acepta hoy para
# creatividades de anuncio. 8 MiB es el limite documentado del endpoint;
# `MIME` restringido a lo que `packages.domain.values.ImageCreativeRef` ya
# valida en el paquete (nunca se amplia aqui la superficie aceptada).
_ALLOWED_CREATIVE_MIME_TYPES: Final = frozenset({"image/jpeg", "image/png"})
_MAX_CREATIVE_UPLOAD_BYTES: Final = 8 * 1024 * 1024
# B4 (revision de seguridad): el `url` que Meta devuelve para la imagen
# recien subida esta alojado por Meta, nunca por nuestra infraestructura
# (BL-6 se cierra por eliminacion de esa superficie) -- se valida de todos
# modos: HTTPS, host propio de Meta, nunca una IP literal.
_ALLOWED_PREVIEW_URL_HOSTS: Final = frozenset({"graph.facebook.com"})
_ALLOWED_PREVIEW_URL_HOST_SUFFIXES: Final = (".fbcdn.net",)

# codigos de error de Meta reintentables con backoff (research §3, Meta
# rate-limiting docs): 17 = User request limit, 32 = Page request limit,
# 613 = Calls to this api have exceeded the rate limit.
_RETRYABLE_ERROR_CODES: Final = frozenset({17, 32, 613})
_BACKOFF_BASE_SECONDS: Final = 1.0
_BACKOFF_CAP_SECONDS: Final = 60.0

_ResultT = TypeVar("_ResultT")


class MetaAdsAdapterError(InfrastructureError):
    """Fallo irrecuperable del adaptador Meta Ads, ya saneado."""


@dataclass(frozen=True, slots=True)
class _AuthorizationResolution:
    """`platform_account_id` viaja junto al veredicto en vez de en un
    atributo de instancia: dos `execute_write` concurrentes sobre el mismo
    `MetaAdsAdapter` (el broker atiende conexiones en el mismo bucle de
    eventos) no deben poder pisarse la cuenta resuelta del otro."""

    gate: WriteOutcome | None
    platform_account_id: str


@dataclass(frozen=True, slots=True)
class MetaAdsAdapterConfig:
    """Credenciales resueltas por `composition` desde `BrokerSettings`."""

    app_id: str
    app_secret: str
    system_user_token: str
    write_budget_max_calls: int = _DEFAULT_WRITE_BUDGET_MAX_CALLS
    write_budget_window: timedelta = field(default=_DEFAULT_WRITE_BUDGET_WINDOW)


class MetaGraphClient(Protocol):
    """Sub-conjunto del Graph API que el adaptador necesita. En tests, un
    doble sencillo; en produccion, un wrapper fino sobre `facebook-business`
    (T026: "SDK mocked in tests").

    `update_node` es el unico punto por el que `execute_write` aplica un
    cambio real: el Graph API es uniforme (POST de campos sobre cualquier
    nodo), a diferencia de Google no hace falta un metodo por tipo de
    mutacion (deliverable 2: "Meta updates for adset/campaign daily_budget,
    status, ad status")."""

    def get_node(self, node_id: str, fields: Sequence[str]) -> Mapping[str, Any]: ...

    def get_edge(
        self,
        node_id: str,
        edge: str,
        fields: Sequence[str],
        params: Mapping[str, Any] | None = None,
        *,
        paginate: bool = True,
    ) -> Sequence[Mapping[str, Any]]: ...

    def update_node(self, node_id: str, fields: Mapping[str, Any]) -> None: ...

    def campaign_creation_currency(self, account_id: str) -> str: ...

    def prepare_child(self, parent: str, plan: Mapping[str, Any]) -> None: ...

    def create_paused_child(self, parent: str, plan: Mapping[str, Any]) -> Mapping[str, Any]: ...

    def create_paused_campaign(
        self, account_id: str, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]: ...

    def create_image(self, account_id: str, file_name: str, media: bytes) -> Mapping[str, Any]:
        """`POST /act_<id>/adimages` (deliverable 2, BL-6 residual): sube los
        bytes y devuelve `{"hash": ..., "url": ...}` -- `hash` es el
        manejador opaco que identifica la imagen para siempre en esa
        cuenta; `url` es una vista previa alojada por Meta, no por
        nosotros."""
        ...


_CAMPAIGN_FIELDS: Final = (
    "id",
    "name",
    "status",
    "daily_budget",
    "lifetime_budget",
    "objective",
    "account_id",
)
_AD_SET_FIELDS: Final = (
    "id",
    "name",
    "status",
    "campaign_id",
    "daily_budget",
    "lifetime_budget",
    "account_id",
)
_AD_FIELDS: Final = (
    "id",
    "name",
    "status",
    "adset_id",
    "account_id",
)
_FIELDS_BY_LEVEL: Final[dict[EntityLevel, Sequence[str]]] = {
    EntityLevel.CAMPAIGN: _CAMPAIGN_FIELDS,
    EntityLevel.AD_SET: _AD_SET_FIELDS,
    EntityLevel.AD: _AD_FIELDS,
}
_INSIGHTS_FIELDS: Final = (
    "date_start",
    "spend",
    "impressions",
    "clicks",
    "reach",
    "frequency",
    "actions",
    "action_values",
    "account_currency",
)
_HOURLY_BREAKDOWN: Final = "hourly_stats_aggregated_by_advertiser_time_zone"
_HOUR_PREFIX: Final = re.compile(r"^\s*(\d{1,2}):\d{2}")
_LAST_HOUR_OF_DAY: Final = 23


def is_retryable_error(error_code: int) -> bool:
    return error_code in _RETRYABLE_ERROR_CODES


def backoff_delay_seconds(
    attempt: int,
    *,
    base: float = _BACKOFF_BASE_SECONDS,
    cap: float = _BACKOFF_CAP_SECONDS,
    rng: random.Random | None = None,
) -> float:
    """ "Full jitter" (AWS architecture blog): espera aleatoria entre 0 y el
    techo exponencial. `rng` inyectable para tests deterministas. No es uso
    criptografico: solo espacia reintentos de red."""
    generator = rng or random.Random()  # noqa: S311
    ceiling = min(cap, base * (2**attempt))
    return generator.uniform(0, ceiling)


def _has_advantage_campaign_budget(campaign: Mapping[str, Any]) -> bool:
    """Senal de Advantage+ campaign budget (CBO): el presupuesto vive en la
    campana, no en el conjunto de anuncios. Cuando esto ocurre, el conjunto
    hijo no controla su propio presupuesto (FR-41)."""
    return campaign.get("daily_budget") is not None or campaign.get("lifetime_budget") is not None


class MetaAdsAdapter:
    def __init__(
        self,
        config: MetaAdsAdapterConfig,
        graph_client: MetaGraphClient,
        clock: Clock,
        *,
        write_pipeline: WriteAuthorizationPipeline | None = None,
        ad_library_client: MetaAdLibraryClient | None = None,
    ) -> None:
        self._config = config
        self._graph_client = graph_client
        self._clock = clock
        self._write_budget = WriteBudgetWindow(
            config.write_budget_max_calls, config.write_budget_window, clock
        )
        # `None` hasta que `composition` cablee `ApprovalVerifier` +
        # `CapsConfig` + `WriteLedgerStore` (misma seam que `GoogleAdsAdapter`).
        self._write_pipeline = write_pipeline
        # `composition/broker.py::_build_meta_adapter` cablea siempre un
        # `LiveMetaAdLibraryClient` real (004 tasks-2.md R7); `None` solo en
        # llamadores que no pasan por esa fabrica (tests unitarios de este
        # modulo) -- `search_ads_archive` falla cerrado con
        # `PlatformCapabilityNotImplementedError` en ese caso, nunca inventa
        # resultados.
        self._ad_library_client = ad_library_client

    def can_attempt_write(self, operations: int = 1) -> bool:  # noqa: ARG002 - see below
        """El chokepoint de `execution` (F2) debe llamar esto antes de
        intentar una escritura real: "Limites de la plataforma respetados
        en el adaptador, no en el llamante" (contracts/platform-port.md).

        `operations` (T035 finding 2, threat-model.md D-2/AL-5) hace que
        esta firma case con `create_paused_campaign`/`create_paused_child`
        (`Callable[[int], bool]`, ahora compartida con Google) -- Meta
        limita LLAMADAS por ventana deslizante, no operaciones por mutate,
        asi que el conteo se ignora aqui a proposito."""
        return self._write_budget.try_consume()

    async def fetch_account_inventory(self, account_ref: AccountRef) -> Sequence[AdEntitySnapshot]:
        fetched_at = self._clock.now()
        currency = _meta_currency(
            await self._get_node(account_ref.external_account_id, ("currency",))
        )
        campaigns = await self._get_edge(
            account_ref.external_account_id, "campaigns", _CAMPAIGN_FIELDS
        )
        snapshots = [
            self._campaign_to_snapshot(c, account_ref, fetched_at, currency) for c in campaigns
        ]
        for campaign in campaigns:
            campaign_node = scoped_meta_id(account_ref.external_account_id, str(campaign["id"]))
            ad_sets = await self._get_edge(campaign_node, "adsets", _AD_SET_FIELDS)
            snapshots.extend(
                self._ad_set_to_snapshot(ad_set, campaign, fetched_at, account_ref, currency)
                for ad_set in ad_sets
            )
            for ad_set in ad_sets:
                ad_set_node = scoped_meta_id(account_ref.external_account_id, str(ad_set["id"]))
                ads = await self._get_edge(ad_set_node, "ads", _AD_FIELDS)
                snapshots.extend(self._ad_to_snapshot(ad, account_ref, fetched_at) for ad in ads)
        return snapshots

    async def fetch_metrics(self, request: MetricsRequest) -> Sequence[MetricFactSnapshot]:
        entity_refs = request.entity_refs or await self._all_campaign_refs(request.account_ref)
        facts: list[MetricFactSnapshot] = []
        for entity_ref in entity_refs:
            account, _ = split_meta_scope(entity_ref.external_id)
            if (
                entity_ref.platform != PlatformCode.META
                or account != request.account_ref.external_account_id
            ):
                raise MetaAdsAdapterError("entidad fuera de la cuenta solicitada")
            rows = await self._get_insights(
                entity_ref.external_id, request.window, request.granularity
            )
            facts.extend(
                self._insight_row_to_snapshot(entity_ref, row, request.granularity) for row in rows
            )
        return facts

    async def read_entity_state(self, entity_ref: EntityRef) -> EntityStateSnapshot:
        fields = _FIELDS_BY_LEVEL.get(entity_ref.level, _CAMPAIGN_FIELDS)
        node = await self._get_node(entity_ref.external_id, fields)
        canonical_state = dict(node)
        return EntityStateSnapshot(
            entity_ref=entity_ref,
            status=_map_status(str(node.get("status", ""))),
            is_controllable=True,
            canonical_state=canonical_state,
            fetched_at=self._clock.now(),
        )

    async def upload_asset(self, request: AssetUploadRequest) -> PlatformAssetHandle:
        """Deliverable 2 / BL-6: `POST /act_<id>/adimages`. Idempotente por
        contenido -- Meta calcula el `hash` desde los bytes, asi que un
        reintento con los MISMOS bytes nunca crea una segunda imagen (BL-6,
        R2.7: "adimages es direccionable por contenido"). Limites y tipos
        comprobados ANTES de llamar al proveedor: nunca se manda al SDK un
        payload que ya sabemos que va a rechazar, y un tipo/tamaño fuera de
        politica no consume presupuesto de escritura.

        H1 (revision de seguridad 0.2.23): una peticion con `package_binding`/
        `package_approval` (paso `UPLOAD_CREATIVE` firmado) pasa PRIMERO por
        `WriteAuthorizationPipeline.admit_upload` -- las mismas R1-R6 que
        cualquier otro paso de paquete mas el cotejo de `sha256(media)`
        contra la plantilla firmada -- y el manejador queda anotado en el
        MISMO libro que resuelve `{creative_of:X}` para el `CREATE_AD` que
        depende de el. Una peticion SIN paquete (`upload_creative_asset`
        independiente) nunca toca el libro, exactamente igual que antes."""
        if request.mime_type not in _ALLOWED_CREATIVE_MIME_TYPES:
            raise MetaAdsAdapterError(f"tipo de imagen no admitido: {request.mime_type!r}")
        if len(request.media) > _MAX_CREATIVE_UPLOAD_BYTES:
            raise MetaAdsAdapterError("la imagen supera el limite de subida de Meta")
        account = _with_account_prefix(request.account_ref.external_account_id)

        async def _create_image() -> PlatformAssetHandle:
            result = await self._call(
                lambda: self._graph_client.create_image(account, request.file_name, request.media)
            )
            image_hash = result.get("hash")
            if not isinstance(image_hash, str) or not image_hash:
                raise MetaAdsAdapterError("Meta no confirmo el hash de la imagen subida")
            return PlatformAssetHandle(
                platform_asset_id=image_hash,
                preview_url=_allowed_creative_preview_url(result.get("url")),
            )

        if request.package_binding is None and request.package_approval is None:
            return await _create_image()
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
            upload=_create_image,
            now=self._clock.now(),
        )

    async def run_gaql(
        self,
        account_ref: AccountRef,  # noqa: ARG002
        query: str,  # noqa: ARG002
        *,
        max_rows: int,  # noqa: ARG002
    ) -> Sequence[Mapping[str, Any]]:
        """GAQL es el lenguaje de consulta de Google Ads; Meta no tiene
        equivalente en este puerto (mismo tratamiento que `upload_asset`)."""
        raise PlatformCapabilityNotImplementedError(
            "run_gaql es especifico de Google Ads (contracts/platform-port.md)"
        )

    async def read_reference_data(
        self, account_ref: AccountRef, *, tool: str, arguments: Mapping[str, Any]
    ) -> Sequence[Mapping[str, Any]]:
        """004 tasks-2.md R3/I1 (op `meta_reference_read`): seis lecturas
        sobre una arista concreta del Graph, despachadas por nombre de
        herramienta MCP. `meta_graph_reader` importa `MetaGraphClient` de
        este modulo -- import local para no cerrar el ciclo."""
        from safent_ads.broker.platforms import meta_graph_reader  # noqa: PLC0415

        account_node = _with_account_prefix(account_ref.external_account_id)
        if tool == "list_meta_pages":
            return await meta_graph_reader.fetch_pages(self._graph_client, account_node)
        if tool == "list_meta_pixels":
            return await meta_graph_reader.fetch_pixels(self._graph_client, account_node)
        if tool == "list_meta_audiences":
            return await self._fetch_meta_audiences(meta_graph_reader, account_node)
        if tool == "list_meta_catalogs":
            # Graph v26: `product_catalogs` no existe en `act_*` ("(#100) Tried
            # accessing nonexisting field", verificado en vivo 2026-09-15); los
            # catalogos cuelgan del Business. Denegacion limpia, nunca TOOL_FAILED.
            raise PlatformCapabilityNotImplementedError(
                "list_meta_catalogs: los catalogos cuelgan del Business, no de la cuenta"
            )
        if tool == "search_meta_targeting":
            return await meta_graph_reader.search_targeting(
                self._graph_client,
                account_node,
                kind=str(arguments["kind"]),
                query=str(arguments["query"]),
            )
        if tool == "get_meta_reach_estimate":
            estimate = await meta_graph_reader.fetch_reach_estimate(
                self._graph_client,
                account_node,
                optimization_goal=str(arguments["optimization_goal"]),
                countries=tuple(str(c) for c in arguments["countries"]),
            )
            return [dict(estimate)]
        raise PlatformCapabilityNotImplementedError(f"{tool} no soportado en Meta")

    async def _fetch_meta_audiences(
        self,
        meta_graph_reader: Any,  # noqa: ANN401 - modulo importado localmente (rompe el ciclo)
        account_node: str,
    ) -> list[dict[str, Any]]:
        """`list_meta_audiences` fusiona `customaudiences`+`saved_audiences`
        en una unica lista, cada fila con `kind` (contrato de
        `broker_reference_data_port.py`)."""
        custom = await meta_graph_reader.fetch_audiences(self._graph_client, account_node)
        saved = await meta_graph_reader.fetch_saved_audiences(self._graph_client, account_node)
        return [
            *({**row, "kind": "custom"} for row in custom),
            *({**row, "kind": "saved"} for row in saved),
        ]

    async def get_meta_graph(
        self,
        account_ref: AccountRef,
        *,
        node: str,
        edge: str,
        fields: Sequence[str],
        params: Mapping[str, Any],
    ) -> Sequence[Mapping[str, Any]]:
        """R5 (historia 19, S-1/B-2/A-1): el `node` que resuelve la
        credencial y hace la llamada real SIEMPRE sale de la cuenta
        autorizada, nunca del argumento crudo del llamante -- un nodo ajeno
        da el mismo `EntityNotFoundError` que uno inexistente, nunca se
        distinguen, y nunca abre sesion con la credencial de otro negocio.
        `paginate=False`: una unica pagina, el bróker jamas sigue el cursor
        `next` de Meta (el recorte de filas/bytes vive en `ads-api` y en el
        propio bróker, no aqui)."""
        resolved_node = await self._resolve_owned_node(node, account_ref.external_account_id)
        if not edge:
            return [await self._get_node(resolved_node, tuple(fields))]
        return await self._get_edge(
            resolved_node, edge, tuple(fields), dict(params) or None, paginate=False
        )

    async def _resolve_owned_node(self, node: str, external_account_id: str) -> str:
        """B-2: un `node` con prefijo `act_` solo puede ser la propia cuenta
        autorizada (se rechaza sin tocar ninguna credencial); uno sin
        prefijo se re-escribe SIEMPRE bajo la cuenta autorizada
        (`scoped_meta_id`) antes de resolver ninguna credencial, para que
        `LiveMetaGraphClient` nunca pueda derivarla de un `node` ajeno."""
        target = _with_account_prefix(external_account_id)
        if node.startswith(_META_ACCOUNT_PREFIX):
            if node != target:
                raise EntityNotFoundError(node)
            return node
        resolved = scoped_meta_id(external_account_id, node)
        try:
            owner = await self._get_node(resolved, ("account_id",))
        except MetaAdsAdapterError as exc:
            raise EntityNotFoundError(node) from exc
        if _meta_account_id(owner) != target:
            raise EntityNotFoundError(node)
        return resolved

    async def search_ads_archive(
        self,
        *,
        country: str,
        search_terms: str | None,
        search_page_ids: str | None,
        active_only: bool,
        external_account_id: str | None = None,
    ) -> Sequence[Mapping[str, Any]]:
        """R7: `search_competitor_ads` sobre la Biblioteca de Anuncios de
        Meta. Sin cliente configurado, falla cerrado -- nunca inventa
        resultados (`mcp/presentation/competitor_tools.py` ya traduce
        cualquier denegacion a `available=false` sin filtrar el motivo).
        `external_account_id` (fix/ad-library-over-composio): la cuenta de
        Meta ya conectada de la empresa que llama, resuelta por el llamante
        (`broker_competitor_research_port.py`) -- el cliente nativo la
        ignora (token de APP), el de Composio la necesita para autenticar
        el proxy."""
        if self._ad_library_client is None:
            raise PlatformCapabilityNotImplementedError("biblioteca de anuncios no configurada")
        return await search_meta_ads_archive(
            self._ad_library_client,
            country=country,
            search_terms=search_terms,
            search_page_ids=search_page_ids,
            active_only=active_only,
            external_account_id=external_account_id,
        )

    async def execute_write(  # noqa: PLR0911 - explicit fail-closed operation dispatch
        self,
        intent: WriteIntent,
        authorization: SignedAuthorization,
        idempotency_key: IdempotencyKey,
    ) -> WriteOutcome:
        """Los 8 controles de contracts/platform-port.md. `platform_account_id`
        no esta en el `entity_ref` de Meta (a diferencia de Google, un nodo
        de campana/conjunto no trae la cuenta en su id) -- la primera lectura
        remota lo resuelve Y sirve de precondicion de deriva a la vez, sin
        una segunda llamada; el orden de VEREDICTOS sigue siendo tope duro
        antes que deriva (`WriteAuthorizationPipeline.authorize`)."""
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
                currency=self._graph_client.campaign_creation_currency,
                create=self._graph_client.create_paused_campaign,
                consume_rate=self.can_attempt_write,
            )
        now = self._clock.now()
        resolution = await self._authorize_write(pipeline, intent, authorization, now)
        if resolution.gate is not None:
            return resolution.gate
        account_id = resolution.platform_account_id
        key = str(idempotency_key)
        if intent.operation == WriteOperation.NATIVE_WRITE:
            return await apply_native_write(
                pipeline=pipeline,
                intent=intent,
                authorization=authorization,
                key=key,
                account=account_id,
                now=now,
                update_node=self._graph_client.update_node,
                read_state=self.read_entity_state,
                consume_rate=self.can_attempt_write,
            )
        if intent.operation in (WriteOperation.CREATE_AD_SET, WriteOperation.CREATE_AD):
            return await create_paused_child(
                pipeline=pipeline,
                intent=intent,
                authorization=authorization,
                key=key,
                account=account_id,
                now=now,
                prepare=self._graph_client.prepare_child,
                create=self._graph_client.create_paused_child,
                consume_rate=self.can_attempt_write,
            )
        mutation = _MUTATE_FIELDS_BY_OPERATION.get(intent.operation)
        if mutation is None:
            return denial_outcome(WriteDenialCode.OPERATION_NOT_SUPPORTED)
        replay = await pipeline.begin_admitted_write(key, intent, authorization, account_id, now)
        if replay is not None:
            return replay
        outcome = await self._apply_write(intent, mutation)
        return pipeline.finalize(key, account_id, intent, outcome, now=now)

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
        now: datetime,
    ) -> _AuthorizationResolution:
        try:
            remote = await self.read_entity_state(intent.entity_ref)
        except MetaAdsAdapterError as exc:
            return _AuthorizationResolution(WriteOutcome("FAILED", None, None, str(exc), None), "")
        account_id = _meta_account_id(remote.canonical_state)
        remote_hash = PlatformStateHash.compute(remote.canonical_state).value
        gate = pipeline.authorize(
            intent,
            authorization,
            platform_account_id=account_id,
            remote_state_hash=remote_hash,
            now=now,
        )
        return _AuthorizationResolution(gate, account_id)

    async def _apply_write(self, intent: WriteIntent, mutation: _MutationFields) -> WriteOutcome:
        if not self.can_attempt_write():
            return WriteOutcome("FAILED", None, None, "rate_limited", None)
        node_id = intent.entity_ref.external_id
        try:
            fields = mutation(intent.valor_propuesto)
            if intent.operation in {WriteOperation.LOWER_BUDGET, WriteOperation.RAISE_BUDGET}:
                if intent.parametro != "daily_budget":
                    raise MetaAdsAdapterError("solo daily_budget esta habilitado para escritura")
            await self._call(lambda: self._graph_client.update_node(node_id, fields))
            confirmed = await self.read_entity_state(intent.entity_ref)
        except Exception:  # noqa: BLE001 - el proveedor puede haber aplicado la mutacion
            # El POST de Graph puede haberse aplicado aunque falle el transporte
            # o la lectura posterior. UNKNOWN conserva el recibo y bloquea un
            # reintento ciego hasta reconciliar con el estado remoto.
            return WriteOutcome("UNKNOWN", None, None, "provider_outcome_unknown", None)
        confirmed_hash = PlatformStateHash.compute(confirmed.canonical_state).value
        return WriteOutcome(
            "SUCCEEDED", intent.valor_propuesto, confirmed_hash, None, intent.entity_ref.external_id
        )

    async def _all_campaign_refs(self, account_ref: AccountRef) -> Sequence[EntityRef]:
        campaigns = await self._get_edge(account_ref.external_account_id, "campaigns", ("id",))
        return [
            EntityRef(
                PlatformCode.META,
                EntityLevel.CAMPAIGN,
                scoped_meta_id(account_ref.external_account_id, str(c["id"])),
            )
            for c in campaigns
        ]

    async def _get_node(self, node_id: str, fields: Sequence[str]) -> Mapping[str, Any]:
        return await self._call(lambda: self._graph_client.get_node(node_id, fields))

    async def _get_edge(
        self,
        node_id: str,
        edge: str,
        fields: Sequence[str],
        params: Mapping[str, Any] | None = None,
        *,
        paginate: bool = True,
    ) -> Sequence[Mapping[str, Any]]:
        return await self._call(
            lambda: self._graph_client.get_edge(node_id, edge, fields, params, paginate=paginate)
        )

    async def _get_insights(
        self, node_id: str, window: DateWindow, granularity: MetricGranularity
    ) -> Sequence[Mapping[str, Any]]:
        params: dict[str, Any] = {
            "time_range": {"since": window.start.isoformat(), "until": window.end.isoformat()},
            "time_increment": 1,
        }
        if granularity == MetricGranularity.HOURLY:
            params["breakdowns"] = [_HOURLY_BREAKDOWN]
        return await self._get_edge(node_id, "insights", _INSIGHTS_FIELDS, params)

    async def _call(self, operation: Callable[[], _ResultT]) -> _ResultT:
        try:
            return await asyncio.to_thread(operation)
        except Exception as exc:  # noqa: BLE001 - frontera con el SDK, nunca fail-open
            raise MetaAdsAdapterError(redact_sdk_error(exc)) from None

    def _campaign_to_snapshot(
        self,
        campaign: Mapping[str, Any],
        account_ref: AccountRef,
        fetched_at: datetime,
        currency: str,
    ) -> AdEntitySnapshot:
        return AdEntitySnapshot(
            entity_ref=EntityRef(
                PlatformCode.META,
                EntityLevel.CAMPAIGN,
                scoped_meta_id(account_ref.external_account_id, str(campaign["id"])),
            ),
            parent_ref=EntityRef(
                PlatformCode.META, EntityLevel.ACCOUNT, account_ref.external_account_id
            ),
            name=str(campaign["name"]),
            status=_map_status(str(campaign.get("status", ""))),
            is_controllable=True,
            learning_state=LearningState.NOT_APPLICABLE,
            budget=_map_budget(campaign, currency),
            bid_target=None,
            shared_budget_ref=None,
            canonical_state=dict(campaign),
            fetched_at=fetched_at,
        )

    def _ad_set_to_snapshot(
        self,
        ad_set: Mapping[str, Any],
        campaign: Mapping[str, Any],
        fetched_at: datetime,
        account_ref: AccountRef,
        currency: str,
    ) -> AdEntitySnapshot:
        is_controllable = not _has_advantage_campaign_budget(campaign)
        return AdEntitySnapshot(
            entity_ref=EntityRef(
                PlatformCode.META,
                EntityLevel.AD_SET,
                scoped_meta_id(account_ref.external_account_id, str(ad_set["id"])),
            ),
            parent_ref=EntityRef(
                PlatformCode.META,
                EntityLevel.CAMPAIGN,
                scoped_meta_id(account_ref.external_account_id, str(campaign["id"])),
            ),
            name=str(ad_set["name"]),
            status=_map_status(str(ad_set.get("status", ""))),
            is_controllable=is_controllable,
            learning_state=LearningState.NOT_APPLICABLE,
            budget=_map_budget(ad_set, currency) if is_controllable else None,
            bid_target=None,
            shared_budget_ref=None,
            canonical_state=dict(ad_set),
            fetched_at=fetched_at,
        )

    def _ad_to_snapshot(
        self, ad: Mapping[str, Any], account_ref: AccountRef, fetched_at: datetime
    ) -> AdEntitySnapshot:
        return AdEntitySnapshot(
            entity_ref=EntityRef(
                PlatformCode.META,
                EntityLevel.AD,
                scoped_meta_id(account_ref.external_account_id, str(ad["id"])),
            ),
            parent_ref=EntityRef(
                PlatformCode.META,
                EntityLevel.AD_SET,
                scoped_meta_id(account_ref.external_account_id, str(ad["adset_id"])),
            ),
            name=str(ad["name"]),
            status=_map_status(str(ad["status"])),
            is_controllable=True,
            learning_state=LearningState.NOT_APPLICABLE,
            budget=None,
            bid_target=None,
            shared_budget_ref=None,
            canonical_state=dict(ad),
            fetched_at=fetched_at,
        )

    def _insight_row_to_snapshot(
        self,
        entity_ref: EntityRef,
        row: Mapping[str, Any],
        granularity: MetricGranularity,
    ) -> MetricFactSnapshot:
        currency = str(row.get("account_currency", "")).strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", currency):
            raise MetaAdsAdapterError("Meta Insights no devolvio account_currency valido")
        stat_date = date.fromisoformat(str(row["date_start"]))
        stat_hour = (
            _meta_insight_hour(row.get(_HOURLY_BREAKDOWN))
            if granularity == MetricGranularity.HOURLY
            else None
        )
        return MetricFactSnapshot(
            entity_ref=entity_ref,
            stat_date=stat_date,
            stat_hour=stat_hour,
            currency=currency,
            spend=Money(round(float(row.get("spend", 0)) * 100), currency),
            impressions=int(row.get("impressions", 0)),
            clicks=int(row.get("clicks", 0)),
            reach=_optional_int(row.get("reach")),
            frequency=_optional_float(row.get("frequency")),
            conversions_by_kind=_actions_to_conversions(row.get("actions")),
            conversion_value=_actions_value_to_money(row.get("action_values"), currency),
            video_views_3s=None,
            video_views_75pct=None,
            search_lost_is_budget=None,
            search_lost_is_rank=None,
        )


def _meta_insight_hour(value: Any) -> int:  # noqa: ANN401 - valor crudo del Graph API
    match = _HOUR_PREFIX.match(str(value or "").split(" - ", 1)[0])
    if match is None:
        raise MetaAdsAdapterError("Meta Insights no devolvio un tramo horario valido")
    hour = int(match.group(1))
    if not 0 <= hour <= _LAST_HOUR_OF_DAY:
        raise MetaAdsAdapterError("Meta Insights devolvio una hora fuera de rango")
    return hour


def _map_status(raw_status: str) -> AdEntityStatus:
    normalized = raw_status.upper()
    if normalized == "ACTIVE":
        return AdEntityStatus.ACTIVE
    if normalized == "PAUSED":
        return AdEntityStatus.PAUSED
    if normalized in {"DELETED", "ARCHIVED"}:
        return AdEntityStatus.REMOVED
    raise MetaAdsAdapterError("Meta Ads devolvio un estado de entidad desconocido")


def _map_budget(node: Mapping[str, Any], currency: str) -> Budget | None:
    daily = node.get("daily_budget")
    if daily is not None:
        return Budget(Money(int(daily), currency), BudgetKind.DAILY)
    lifetime = node.get("lifetime_budget")
    if lifetime is not None:
        return Budget(Money(int(lifetime), currency), BudgetKind.LIFETIME)
    return None


def _meta_currency(account: Mapping[str, Any]) -> str:
    currency = str(account.get("currency", "")).strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", currency):
        raise MetaAdsAdapterError("la cuenta Meta no devolvio una moneda valida")
    return currency


def _actions_to_conversions(actions: Any) -> Mapping[str, int]:  # noqa: ANN401 - JSON del SDK
    if not actions:
        return {}
    return {
        str(action["action_type"]): int(float(action["value"]))
        for action in actions
        if "action_type" in action and "value" in action
    }


def _actions_value_to_money(action_values: Any, currency: str) -> Money | None:  # noqa: ANN401
    if not action_values:
        return None
    total = sum(float(item["value"]) for item in action_values if "value" in item)
    return Money(round(total * 100), currency)


def _optional_int(value: Any) -> int | None:  # noqa: ANN401 - valor crudo del SDK
    return None if value is None else int(value)


def _optional_float(value: Any) -> float | None:  # noqa: ANN401 - valor crudo del SDK
    return None if value is None else float(value)


def _allowed_creative_preview_url(  # noqa: PLR0911 - independent fail-closed URL checks
    value: object,
) -> str | None:
    """B4 (revision de seguridad): `None` para cualquier valor que no sea
    una URL HTTPS de un host propio de Meta -- nunca se propaga una URL no
    fiable como `preview_url` (que `packages.infrastructure.
    chokepoint_step_executor` sustituira en `link_data.picture`)."""
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme != "https" or not parsed.hostname:
        return None
    host = parsed.hostname.lower()
    try:
        ip_address(host)
    except ValueError:
        pass
    else:
        return None  # una IP literal nunca es un host de Meta
    if host in _ALLOWED_PREVIEW_URL_HOSTS:
        return value
    if any(host.endswith(suffix) for suffix in _ALLOWED_PREVIEW_URL_HOST_SUFFIXES):
        return value
    return None


def _with_account_prefix(value: str) -> str:
    """Antepone `act_` si falta -- forma canonica de cuenta que usa el
    resto del sistema (`AccountRef`/`caps.yaml`), a diferencia del
    `account_id` crudo que devuelve el Graph API."""
    return value if value.startswith(_META_ACCOUNT_PREFIX) else f"{_META_ACCOUNT_PREFIX}{value}"


def _meta_account_id(canonical_state: Mapping[str, Any]) -> str:
    """El campo `account_id` del Graph API llega SIN el prefijo `act_`
    (a diferencia del `external_id` de cuenta que usa el resto del sistema,
    `AccountRef`/`caps.yaml`) -- se antepone aqui, en el unico punto que
    convierte de la forma del Graph API a la forma canonica de la cuenta."""
    raw_account_id = canonical_state.get("account_id")
    if not raw_account_id:
        raise MetaAdsAdapterError("nodo sin account_id: no se puede resolver la cuenta")
    return _with_account_prefix(str(raw_account_id))


def _money_minor_units(value: Any) -> int:  # noqa: ANN401 - JsonValue del WriteIntent
    """`valor_propuesto` llega como `proposals.domain.money.Money.
    to_canonical()`: `{"amount": "5.00", "currency": "..."}`. El Graph API
    factura `daily_budget`/`lifetime_budget` en centimos (unidad menor),
    no en unidades enteras."""
    if not isinstance(value, Mapping) or "amount" not in value:
        raise MetaAdsAdapterError("valor_propuesto sin importe valido para mutar presupuesto")
    try:
        return int((Decimal(str(value["amount"])) * _MINOR_UNITS_PER_MAJOR).to_integral_value())
    except InvalidOperation as exc:
        raise MetaAdsAdapterError("importe no numerico") from exc


_MutationFields = Callable[[Any], dict[str, Any]]

_MUTATE_FIELDS_BY_OPERATION: Final[dict[WriteOperation, _MutationFields]] = {
    WriteOperation.LOWER_BUDGET: lambda value: {"daily_budget": _money_minor_units(value)},
    WriteOperation.RAISE_BUDGET: lambda value: {"daily_budget": _money_minor_units(value)},
    WriteOperation.PAUSE: lambda _value: {"status": _PAUSED_STATUS},
    WriteOperation.RESUME: lambda _value: {"status": _ACTIVE_STATUS},
    # "Rotar fuera" una creatividad es siempre pausarla (deliverable 2: "ad
    # status" para las dos plataformas) -- nunca reanuda.
    WriteOperation.ROTATE_OUT_CREATIVE: lambda _value: {"status": _PAUSED_STATUS},
    # Borrado irreversible (design.md §0.7): Meta
    # Graph API borra una campana/conjunto/anuncio poniendo su `status` a
    # `DELETED` -- mismo mecanismo uniforme que pausar/reanudar, ningun
    # endpoint DELETE propio.
    WriteOperation.DELETE: lambda _value: {"status": _DELETED_STATUS},
}
