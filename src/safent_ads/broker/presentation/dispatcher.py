"""Enruta la peticion ya tipada hacia el adaptador de plataforma o el
flujo OAuth correcto y serializa el resultado. Cualquier `op` desconocida
no tiene entrada en `_HANDLERS`: cae en `DENIED` por ausencia de esquema.
`execute_write` si tiene esquema (F2): el handler es tan plano como los de
lectura -- arma el `WriteIntent`/`SignedAuthorization` tipados y se los
pasa al adaptador, que es quien aplica los 8 controles de
contracts/platform-port.md y nunca lanza por una denegacion (siempre
devuelve un `WriteOutcome`, ok:true con el veredicto que sea). `upload_asset`
(M-3, revision de seguridad 0.2.22) manda los bytes del activo en base64
DENTRO de la peticion -- al reves que `render_image`, que los devuelve en
la respuesta -- por eso es la otra `op` con techo de trama ampliado en la
LECTURA (`socket_server.py::_EXTENDED_FRAME_OPS`)."""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Any, NoReturn, TypeVar, cast

import structlog
from pydantic import BaseModel, ValidationError

from safent_ads.accounts.application.errors import EntityNotFoundError
from safent_ads.accounts.application.native_ads import NativeAdsReadPort
from safent_ads.accounts.application.ports import (
    AccountRef,
    AssetUploadRequest,
    DateWindow,
    MetricsRequest,
    SignedAuthorization,
    WriteIntent,
)
from safent_ads.accounts.domain.refs import CredentialRefId, IdempotencyKey
from safent_ads.broker.application.app_credentials_service import AppCredentialsService
from safent_ads.broker.application.connection_scope import (
    ConnectionScope,
    connection_scope,
    current_connection_scope,
)
from safent_ads.broker.application.errors import (
    AppCredentialsIncompleteError,
    AppCredentialsNotConfiguredError,
    CredentialNotFoundError,
    EnvelopeAccountsExhaustedError,
    EnvelopeChangesExhaustedError,
    EnvelopeExceededError,
    EnvelopeNotDeclaredError,
    GoogleAccountAccessDeniedError,
    GoogleAccountNotEnabledError,
    GoogleAccountSelectionRequiredError,
    GoogleProjectAccessDeniedError,
    InvalidAccountCapsError,
    OAuthProviderDeniedError,
    OAuthSessionExpiredError,
    OAuthSessionNotFoundError,
)
from safent_ads.broker.application.oauth_connect_flow import OAuthConnectFlow
from safent_ads.broker.application.render_image import (
    ImageRendererNotConfiguredError,
    ImageRenderQuotaExceededError,
    ImageRenderTimeoutError,
    RenderCostCapExceededError,
    RenderImageService,
    RenderQuotaExceededError,
)
from safent_ads.broker.domain.meta_graph_policy import (
    GraphPolicyDeniedError,
    truncate_graph_response,
    validate_graph_edge,
    validate_graph_fields,
    validate_graph_node_shape,
    validate_graph_params,
)
from safent_ads.broker.infrastructure.adapter_registry import PlatformAdapterRegistry
from safent_ads.broker.infrastructure.caps_config import HardCapsStatus
from safent_ads.broker.infrastructure.caps_state import CapsStateUnwritableError
from safent_ads.broker.infrastructure.composio_lease import ComposioLeaseError, ComposioLeaseStore
from safent_ads.broker.infrastructure.hard_caps_service import HardCapsService, PanelCapsRequest
from safent_ads.broker.platforms.errors import (
    CredentialNotConnectedError,
    GaqlValidationError,
    PlatformCapabilityNotImplementedError,
)
from safent_ads.broker.platforms.gaql_validator import validate_gaql
from safent_ads.broker.platforms.google_ads_adapter import GoogleAdsAdapter
from safent_ads.broker.platforms.meta_ad_library import MetaAdLibraryIdentityRequiredError
from safent_ads.broker.platforms.meta_ads_adapter import MetaAdsAdapter
from safent_ads.broker.platforms.native_mcp_policy import NativeMcpDeniedError
from safent_ads.broker.platforms.write_pipeline import PackageUploadDeniedError
from safent_ads.broker.presentation.request_schemas import (
    ComposioChannelRequest,
    ComposioLeaseRequest,
    CredentialStatusRequest,
    DeleteAccountCapsRequest,
    DeletePlatformAppCredentialsRequest,
    ExecuteWriteRequest,
    FetchAccountInventoryRequest,
    FetchMetricsRequest,
    GetPlatformAppStatusRequest,
    GoogleReferenceReadRequest,
    GoogleTagManagerReadRequest,
    HardCapsStatusRequest,
    MetaAdsArchiveRequest,
    MetaGraphGetRequest,
    MetaReferenceReadRequest,
    NativeAdsReadRequest,
    OAuthBeginRequest,
    OAuthCompleteRequest,
    ReadEntityStateRequest,
    ReadWriteReceiptRequest,
    RegisterMetaSystemUserTokenRequest,
    RenderImageRequest,
    ResolveAccountCapsRequest,
    RevokeCredentialRequest,
    RunGaqlRequest,
    SetAccountCapsRequest,
    SetPlatformAppCredentialsRequest,
    UploadAssetRequest,
)
from safent_ads.broker.presentation.serializers import (
    serialize_ad_entity_snapshot,
    serialize_app_credentials_status,
    serialize_caps_view,
    serialize_credential_status,
    serialize_entity_state_snapshot,
    serialize_gaql_rows,
    serialize_metric_fact_snapshot,
    serialize_oauth_begin,
    serialize_oauth_complete,
    serialize_platform_asset_handle,
    serialize_rendered_image,
    serialize_write_outcome,
)
from safent_ads.creative.domain.brand_kit import BrandKit, SafeArea
from safent_ads.creative.domain.enums import Format, RendererName
from safent_ads.creative.domain.identifiers import AssetId
from safent_ads.creative.domain.render_specs import ImageSpec
from safent_ads.shared.clock import Clock, SystemClock
from safent_ads.shared.ids import EntityRef, PlatformCode
from safent_ads.shared.managed_ads import binding_from_json

logger = structlog.get_logger(__name__)

_RequestT = TypeVar("_RequestT", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class BrokerRuntime:
    """Todo lo que un handler del socket puede necesitar: los adaptadores
    de plataforma (US1) y el flujo de conexion OAuth (US3). Un unico
    parametro en vez de dos porque `_HANDLERS` es homogeneo — anadir un
    tercer contexto en el futuro no cambia la firma de cada handler."""

    adapters: PlatformAdapterRegistry
    oauth_flow: OAuthConnectFlow
    app_credentials: AppCredentialsService
    native_ads: NativeAdsReadPort | None = None
    composio_lease: ComposioLeaseStore | None = None
    hard_caps_status: HardCapsStatus | None = None
    # `None` cuando `config/caps.yaml` no declara `panel_managed`: los tres
    # comandos de topes responden entonces ENVELOPE_NOT_DECLARED y el
    # despliegue se comporta exactamente como antes de spec 008.
    hard_caps: HardCapsService | None = None
    render_image_service: RenderImageService | None = None
    clock: Clock = field(default_factory=SystemClock)


_Handler = Callable[[_RequestT, BrokerRuntime], Awaitable[Any]]

# Excepciones de `application` con desenlace esperado (state caducado, token
# rechazado por el proveedor...): `ads-api` necesita distinguirlas de un
# fallo generico de adaptador para traducirlas a la respuesta HTTP correcta
# (contracts/rest-api.md §Conexiones). Cualquier otra excepcion sigue
# cayendo en el `except Exception` de mas abajo — nunca fail-open.
_KNOWN_ERROR_CODES: dict[type[Exception], str] = {
    ComposioLeaseError: "COMPOSIO_LEASE_DENIED",
    NativeMcpDeniedError: "NATIVE_MCP_UNAVAILABLE",
    OAuthSessionNotFoundError: "OAUTH_SESSION_NOT_FOUND",
    OAuthSessionExpiredError: "OAUTH_SESSION_EXPIRED",
    OAuthProviderDeniedError: "OAUTH_PROVIDER_DENIED",
    GoogleAccountAccessDeniedError: "GOOGLE_ACCOUNT_ACCESS_DENIED",
    GoogleAccountNotEnabledError: "GOOGLE_ACCOUNT_NOT_ENABLED",
    GoogleAccountSelectionRequiredError: "GOOGLE_ACCOUNT_SELECTION_REQUIRED",
    GoogleProjectAccessDeniedError: "GOOGLE_PROJECT_ACCESS_LEVEL_TEST",
    CredentialNotFoundError: "CREDENTIAL_NOT_FOUND",
    GaqlValidationError: "GAQL_VALIDATION_FAILED",
    AppCredentialsNotConfiguredError: "PLATFORM_APP_NOT_CONFIGURED",
    AppCredentialsIncompleteError: "APP_CREDENTIALS_INCOMPLETE",
    PlatformCapabilityNotImplementedError: "PLATFORM_CAPABILITY_NOT_IMPLEMENTED",
    # R5 (historia 19, S-1): nodo inexistente y nodo ajeno dan el MISMO
    # codigo -- `broker_graph_passthrough_port.py` (ads-api) lo espera
    # literal para traducirlo a `EntityNotFoundError` de dominio.
    EntityNotFoundError: "ENTITY_NOT_FOUND",
    # B-3 (TB-4): el bróker vuelve a aplicar la politica de `get_meta_graph`
    # por su cuenta, nunca confia en que ya se aplico ads-api arriba.
    GraphPolicyDeniedError: "GRAPH_POLICY_DENIED",
    # H-follow-up (revision de codigo 2026-09-15): `CredentialNotConnectedError`
    # (broker/platforms/errors.py) escapaba sin traducir por `run_gaql`/
    # `meta_reference_read`/`google_reference_read` -- `ads-api` la veia como
    # `FAILED`/`adapter_error` generico en vez de este codigo, que
    # `mcp.application.errors.broker_denial_error` ya sabe traducir a
    # `CredentialNotConnectedError` tipado (mismo incidente de produccion que
    # `PLATFORM_APP_NOT_CONFIGURED`, companion 0.2.21).
    CredentialNotConnectedError: "CREDENTIAL_NOT_CONNECTED",
    # render_image (lane 003): igual criterio que `AppCredentialsNotConfiguredError`
    # arriba -- "sin clave de proveedor configurada" es una denegacion
    # esperada, no un fallo de adaptador que merezca WARNING.
    ImageRendererNotConfiguredError: "IMAGE_RENDERER_NOT_CONFIGURED",
    ImageRenderQuotaExceededError: "RATE_LIMITED",
    ImageRenderTimeoutError: "PLATFORM_UNAVAILABLE",
    # M-2 (revision de seguridad 0.2.22): codigos propios, distintos de
    # "RATE_LIMITED" -- el caller (`GenerateCreativeAssets`/MCP) necesita
    # distinguir "este negocio agoto SU cuota" y "el precio de lista supera
    # el tope" de un rate-limit generico, para poder mostrar un mensaje
    # accionable en vez de "intentalo mas tarde".
    RenderQuotaExceededError: "RENDER_QUOTA_EXCEEDED",
    RenderCostCapExceededError: "RENDER_COST_CAP",
    # fix/ad-library-identity-reason: Meta's own documented response (400,
    # `OAuthException/10`) on `ads_archive` when the Facebook user behind
    # the token has not completed the Ad Library identity/location
    # confirmation -- a denial worth naming, not the generic `FAILED`/
    # `adapter_error` every other `MetaAdLibraryError` still falls into.
    MetaAdLibraryIdentityRequiredError: "META_AD_LIBRARY_IDENTITY_REQUIRED",
    # spec 008 T030: denegaciones esperadas de los tres comandos de topes.
    # `ads-api` las traduce a 409/400/503 en la superficie REST; ninguna es
    # un fallo de adaptador ni merece WARNING generico.
    EnvelopeNotDeclaredError: "ENVELOPE_NOT_DECLARED",
    EnvelopeExceededError: "ENVELOPE_EXCEEDED",
    EnvelopeAccountsExhaustedError: "ENVELOPE_ACCOUNTS_EXHAUSTED",
    EnvelopeChangesExhaustedError: "ENVELOPE_CHANGES_EXHAUSTED",
    InvalidAccountCapsError: "INVALID_CAPS",
    CapsStateUnwritableError: "CAPS_STATE_UNWRITABLE",
}


async def _handle_hard_caps_status(_request: HardCapsStatusRequest, runtime: BrokerRuntime) -> Any:
    """`caps_digest` y `accounts_count` conservan su significado exacto
    (los bytes de `config/caps.yaml`, decision D14). `panel_state_digest` y
    `panel_accounts_count` son campos NUEVOS y aditivos, presentes solo
    cuando el despliegue declara un sobre: mezclarlos en el digest romperia
    a quien hoy lo compara con el fichero del host."""
    status = runtime.hard_caps_status
    if status is None:
        raise ValueError("hard_caps_status_unavailable")
    if runtime.hard_caps is None:
        return {
            "caps_digest": status.caps_digest,
            "accounts_count": status.accounts_count,
            "panel_state_digest": None,
            "panel_accounts_count": 0,
        }
    return runtime.hard_caps.status()


def _hard_caps(runtime: BrokerRuntime) -> HardCapsService:
    if runtime.hard_caps is None:
        raise EnvelopeNotDeclaredError("config/caps.yaml no declara panel_managed")
    return runtime.hard_caps


async def _handle_set_account_caps(request: SetAccountCapsRequest, runtime: BrokerRuntime) -> Any:
    view = await _hard_caps(runtime).set_account_caps(
        request.platform_account_id,
        PanelCapsRequest(
            daily_cap_minor=request.caps.daily_cap_minor,
            monthly_cap_minor=request.caps.monthly_cap_minor,
            ceiling_minor=request.caps.ceiling_minor,
            currency=request.caps.currency,
        ),
        requested_by=request.requested_by,
        request_id=request.request_id,
    )
    return serialize_caps_view(view)


async def _handle_delete_account_caps(
    request: DeleteAccountCapsRequest, runtime: BrokerRuntime
) -> Any:
    view = await _hard_caps(runtime).delete_account_caps(
        request.platform_account_id,
        requested_by=request.requested_by,
        request_id=request.request_id,
    )
    return serialize_caps_view(view)


async def _handle_resolve_account_caps(
    request: ResolveAccountCapsRequest, runtime: BrokerRuntime
) -> Any:
    """La UNICA lectura de topes que `ads-api` tiene: no lee el fichero ni
    el directorio de estado, los pide. Responde tambien sin sobre (con
    `envelope: null`), para que el panel pueda decir "el sobre no esta
    declarado" en vez de quedarse sin pantalla."""
    return serialize_caps_view(_hard_caps(runtime).view(request.platform_account_id))


async def _handle_composio_channel(_request: ComposioChannelRequest, runtime: BrokerRuntime) -> Any:
    if runtime.composio_lease is None:
        raise ComposioLeaseError("composio_channel_unavailable")
    return runtime.composio_lease.channel()


async def _handle_composio_lease(request: ComposioLeaseRequest, runtime: BrokerRuntime) -> Any:
    if runtime.composio_lease is None:
        raise ComposioLeaseError("composio_channel_unavailable")
    runtime.composio_lease.accept(request.envelope)
    return {"accepted": True}


async def _handle_fetch_account_inventory(
    request: FetchAccountInventoryRequest, runtime: BrokerRuntime
) -> Any:  # noqa: ANN401 - forma JSON heterogenea, tipada por el serializador
    account_ref = _request_account(request)
    adapter = runtime.adapters.get(request.platform)
    snapshots = await adapter.fetch_account_inventory(account_ref)
    return [
        serialize_ad_entity_snapshot(
            replace(s, entity_ref=_scoped_ref(s.entity_ref), parent_ref=_scoped_ref(s.parent_ref))
        )
        for s in snapshots
    ]


async def _handle_fetch_metrics(request: FetchMetricsRequest, runtime: BrokerRuntime) -> Any:  # noqa: ANN401
    account_ref = _request_account(request)
    adapter = runtime.adapters.get(request.platform)
    entity_refs = (
        [EntityRef.parse(raw) for raw in request.entity_refs] if request.entity_refs else None
    )
    metrics_request = MetricsRequest(
        account_ref=account_ref,
        window=DateWindow(start=request.window_start, end=request.window_end),
        granularity=request.granularity,
        entity_refs=entity_refs,
    )
    facts = await adapter.fetch_metrics(metrics_request)
    return [
        serialize_metric_fact_snapshot(replace(f, entity_ref=_scoped_ref(f.entity_ref)))
        for f in facts
    ]


async def _handle_read_entity_state(request: ReadEntityStateRequest, runtime: BrokerRuntime) -> Any:  # noqa: ANN401
    entity_ref = EntityRef.parse(request.entity_ref)
    adapter = runtime.adapters.get(entity_ref.platform)
    snapshot = await adapter.read_entity_state(entity_ref)
    return serialize_entity_state_snapshot(snapshot)


async def _handle_execute_write(
    request: ExecuteWriteRequest | ReadWriteReceiptRequest, runtime: BrokerRuntime
) -> Any:  # noqa: ANN401
    entity_ref = EntityRef.parse(request.entity_ref)
    adapter = runtime.adapters.get(entity_ref.platform)
    intent = WriteIntent(
        entity_ref=entity_ref,
        operation=request.operation,
        parametro=request.parametro,
        valor_actual=request.valor_actual,
        valor_propuesto=request.valor_propuesto,
        diff_hash=request.diff_hash,
        expected_state_hash=request.expected_state_hash,
        business_id=request.business_id,
        managed_binding=binding_from_json(request.managed_binding),
        package_binding=request.package_binding,
    )
    authorization = SignedAuthorization(
        authorization_id=request.authorization.authorization_id,
        proposal_id=request.authorization.proposal_id,
        kind=request.authorization.kind,
        diff_hash=request.authorization.diff_hash,
        guardrail_verdict_hash=request.authorization.guardrail_verdict_hash,
        issued_by=request.authorization.issued_by,
        expires_at=request.authorization.expires_at,
        signature=request.authorization.signature,
        managed_binding=binding_from_json(request.authorization.managed_binding),
        package_approval=request.authorization.package_approval,
    )
    if request.op == "read_write_receipt":
        outcome = await adapter.read_write_receipt(
            intent, authorization, IdempotencyKey(request.idempotency_key)
        )
        if outcome is None:
            return None
    else:
        outcome = await adapter.execute_write(
            intent, authorization, IdempotencyKey(request.idempotency_key)
        )
    return serialize_write_outcome(outcome)


async def _handle_run_gaql(request: RunGaqlRequest, runtime: BrokerRuntime) -> Any:  # noqa: ANN401
    """La validacion corre ANTES de resolver el adaptador (contracts/
    platform-port.md, threat-model.md T-1): una consulta rechazada nunca
    toca ningun SDK, ni siquiera el de una plataforma real conectada."""
    validate_gaql(request.query)
    account_ref = _request_account(request)
    adapter = runtime.adapters.get(request.platform)
    rows = await adapter.run_gaql(account_ref, request.query, max_rows=request.max_rows)
    return serialize_gaql_rows(rows)


async def _handle_native_ads_read(request: NativeAdsReadRequest, runtime: BrokerRuntime) -> Any:
    if runtime.native_ads is None:
        raise NativeMcpDeniedError("native_mcp_not_configured")
    account = _request_account(request)
    if request.tool is None:
        return await runtime.native_ads.list_native_tools(account)
    return await runtime.native_ads.read_native_tool(account, request.tool, request.arguments)


async def _handle_platform_reference_read(
    request: MetaReferenceReadRequest | GoogleReferenceReadRequest, runtime: BrokerRuntime
) -> Any:
    """004 tasks-2.md R3/R4/I1 (ops `meta_reference_read`/`google_reference_read`):
    el `platform` del esquema ya fija que adaptador concreto responde --
    `cast` en vez de `isinstance` porque `composition/broker.py` solo
    registra `MetaAdsAdapter`/`GoogleAdsAdapter` bajo su `PlatformCode`
    (mismo criterio que el resto de handlers, que confian en
    `PlatformAdapterRegistry` sin volver a comprobar el tipo)."""
    account_ref = _request_account(request)
    adapter = cast("MetaAdsAdapter | GoogleAdsAdapter", runtime.adapters.get(request.platform))
    rows = await adapter.read_reference_data(
        account_ref, tool=request.tool, arguments=request.arguments
    )
    return {"rows": list(rows)}


async def _handle_google_tag_manager_read(
    request: GoogleTagManagerReadRequest, runtime: BrokerRuntime
) -> Any:  # noqa: ANN401 - GTM resources have heterogeneous JSON shapes
    account_ref = _request_account(request)
    adapter = cast(GoogleAdsAdapter, runtime.adapters.get(PlatformCode.GOOGLE))
    return await adapter.read_google_tag_manager(
        account_ref,
        resource=request.resource,
        parent_path=request.parent_path,
    )


async def _handle_meta_graph_get(request: MetaGraphGetRequest, runtime: BrokerRuntime) -> Any:
    """B-3 (TB-4): la politica de `get_meta_graph` (`meta_graph_policy.py`)
    se reaplica AQUI, antes de tocar el adaptador -- el bróker nunca confia
    en que ads-api ya la aplico. A-1: recorte tambien en el bróker, no solo
    en el cliente MCP -- una unica pagina, nunca mas de 200 filas/64 KiB."""
    validate_graph_edge(request.edge)
    validate_graph_fields(request.edge, request.fields)
    validate_graph_node_shape(request.node)
    validate_graph_params(request.params, today=runtime.clock.now().date())
    account_ref = _request_account(request)
    adapter = cast(MetaAdsAdapter, runtime.adapters.get(request.platform))
    rows = await adapter.get_meta_graph(
        account_ref,
        node=request.node,
        edge=request.edge,
        fields=tuple(request.fields),
        params=request.params,
    )
    capped = truncate_graph_response(rows)
    return {"rows": list(capped.rows)}


async def _handle_meta_ads_archive(request: MetaAdsArchiveRequest, runtime: BrokerRuntime) -> Any:
    """R7: la Biblioteca de Anuncios no cuelga de una cuenta conectada
    (sin `platform` en el esquema) -- siempre Meta, la unica plataforma con
    esta API oficial. `external_account_id` (fix/ad-library-over-composio)
    solo autentica el proxy de Composio; `connection_scope` ya quedo fijado
    por `_dispatch` (`_request_scope`, mas abajo) a partir de
    `request.connection_id`, antes de que este handler se ejecute."""
    adapter = cast(MetaAdsAdapter, runtime.adapters.get(PlatformCode.META))
    ads = await adapter.search_ads_archive(
        country=request.country,
        search_terms=request.search_terms,
        search_page_ids=request.search_page_ids,
        active_only=request.active_status == "ACTIVE",
        external_account_id=request.external_account_id,
    )
    return {"ads": list(ads)}


async def _handle_oauth_begin(request: OAuthBeginRequest, runtime: BrokerRuntime) -> Any:  # noqa: ANN401
    result = await runtime.oauth_flow.begin(
        provider=request.provider,
        business_id=request.business_id,
        redirect_uri=request.redirect_uri,
        owner_id=request.owner_id,
        google_customer_id=request.google_customer_id,
    )
    return serialize_oauth_begin(result)


async def _handle_oauth_complete(request: OAuthCompleteRequest, runtime: BrokerRuntime) -> Any:  # noqa: ANN401
    result = await runtime.oauth_flow.complete(state=request.state, code=request.code)
    return serialize_oauth_complete(result)


async def _handle_credential_status(
    request: CredentialStatusRequest, runtime: BrokerRuntime
) -> Any:  # noqa: ANN401
    credential_ref_id = CredentialRefId(uuid.UUID(request.credential_ref_id))
    result = await runtime.oauth_flow.status(credential_ref_id)
    return serialize_credential_status(result)


async def _handle_revoke_credential(
    request: RevokeCredentialRequest, runtime: BrokerRuntime
) -> Any:  # noqa: ANN401
    credential_ref_id = CredentialRefId(uuid.UUID(request.credential_ref_id))
    await runtime.oauth_flow.revoke(credential_ref_id)
    return {"revoked": True}


async def _handle_register_meta_system_user_token(
    request: RegisterMetaSystemUserTokenRequest, runtime: BrokerRuntime
) -> Any:  # noqa: ANN401
    result = await runtime.oauth_flow.register_meta_system_user_token(
        token=request.token,
        business_id=request.business_id,
        owner_id=request.owner_id,
    )
    return serialize_oauth_complete(result)


async def _handle_set_platform_app_credentials(
    request: SetPlatformAppCredentialsRequest, runtime: BrokerRuntime
) -> Any:  # noqa: ANN401
    if request.platform == PlatformCode.GOOGLE:
        status = runtime.app_credentials.set_google(
            client_id=request.client_id or "",
            client_type=request.client_type,
            client_secret=request.client_secret or "",
            login_customer_id=request.login_customer_id,
        )
    else:
        status = runtime.app_credentials.set_meta(
            app_id=request.app_id or "", app_secret=request.app_secret or ""
        )
    return serialize_app_credentials_status(status)


async def _handle_get_platform_app_status(
    request: GetPlatformAppStatusRequest, runtime: BrokerRuntime
) -> Any:  # noqa: ANN401
    return serialize_app_credentials_status(runtime.app_credentials.status(request.platform))


async def _handle_delete_platform_app_credentials(
    request: DeletePlatformAppCredentialsRequest, runtime: BrokerRuntime
) -> Any:  # noqa: ANN401
    runtime.app_credentials.delete(request.platform)
    return {"deleted": True}


def _renderer_name_from_request(raw: str) -> RendererName:
    """Un `renderer` bien formado pero desconocido (`RendererName(raw)`
    lanza `ValueError`) es, desde `ads-api`, exactamente lo mismo que uno
    sin adaptador inyectado: la cascada de `GenerateCreativeAssets` prueba
    el siguiente candidato en los dos casos -- un solo tipo de error para
    los dos, nunca un `FAILED` generico distinto sin motivo."""
    try:
        return RendererName(raw)
    except ValueError as exc:
        raise ImageRendererNotConfiguredError(raw) from exc


def _image_spec_from_request(request: RenderImageRequest) -> ImageSpec:
    fields = request.brand_kit
    brand_kit = BrandKit(
        primary_font=fields.primary_font,
        secondary_font=fields.secondary_font,
        primary_color_hex=fields.primary_color_hex,
        secondary_color_hex=fields.secondary_color_hex,
        logo_asset_id=AssetId.parse(fields.logo_asset_id),
        safe_area=SafeArea(
            top=fields.safe_area_top,
            bottom=fields.safe_area_bottom,
            left=fields.safe_area_left,
            right=fields.safe_area_right,
        ),
    )
    return ImageSpec(
        prompt=request.prompt,
        # Ver el docstring de `RenderImageRequest`: ningun llamante manda
        # activos de referencia todavia.
        reference_assets=(),
        format=Format(request.format),
        seed=request.seed,
        brand_kit=brand_kit,
    )


async def _handle_render_image(request: RenderImageRequest, runtime: BrokerRuntime) -> Any:  # noqa: ANN401
    """threat-model.md C-29: nunca se loguea `request.prompt` ni ninguna
    clave -- solo `serialize_rendered_image` decide que cruza al log
    (renderizador y coste), y eso ocurre en la capa de aplicacion, no aqui."""
    if runtime.render_image_service is None:
        raise ImageRendererNotConfiguredError(request.renderer)
    result = await runtime.render_image_service.render(
        renderer=_renderer_name_from_request(request.renderer),
        spec=_image_spec_from_request(request),
        business_id=request.business_id,
    )
    return serialize_rendered_image(result)


async def _handle_upload_asset(request: UploadAssetRequest, runtime: BrokerRuntime) -> Any:  # noqa: ANN401
    """M-3 (revision de seguridad 0.2.22): `media_base64` viaja EN LA
    PETICION (al reves que `render_image`) -- `socket_server.py` la deja
    crecer hasta 16 MiB solo para esta `op`, sondeando el prefijo del
    sobre antes de leer el resto. H1 (revision de seguridad 0.2.23):
    `width`/`height`/`package_binding`/`package_approval` viajan tal cual al
    adaptador -- la admision R1-R6 mas el cotejo de checksum ocurre dentro de
    `AdsPlatformPort.upload_asset` (`WriteAuthorizationPipeline.admit_upload`
    para `MetaAdsAdapter`), nunca aqui: un `PackageUploadDeniedError` que
    escape de esa llamada lo traduce `_dispatch`, igual que cualquier otra
    denegacion del bróker."""
    account_ref = _request_account(request)
    adapter = runtime.adapters.get(request.platform)
    handle = await adapter.upload_asset(
        AssetUploadRequest(
            account_ref=account_ref,
            file_name=request.file_name,
            mime_type=request.mime_type,
            media=base64.b64decode(request.media_base64),
            width=request.width,
            height=request.height,
            package_binding=request.package_binding,
            package_approval=request.package_approval,
        )
    )
    return serialize_platform_asset_handle(handle)


_HANDLERS: dict[str, tuple[type[BaseModel], _Handler[Any]]] = {
    "composio_channel": (ComposioChannelRequest, _handle_composio_channel),
    "composio_lease": (ComposioLeaseRequest, _handle_composio_lease),
    "get_hard_caps_status": (HardCapsStatusRequest, _handle_hard_caps_status),
    "set_account_caps": (SetAccountCapsRequest, _handle_set_account_caps),
    "delete_account_caps": (DeleteAccountCapsRequest, _handle_delete_account_caps),
    "resolve_account_caps": (ResolveAccountCapsRequest, _handle_resolve_account_caps),
    "native_ads_read": (NativeAdsReadRequest, _handle_native_ads_read),
    "fetch_account_inventory": (FetchAccountInventoryRequest, _handle_fetch_account_inventory),
    "fetch_metrics": (FetchMetricsRequest, _handle_fetch_metrics),
    "read_entity_state": (ReadEntityStateRequest, _handle_read_entity_state),
    "execute_write": (ExecuteWriteRequest, _handle_execute_write),
    "read_write_receipt": (ReadWriteReceiptRequest, _handle_execute_write),
    "run_gaql": (RunGaqlRequest, _handle_run_gaql),
    "meta_reference_read": (MetaReferenceReadRequest, _handle_platform_reference_read),
    "google_reference_read": (GoogleReferenceReadRequest, _handle_platform_reference_read),
    "google_tag_manager_read": (GoogleTagManagerReadRequest, _handle_google_tag_manager_read),
    "meta_graph_get": (MetaGraphGetRequest, _handle_meta_graph_get),
    "meta_ads_archive": (MetaAdsArchiveRequest, _handle_meta_ads_archive),
    "render_image": (RenderImageRequest, _handle_render_image),
    "upload_asset": (UploadAssetRequest, _handle_upload_asset),
    "oauth_begin": (OAuthBeginRequest, _handle_oauth_begin),
    "oauth_complete": (OAuthCompleteRequest, _handle_oauth_complete),
    "credential_status": (CredentialStatusRequest, _handle_credential_status),
    "revoke_credential": (RevokeCredentialRequest, _handle_revoke_credential),
    "register_meta_system_user_token": (
        RegisterMetaSystemUserTokenRequest,
        _handle_register_meta_system_user_token,
    ),
    "set_platform_app_credentials": (
        SetPlatformAppCredentialsRequest,
        _handle_set_platform_app_credentials,
    ),
    "get_platform_app_status": (GetPlatformAppStatusRequest, _handle_get_platform_app_status),
    "delete_platform_app_credentials": (
        DeletePlatformAppCredentialsRequest,
        _handle_delete_platform_app_credentials,
    ),
}


def _error_response(error_code: str, reason: str) -> bytes:
    return json.dumps({"ok": False, "error_code": error_code, "reason": reason}).encode("utf-8")


def _ok_response(result: Any) -> bytes:  # noqa: ANN401 - JSON heterogeneo ya serializado
    return json.dumps({"ok": True, "result": result}).encode("utf-8")


def _reject_json_constant(name: str) -> NoReturn:
    """`json.loads` acepta `NaN`, `Infinity` y `-Infinity` por defecto, y
    pydantic los toma por `float` validos. En un importe eso es un tope sin
    cota: `NaN` no es mayor que nada, asi que toda comparacion contra el
    sobre saldria falsa. Se rechaza la trama ENTERA en el parseo, antes de
    que ningun esquema la vea (spec 008 T028, x-invariant 7 de
    `contracts/broker-set-account-caps.schema.json`)."""
    raise ValueError(f"json_constant_not_allowed:{name}")


async def handle_payload(raw: bytes, runtime: BrokerRuntime) -> bytes:
    """Nunca fail-open (contracts/platform-port.md): cualquier fallo de
    parseo, esquema o adaptador cae en una respuesta `ok: false`."""
    try:
        payload = json.loads(raw, parse_constant=_reject_json_constant)
    except ValueError:
        # `json.JSONDecodeError` hereda de `ValueError`, igual que el
        # rechazo de `_reject_json_constant`: los dos son "no se pudo
        # parsear", nunca una ruta distinta.
        return _error_response("DENIED", "unparseable_request")

    if not isinstance(payload, dict):
        return _error_response("DENIED", "request_must_be_an_object")

    op = payload.get("op")
    if not isinstance(op, str):
        logger.info("broker_op_not_read_allowed", op=op)
        return _error_response("DENIED", "op_not_available_in_f1")

    handler_entry = _HANDLERS.get(op)
    if handler_entry is None:
        logger.info("broker_op_not_read_allowed", op=op)
        return _error_response("DENIED", "op_not_available_in_f1")

    model_cls, handler = handler_entry
    try:
        request = model_cls.model_validate(payload)
    except ValidationError:
        return _error_response("DENIED", "invalid_schema")

    return await _dispatch(op, request, handler, runtime)


async def _dispatch(
    op: str, request: BaseModel, handler: _Handler[Any], runtime: BrokerRuntime
) -> bytes:
    try:
        with connection_scope(_request_scope(request)):
            result = await handler(request, runtime)
    except PackageUploadDeniedError as exc:
        # H1 (revision de seguridad 0.2.23): a diferencia de `_KNOWN_ERROR_
        # CODES` (un `error_code` fijo por tipo de excepcion), esta denegacion
        # lleva su propio `WriteDenialCode.value` -- una de varias reglas
        # (R1-R6, checksum) pudo ser la que fallo.
        logger.info("broker_op_denied", op=op, error_code=exc.error_code)
        return _error_response(exc.error_code, "denied")
    except tuple(_KNOWN_ERROR_CODES) as exc:
        error_code = _KNOWN_ERROR_CODES[type(exc)]
        logger.info("broker_op_denied", op=op, error_code=error_code)
        return _error_response(error_code, "denied")
    except Exception as exc:  # noqa: BLE001 - frontera con el adaptador/proveedor, nunca fail-open
        # Class name only: the message may carry provider data. Without it a
        # transport allowlist miss was indistinguishable from a provider outage.
        logger.warning("broker_read_op_failed", op=op, error_type=type(exc).__name__)
        return _error_response("FAILED", "adapter_error")
    return _ok_response(result)


def _request_scope(request: BaseModel) -> ConnectionScope | None:
    business = getattr(request, "business_id", None)
    connection = getattr(request, "connection_id", None)
    raw_entity = getattr(request, "entity_ref", None)
    if raw_entity is not None:
        entity = EntityRef.parse(raw_entity)
        if entity.connection_id is not None:
            if business is not None and str(entity.business_id) != business:
                raise ValueError("connection_business_mismatch")
            business, connection = str(entity.business_id), str(entity.connection_id)
    if connection is None:
        return None  # Legacy objects cannot resolve a production credential.
    scope = ConnectionScope(uuid.UUID(business), uuid.UUID(connection))
    for raw in getattr(request, "entity_refs", None) or ():
        entity = EntityRef.parse(raw)
        if (entity.business_id, entity.connection_id) != (scope.business_id, scope.connection_id):
            raise ValueError("metric_connection_mismatch")
    return scope


def _request_account(request: Any) -> AccountRef:
    scope = current_connection_scope()
    return AccountRef(
        request.platform,
        request.external_account_id,
        scope.business_id if scope else None,
        scope.connection_id if scope else None,
    )


def _scoped_ref(entity: EntityRef) -> EntityRef:
    scope = current_connection_scope()
    return (
        replace(entity, business_id=scope.business_id, connection_id=scope.connection_id)
        if scope
        else entity
    )
