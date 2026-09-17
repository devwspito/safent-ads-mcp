"""Entrypoint de `ads-broker`: escucha en el socket Unix
(`broker.presentation.socket_server.serve`, `SO_PEERCRED` contra
`ADS_BROKER_ALLOWED_UIDS` antes de leer ningun byte, plan.md §3.3) y
enruta las tres lecturas de US1 hacia `GoogleAdsAdapter`/`MetaAdsAdapter`
via `PlatformAdapterRegistry`.

`_build_registry` ya NO fija los adaptadores una unica vez al arrancar:
`DynamicPlatformAdapterRegistry` (`broker/infrastructure/
dynamic_platform_adapters.py`) resuelve las credenciales de VENDOR de esa
plataforma (`client_id`+`client_secret` para Google,
`app_id`+`app_secret` para Meta) contra el mismo `EncryptedCredentialStore`
que usa el flujo OAuth "Conectar" EN CADA ACCESO -- el mismo patron que
`broker/platforms/dynamic_oauth_adapters.py` ya usaba solo para "Conectar".
Si el propietario teclea o borra la app desde el panel, la siguiente
lectura/escritura (incluido el proximo tick de `IngestionCycle`, via el
socket) la recoge sin reiniciar `ads-broker` -- ya no hace falta editar
`vendor.env` a mano ni relanzar el proceso. `BrokerSettings`/entorno queda
como respaldo de desarrollo, solo cuando el almacen no tiene nada guardado.

Sin credenciales (ni almacen ni respaldo), la plataforma deniega con
`AppCredentialsNotConfiguredError` (`broker/presentation/dispatcher.py` la
traduce a `PLATFORM_APP_NOT_CONFIGURED`, tanto para lecturas como para
`execute_write`) -- fail closed, nunca un adaptador a medias ni una
respuesta inventada. La resolucion DNS del host fijo de cada plataforma
(`assert_egress_allowed`, threat-model.md C-12) sigue evaluandose UNA vez
al arrancar: el host no cambia con la credencial, asi que una resolucion
bloqueada deja la plataforma fuera durante toda la vida del proceso, igual
que antes de este cableado.

`GoogleAdsAdapter`/`MetaAdsAdapter` cargan contadores propios entre
llamadas (`DailyOperationBudget`/`WriteBudgetWindow`) que una
reconstruccion en cada lectura resetearia -- una forma silenciosa de
saltarse el tope diario. `DynamicPlatformAdapterRegistry` cachea el
adaptador ya construido y solo lo rehace cuando las credenciales
resueltas cambian de verdad.

Regla de producto (integracion): las credenciales de CLIENTE (refresh
token de Google, system user token de Meta) se conectan por OAuth desde
el panel y NUNCA viven en `BrokerSettings`/variables de entorno --
`GoogleAdsAdapterConfig.refresh_token`/`login_customer_id` y
`MetaAdsAdapterConfig.system_user_token` quedan vacios a proposito
(campos que `GoogleAdsAdapter`/`MetaAdsAdapter` no leen salvo para los
topes de operaciones, `broker/platforms/*.py`): `LiveGoogleAdsSearchClient`/
`LiveMetaGraphClient` resuelven la credencial real por cuenta contra
`CredentialStorePort` en cada llamada. `ConnectedCredentialStore` resuelve
el alias de cuenta guardado durante OAuth en el mismo almacen cifrado.
Credenciales ausentes, caducadas o revocadas fallan cerrado.

`caps.yaml` (topes duros, `ADS_BROKER_HARD_CAPS_FILE`) se carga y valida
al arrancar -- fail loud si falta o esta mal formado (`check-secrets` del
Makefile ya lo exige antes de `make up`).

`execute_write` (F2, contracts/platform-port.md): `_build_write_pipeline`
ensambla `ApprovalVerifier` (desde `BrokerSettings.approval_public_key`) +
`CapsConfig` (ya cargado antes de construir el registro) +
`WriteLedgerStore` (`credential_store_dir/write_ledger.sqlite3`) en un
unico `WriteAuthorizationPipeline`, compartido por `GoogleAdsAdapter` y
`MetaAdsAdapter` -- los 8 controles del contrato pasan a estar activos.
Fail closed sin tumbar el proceso: una clave publica ausente o mal
formada deja `write_pipeline=None` en los dos adaptadores (con un log de
error) en vez de impedir que el broker arranque -- las lecturas siguen
sirviendo, y cada adaptador ya deniega `execute_write` con
`error_code = "write_path_not_wired"` cuando no tiene pipeline (el mismo
comportamiento que antes de este cableado)."""

from __future__ import annotations

import asyncio
import os
from decimal import Decimal
from pathlib import Path
from typing import Final

import structlog

from safent_ads.broker.application.app_credentials_service import AppCredentialsService
from safent_ads.broker.application.managed_oauth_connect import (
    ManagedOAuthConfig,
    ManagedOAuthConnectService,
)
from safent_ads.broker.application.oauth_connect_flow import OAuthConnectFlow
from safent_ads.broker.application.ports import CredentialStorePort
from safent_ads.broker.application.render_image import RenderImageService
from safent_ads.broker.infrastructure.adapter_registry import PlatformAdapterRegistry
from safent_ads.broker.infrastructure.caps_config import (
    CapsConfig,
    CapsResolverPort,
    HardCapsStatus,
    load_caps_snapshot,
)
from safent_ads.broker.infrastructure.caps_state import (
    CapsStateStore,
    CapsStateUnwritableError,
    assert_state_directory_is_private,
)
from safent_ads.broker.infrastructure.composio_lease import ComposioLeaseStore
from safent_ads.broker.infrastructure.connected_credential_store import ConnectedCredentialStore
from safent_ads.broker.infrastructure.credential_store import EncryptedCredentialStore
from safent_ads.broker.infrastructure.dynamic_platform_adapters import (
    DynamicPlatformAdapterRegistry,
    GoogleAppSecrets,
    MetaAppSecrets,
)
from safent_ads.broker.infrastructure.effective_caps import EffectiveCapsResolver
from safent_ads.broker.infrastructure.egress_guard import EgressDeniedError, assert_egress_allowed
from safent_ads.broker.infrastructure.hard_caps_service import HardCapsService, PanelCapsWriting
from safent_ads.broker.infrastructure.write_ledger_store import WriteLedgerStore
from safent_ads.broker.platforms.composio_sdk_clients import (
    ComposioGoogleAdsSearchClient,
    ComposioMetaAdLibraryClient,
    ComposioMetaGraphClient,
)
from safent_ads.broker.platforms.composio_transport import ComposioAdsTransport
from safent_ads.broker.platforms.dynamic_oauth_adapters import (
    DynamicGoogleOAuthAdapter,
    DynamicMetaOAuthAdapter,
)
from safent_ads.broker.platforms.google_ads_adapter import (
    GoogleAdsAdapter,
    GoogleAdsAdapterConfig,
    GoogleAdsQueryTemplates,
)
from safent_ads.broker.platforms.google_oauth_adapter import GoogleOAuthAdapterConfig
from safent_ads.broker.platforms.live_google_ads_client import (
    LiveGoogleAdsSearchClient,
    LiveGoogleAssetUploadClient,
    LiveGoogleKeywordIdeaClient,
)
from safent_ads.broker.platforms.live_meta_ad_library_client import LiveMetaAdLibraryClient
from safent_ads.broker.platforms.live_meta_graph_client import LiveMetaGraphClient
from safent_ads.broker.platforms.meta_ads_adapter import MetaAdsAdapter, MetaAdsAdapterConfig
from safent_ads.broker.platforms.meta_oauth_adapter import MetaOAuthAdapterConfig
from safent_ads.broker.platforms.native_mcp import NativeMcpReadGateway
from safent_ads.broker.platforms.oauth_http import HttpxOAuthHttpClient
from safent_ads.broker.platforms.write_pipeline import WriteAuthorizationPipeline
from safent_ads.broker.presentation.dispatcher import BrokerRuntime
from safent_ads.broker.presentation.socket_server import serve
from safent_ads.composition.creative_renderers import build_image_renderers
from safent_ads.composition.gaql_templates import load_gaql_template
from safent_ads.composition.settings import BrokerSettings
from safent_ads.creative.domain.enums import RendererName
from safent_ads.creative.infrastructure.in_memory_asset_store import InMemoryAssetStore
from safent_ads.iam.infrastructure.enterprise_ads_authority import EnterpriseAdsAuthority
from safent_ads.logging_setup import configure_logging
from safent_ads.observability.server import start_metrics_server
from safent_ads.shared.clock import Clock, SystemClock
from safent_ads.shared.crypto.ed25519 import ApprovalVerifier, VerificationKeyMissingError
from safent_ads.shared.ids import PlatformCode

logger = structlog.get_logger(__name__)

# T127: mismo puerto fijo que `composition/app.py`/`composition/worker.py`
# -- contenedor/netns propio, sin colision real -- siempre en loopback
# (`observability/server.py`), nunca publicado en compose. `ads-broker`
# solo expone `ads_broker_write_denials_total` (`broker/platforms/
# write_pipeline.py`): sin sesion de BD propia (`BrokerSettings` aislado a
# proposito, threat-model.md C-24), no reporta `ads_credential_health`
# (eso vive en `ads-api`, unico proceso con `database_url`).
_METRICS_PORT = 9410

_WRITE_LEDGER_FILENAME: Final = "write_ledger.sqlite3"

# gid del grupo suplementario `ads-broker-clients` (Containerfile). 0600
# propiedad de adsbroker dejaria fuera a adsapi/adsworker con EACCES en
# connect() antes de que SO_PEERCRED llegue a evaluarse; 0666 lo abriria a
# cualquier proceso con acceso al volumen `broker-sock`. 0660 + este grupo
# es el permiso minimo que deja pasar al cliente legitimo — la autorizacion
# real sigue siendo el chequeo de SO_PEERCRED de `socket_server.serve`, no
# el modo del fichero.
_SOCKET_GROUP_GID = 10003
_SOCKET_MODE = 0o660

_GOOGLE_ADS_EGRESS_HOST = "googleads.googleapis.com"
_META_GRAPH_EGRESS_HOST = "graph.facebook.com"

# M-3 (revision de seguridad 0.2.22): `render_image` devuelve una imagen en
# base64 que supera de sobra los 64 KiB por defecto de `socket_server.py::
# _DEFAULT_MAX_FRAME_BYTES` -- pero ese techo mayor es SOLO para la
# RESPUESTA de esa `op` (`socket_server.py::_PER_OP_MAX_RESPONSE_BYTES`),
# no para la lectura de la peticion entrante: subir el limite GLOBAL del
# socket (como se hacia antes aqui) tambien dejaba pasar una peticion de
# 16 MiB de basura bajo cualquier `op`, incluidas las de solo lectura --
# una superficie de denegacion de servicio nueva. `serve()` se deja en su
# `max_frame_bytes` por defecto (64 KiB, solo lectura) a proposito.


def _build_google_adapter(
    secrets: GoogleAppSecrets | None,
    credential_store: CredentialStorePort,
    clock: Clock,
    write_pipeline: WriteAuthorizationPipeline | None,
    templates: GoogleAdsQueryTemplates,
    *,
    composio_transport: ComposioAdsTransport | None = None,
) -> GoogleAdsAdapter:
    """Fabrica pura: `secrets` ya viene resuelta (almacen o respaldo de
    entorno, `DynamicPlatformAdapterRegistry._google_secrets`) -- decidir
    si HAY credenciales es responsabilidad del registro dinamico, no de
    esta funcion."""
    client_id = secrets.client_id if secrets is not None else ""
    client_secret = secrets.client_secret if secrets is not None else ""
    search_client = (
        ComposioGoogleAdsSearchClient(
            client_id=client_id,
            client_secret=client_secret,
            credential_store=credential_store,
            composio_transport=composio_transport,
        )
        if composio_transport is not None
        else LiveGoogleAdsSearchClient(
            client_id=client_id,
            client_secret=client_secret,
            credential_store=credential_store,
        )
    )
    config = GoogleAdsAdapterConfig(
        client_id=client_id,
        client_secret=client_secret,
        # De CLIENTE, no de VENDOR: `LiveGoogleAdsSearchClient` los resuelve
        # por cuenta contra `CredentialStorePort`, nunca desde aqui.
        refresh_token="",
        login_customer_id="",
    )
    return GoogleAdsAdapter(
        config,
        search_client,
        clock,
        write_pipeline=write_pipeline,
        templates=templates,
        # `LiveGoogleKeywordIdeaClient` compone la MISMA `search_client`
        # (`build_sdk_client`, 004 tasks-2.md R4): en modo Composio hereda
        # su credencial leased sin una segunda copia -- si esa credencial
        # es `composio`, `KeywordPlanIdeaService` no esta en la lista
        # blanca de `_GoogleSdkFacade` y falla cerrado, nunca inventa ideas.
        keyword_idea_client=LiveGoogleKeywordIdeaClient(search_client),
        # `LiveGoogleAssetUploadClient` (residual a04cf9f, threat-model.md
        # #17): mismo criterio -- compone la MISMA `search_client` en los
        # dos modos (directo o Composio), nunca una segunda resolucion de
        # credencial. Sin esto, `upload_asset` fallaba cerrado con
        # `PlatformCapabilityNotImplementedError` incluso con credencial de
        # Google conectada.
        asset_upload_client=LiveGoogleAssetUploadClient(search_client),
    )


def _load_google_ads_query_templates() -> GoogleAdsQueryTemplates:
    """T162: las cinco consultas de `GoogleAdsAdapter` dejan de ser
    constantes Python -- se cargan y validan aqui, contra
    `broker/platforms/gaql/*.gaql`, y un fichero ausente o mal formado tumba el
    arranque del broker (`GaqlTemplateError`), nunca sirve una plantilla a
    medias en produccion."""
    return GoogleAdsQueryTemplates(
        campaign_inventory=load_gaql_template("campaign_inventory"),
        ad_group_inventory=load_gaql_template("ad_group_inventory"),
        ad_inventory=load_gaql_template("ad_inventory"),
        campaign_metrics=load_gaql_template("campaign_metrics"),
        campaign_budget_lookup=load_gaql_template("campaign_budget_lookup"),
    )


def _build_meta_adapter(
    secrets: MetaAppSecrets | None,
    credential_store: CredentialStorePort,
    clock: Clock,
    write_pipeline: WriteAuthorizationPipeline | None,
    composio_transport: ComposioAdsTransport | None = None,
) -> MetaAdsAdapter:
    """Fabrica pura -- mismo criterio que `_build_google_adapter`."""
    app_id = secrets.app_id if secrets is not None else ""
    app_secret = secrets.app_secret if secrets is not None else ""
    graph_client = (
        ComposioMetaGraphClient(
            app_id=app_id,
            app_secret=app_secret,
            credential_store=credential_store,
            composio_transport=composio_transport,
        )
        if composio_transport is not None
        else LiveMetaGraphClient(
            app_id=app_id,
            app_secret=app_secret,
            credential_store=credential_store,
        )
    )
    config = MetaAdsAdapterConfig(
        app_id=app_id,
        app_secret=app_secret,
        # De CLIENTE, no de VENDOR: `LiveMetaGraphClient` lo resuelve por
        # cuenta contra `CredentialStorePort`, nunca desde aqui.
        system_user_token="",
    )
    # `LiveMetaAdLibraryClient` (R7): token de APP (`app_id|app_secret`),
    # nunca el token de sistema de una cuenta -- la Biblioteca de Anuncios
    # no cuelga de ninguna cuenta conectada. Sin `app_id`/`app_secret`
    # configurados el cliente falla cerrado en la primera llamada, igual
    # que `graph_client` sin credenciales. En modo Composio (companion, sin
    # app nativa de Meta) ese fallo era SIEMPRE -- `ComposioMetaAdLibraryClient`
    # autentica en su lugar contra la cuenta de Meta ya conectada de cada
    # negocio (fix/ad-library-over-composio).
    ad_library_client = (
        ComposioMetaAdLibraryClient(composio_transport=composio_transport)
        if composio_transport is not None
        else LiveMetaAdLibraryClient(app_id=app_id, app_secret=app_secret)
    )
    return MetaAdsAdapter(
        config,
        graph_client,
        clock,
        write_pipeline=write_pipeline,
        ad_library_client=ad_library_client,
    )


def _build_write_pipeline(
    settings: BrokerSettings, caps: CapsResolverPort, credential_store: ConnectedCredentialStore
) -> WriteAuthorizationPipeline | None:
    """`None` deja a los dos adaptadores denegar `execute_write` con
    `WRITE_PATH_NOT_WIRED` (fail closed) en vez de tumbar el arranque del
    broker: una clave publica de aprobacion invalida no debe impedir que
    las lecturas sigan funcionando (mismo criterio que un vendor de
    plataforma sin configurar, `_build_registry`)."""
    try:
        verifier = ApprovalVerifier.from_public_key_b64(settings.approval_public_key)
    except VerificationKeyMissingError:
        logger.error("broker_approval_public_key_invalid")
        return None
    ledger = WriteLedgerStore(settings.credential_store_dir / _WRITE_LEDGER_FILENAME)
    authority = (
        EnterpriseAdsAuthority(settings.managed_trust()) if settings.managed_central else None
    )
    return WriteAuthorizationPipeline(
        verifier,
        caps,
        ledger,
        scope_resolver=credential_store.resolve_write_scope,
        require_owner_approval=True,
        managed_authority=authority,
        require_managed_binding=settings.managed_central,
    )


async def _egress_allowed(hostname: str, platform: str) -> bool:
    try:
        await assert_egress_allowed(hostname)
    except EgressDeniedError:
        logger.error("broker_egress_denied", platform=platform, hostname=hostname)
        return False
    return True


def _google_app_secrets_fallback(settings: BrokerSettings) -> GoogleAppSecrets | None:
    """Respaldo de DESARROLLO: mismo criterio que `_google_env_fallback`,
    reducido a los dos campos que `_build_google_adapter` necesita de
    verdad (sin `login_customer_id`, de CLIENTE)."""
    config = _google_env_fallback(settings)
    if config is None:
        return None
    return GoogleAppSecrets(
        client_id=config.client_id,
        client_secret=config.client_secret,
    )


def _meta_app_secrets_fallback(settings: BrokerSettings) -> MetaAppSecrets | None:
    """Respaldo de DESARROLLO: mismo criterio que `_meta_env_fallback`."""
    config = _meta_env_fallback(settings)
    if config is None:
        return None
    return MetaAppSecrets(app_id=config.app_id, app_secret=config.app_secret)


async def _build_registry(
    settings: BrokerSettings,
    caps: CapsResolverPort,
    store: EncryptedCredentialStore,
    composio_lease: ComposioLeaseStore | None = None,
) -> PlatformAdapterRegistry:
    """`store` es el MISMO `EncryptedCredentialStore` que `_build_runtime`
    cablea en el flujo OAuth "Conectar" (`run`, mas abajo) -- las
    credenciales de VENDOR que el propietario teclea desde el panel llegan
    a las lecturas/escrituras de US1/F2 sin reiniciar el broker
    (`DynamicPlatformAdapterRegistry`, `broker/infrastructure/
    dynamic_platform_adapters.py`)."""
    clock = SystemClock()
    credential_store = ConnectedCredentialStore(store, clock, settings.google_ads_login_customer_id)
    write_pipeline = _build_write_pipeline(settings, caps, credential_store)
    templates = _load_google_ads_query_templates()
    composio_transport = None
    if (settings.companion_mode or settings.composio_api_key is not None) and await _egress_allowed(
        "backend.composio.dev", "composio"
    ):
        composio_transport = ComposioAdsTransport(
            api_key_source=lambda: _composio_config(settings, composio_lease).api_key,
            credential_store=credential_store,
        )

    adapters = DynamicPlatformAdapterRegistry(
        store=store,
        google_factory=lambda secrets: _build_google_adapter(
            secrets,
            credential_store,
            clock,
            write_pipeline,
            templates,
            composio_transport=composio_transport,
        ),
        meta_factory=lambda secrets: _build_meta_adapter(
            secrets, credential_store, clock, write_pipeline, composio_transport
        ),
        google_fallback=_google_app_secrets_fallback(settings),
        meta_fallback=_meta_app_secrets_fallback(settings),
        google_egress_allowed=await _egress_allowed(_GOOGLE_ADS_EGRESS_HOST, "google"),
        meta_egress_allowed=await _egress_allowed(_META_GRAPH_EGRESS_HOST, "meta"),
        managed_transport_enabled=lambda: (
            composio_transport is not None
            and bool(_composio_config(settings, composio_lease).api_key)
        ),
    )
    return PlatformAdapterRegistry(adapters=adapters)


def _harden_socket_permissions(socket_path: Path) -> None:
    os.chown(socket_path, -1, _SOCKET_GROUP_GID)
    os.chmod(socket_path, _SOCKET_MODE)


# --- lane: oauth-connect (cableado) ---
def _google_env_fallback(settings: BrokerSettings) -> GoogleOAuthAdapterConfig | None:
    """Respaldo de DESARROLLO: `GOOGLE_ADS_*` en `secrets/broker.env`. La
    fuente principal es el almacen cifrado (owner decision, app-credentials-ui,
    `PUT /platform-apps/google`) -- este fallback solo entra en juego cuando
    el almacen no tiene nada guardado (`DynamicGoogleOAuthAdapter._required`).
    `client_id`/`client_secret` ausentes = sin fallback."""
    if settings.google_ads_client_id is None or settings.google_ads_client_secret is None:
        return None
    return GoogleOAuthAdapterConfig(
        client_id=settings.google_ads_client_id.get_secret_value(),
        client_secret=settings.google_ads_client_secret.get_secret_value(),
        login_customer_id=settings.google_ads_login_customer_id,
    )


def _meta_env_fallback(settings: BrokerSettings) -> MetaOAuthAdapterConfig | None:
    """Respaldo de DESARROLLO: `META_APP_*` en `secrets/broker.env` -- mismo
    criterio que `_google_env_fallback`."""
    if settings.meta_app_id is None or settings.meta_app_secret is None:
        return None
    return MetaOAuthAdapterConfig(
        app_id=settings.meta_app_id.get_secret_value(),
        app_secret=settings.meta_app_secret.get_secret_value(),
    )


def _composio_config(
    settings: BrokerSettings, lease: ComposioLeaseStore | None
) -> ManagedOAuthConfig:
    if lease is not None:
        return lease.current_config()
    if settings.companion_mode:
        return ManagedOAuthConfig(api_key="")
    return ManagedOAuthConfig(
        api_key=settings.composio_api_key.get_secret_value() if settings.composio_api_key else "",
        google_auth_config_id=settings.composio_googleads_auth_config_id,
        meta_auth_config_id=settings.composio_metaads_auth_config_id,
    )


def _list_price_usd_by_renderer(settings: BrokerSettings) -> dict[RendererName, Decimal]:
    """M-2 (revision de seguridad 0.2.22): mismos precios de lista que ya
    configura cada adaptador (`fal_image_price_usd`/
    `openai_image_price_usd_estimate`) -- una sola fuente de verdad, nunca
    un numero distinto inventado solo para el tope de coste."""
    return {
        RendererName.FLUX2_KLEIN_9B: settings.fal_image_price_usd,
        RendererName.GPT_IMAGE_1_5: settings.openai_image_price_usd_estimate,
    }


def _build_render_image_service(settings: BrokerSettings, clock: Clock) -> RenderImageService:
    """`InMemoryAssetStore` (nunca `LocalAssetStorage`): el broker no
    escribe en el directorio de activos de `ads-api` (threat-model.md C-29,
    procesos distintos, sin disco compartido por contrato) -- los bytes
    viven en memoria de este proceso solo mientras dura la peticion
    `render_image`, `RenderImageService.render` los recupera con `pop` y
    los devuelve por el socket.

    M-2: cuota por negocio y tope de coste con los valores REALES de
    `BrokerSettings` -- sin esto, `RenderImageService` cae en sus defectos
    documentados (pensados para tests, no para produccion)."""
    asset_store = InMemoryAssetStore()
    image_renderers = build_image_renderers(settings, asset_store=asset_store, clock=clock)
    return RenderImageService(
        image_renderers,
        asset_store=asset_store,
        clock=clock,
        quota_per_business_per_minute=settings.render_image_quota_per_minute,
        quota_per_business_per_day=settings.render_image_quota_per_day,
        max_cost_usd=settings.render_image_max_cost_usd,
        list_price_usd_by_renderer=_list_price_usd_by_renderer(settings),
    )


def _build_runtime(
    settings: BrokerSettings,
    registry: PlatformAdapterRegistry,
    store: EncryptedCredentialStore,
    *,
    composio_lease: ComposioLeaseStore | None = None,
    hard_caps_status: HardCapsStatus | None = None,
    hard_caps: HardCapsService | None = None,
) -> BrokerRuntime:
    """Todo lo que el socket necesita: adaptadores de plataforma (US1), el
    flujo OAuth "Conectar" (US3) y el alta/estado de credenciales de VENDOR
    (US-app-credentials-ui). `store` es el MISMO `EncryptedCredentialStore`
    que `_build_registry` le paso a `DynamicPlatformAdapterRegistry` (`run`,
    mas abajo) -- una unica fuente de verdad para las credenciales de
    VENDOR, sin dos copias del almacen cifrado abriendo el mismo fichero
    por separado. `DynamicGoogleOAuthAdapter`/`DynamicMetaOAuthAdapter`
    resuelven la app del propietario desde el almacen EN CADA LLAMADA (no
    aqui, una vez, al arrancar) -- el entorno
    (`_google_env_fallback`/`_meta_env_fallback`) solo entra si el almacen
    no tiene nada guardado todavia. Sin ninguno de los dos, el flujo falla
    cerrado en el momento de usarse (`AppCredentialsNotConfiguredError` ->
    `PLATFORM_APP_NOT_CONFIGURED`) en vez de impedir que el broker
    arranque -- las lecturas y el resto de operaciones siguen
    disponibles."""
    clock = SystemClock()
    http = HttpxOAuthHttpClient()
    managed = ManagedOAuthConnectService(
        lambda: _composio_config(settings, composio_lease),
        http,
        store,
        clock,
        required=settings.companion_mode,
    )
    google = DynamicGoogleOAuthAdapter(store, http, clock, fallback=_google_env_fallback(settings))
    meta = DynamicMetaOAuthAdapter(
        store,
        http,
        clock,
        fallback=_meta_env_fallback(settings),
        request_mcp_access=settings.meta_native_mcp_enabled,
    )
    return BrokerRuntime(
        adapters=registry,
        oauth_flow=OAuthConnectFlow(store, google, meta, clock, managed=managed),
        app_credentials=AppCredentialsService(
            store,
            clock,
            managed_platforms=lambda: frozenset(
                platform for platform in PlatformCode if managed.configured(platform)
            ),
            managed_required=settings.companion_mode,
        ),
        composio_lease=composio_lease,
        hard_caps_status=hard_caps_status,
        hard_caps=hard_caps,
        render_image_service=_build_render_image_service(settings, clock),
        native_ads=NativeMcpReadGateway(
            ConnectedCredentialStore(store, clock, settings.google_ads_login_customer_id),
            store,
            google_executable=settings.google_native_mcp_executable,
            google_project=settings.google_native_mcp_project,
            meta_enabled=settings.meta_native_mcp_enabled,
        ),
    )


# --- end lane: oauth-connect (cableado) ---


def _build_caps_state_store(settings: BrokerSettings, caps: CapsConfig) -> CapsStateStore | None:
    """`None` cuando `caps.yaml` no declara `panel_managed`: el panel no
    puede fijar nada, no hace falta directorio de estado y la resolucion
    para escritura queda EXACTAMENTE la de hoy (i1). Con el sobre
    declarado, el directorio es obligatorio y se comprueban modo (0700) y
    propietario al arrancar -- si no cuadran, el broker no arranca y nombra
    la variable, nunca un tope permisivo por omision."""
    if caps.panel_managed is None:
        return None
    if settings.caps_state_dir is None:
        raise CapsStateUnwritableError(
            "config/caps.yaml declara panel_managed: hace falta ADS_BROKER_CAPS_STATE_DIR"
        )
    assert_state_directory_is_private(settings.caps_state_dir)
    store = CapsStateStore(settings.caps_state_dir)
    logger.info(
        "ads_broker_caps_state_loaded",
        available=store.snapshot().available,
        panel_accounts=store.snapshot().accounts_count,
    )
    return store


async def run(settings: BrokerSettings | None = None) -> None:
    configure_logging()
    start_metrics_server(_METRICS_PORT)
    resolved_settings = settings or BrokerSettings()  # type: ignore[call-arg]
    allowed_uids = frozenset(resolved_settings.allowed_uids)
    socket_path = resolved_settings.broker_socket_path

    snapshot = load_caps_snapshot(resolved_settings.hard_caps_file)
    caps = snapshot.caps
    logger.info(
        "ads_broker_caps_loaded",
        accounts_configured=len(caps.accounts),
        panel_managed=caps.panel_managed is not None,
    )
    caps_state = _build_caps_state_store(resolved_settings, caps)
    effective_caps = EffectiveCapsResolver(caps, caps_state)
    # Sin sobre declarado la tuberia de escritura sigue recibiendo el
    # `CapsConfig` de siempre: misma clase, mismo camino, i1 intacta.
    caps_resolver: CapsResolverPort = caps if caps_state is None else effective_caps
    hard_caps = HardCapsService(
        effective_caps,
        snapshot.status,
        writing=(
            None
            if caps_state is None or caps.panel_managed is None
            else PanelCapsWriting(caps.panel_managed, caps_state)
        ),
    )

    store = EncryptedCredentialStore(
        resolved_settings.credential_store_dir,
        resolved_settings.credential_master_key.get_secret_value(),
    )
    lease = (
        ComposioLeaseStore(
            master_key_b64=resolved_settings.credential_master_key.get_secret_value(),
            issuer_public_key=resolved_settings.sso_public_key or "",
            metadata_path=resolved_settings.credential_store_dir / "composio-lease-state.sqlite3",
        )
        if resolved_settings.companion_mode
        else None
    )
    registry = await _build_registry(resolved_settings, caps_resolver, store, lease)
    runtime = _build_runtime(
        resolved_settings,
        registry,
        store,
        composio_lease=lease,
        hard_caps_status=snapshot.status,
        hard_caps=hard_caps,
    )
    server = await serve(socket_path, runtime, allowed_uids, self_uid=os.getuid())
    _harden_socket_permissions(socket_path)
    logger.info(
        "ads_broker_listening",
        socket_path=str(socket_path),
        platforms=sorted(platform.value for platform in registry.adapters),
    )

    async with server:
        await server.serve_forever()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
