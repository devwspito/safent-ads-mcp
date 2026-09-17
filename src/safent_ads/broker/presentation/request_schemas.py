"""Esquema tipado de las peticiones del socket (contracts/platform-port.md
punto 2 de las comprobaciones del broker: "la peticion se valida contra el
esquema; campo desconocido o tipo inesperado ⇒ `DENIED`. Nada de
diccionarios sin contrato."). Las cuatro operaciones de lectura de US1
(`fetch_account_inventory`/`fetch_metrics`/`read_entity_state`/`run_gaql`),
las cinco de conexion OAuth (US3) y `execute_write` (F2) tienen esquema; un
campo ausente o de tipo inesperado en cualquiera de ellas cae en `DENIED`
por `invalid_schema`, antes de que el adaptador vea nada."""

from __future__ import annotations

from datetime import date, datetime
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from safent_ads.accounts.application.ports import MetricGranularity, WriteOperation
from safent_ads.accounts.domain.google_customer_id import normalize_google_customer_id
from safent_ads.broker.domain.account_key import MAX_PLATFORM_ACCOUNT_ID_LENGTH
from safent_ads.broker.infrastructure.caps_config import MAX_MINOR_AMOUNT
from safent_ads.shared.ids import PlatformCode
from safent_ads.shared.managed_ads import ManagedAdsBinding

# `accounts.domain.json_value.JsonValue` esta definido sobre `Sequence`/
# `Mapping` con `from __future__ import annotations`: pydantic no puede
# generar un schema para un alias recursivo asi (recursion infinita al
# resolver el forward-ref). El `type` nativo de Python 3.12 si funciona
# con pydantic -- misma forma JSON, solo para la validacion del wire.
type _WireJsonValue = (
    None | bool | int | float | str | list[_WireJsonValue] | dict[str, _WireJsonValue]
)

# Techo de saneado del propio esquema (un cliente no puede pedir un
# `max_rows` disparatado); el tope real lo aplica el adaptador con su
# propio `_max_rows_per_query` (`broker/platforms/google_ads_adapter.py`,
# `min(max_rows, self._max_rows_per_query)`) -- este valor solo evita que
# una peticion mal formada pase de aqui, no es la fuente de verdad.
_MAX_GAQL_ROWS: Final = 10_000
_MAX_PLATFORM_ACCOUNT_ID: Final = MAX_PLATFORM_ACCOUNT_ID_LENGTH

# `MetaAdsAdapter._MAX_CREATIVE_UPLOAD_BYTES` (8 MiB) mas el overhead de
# base64 (~4/3) mas margen: techo de SANEADO del sobre, no la fuente de
# verdad del limite -- el adaptador rechaza igual un payload dentro de este
# techo pero fuera del suyo propio.
_MAX_UPLOAD_MEDIA_BASE64_LENGTH: Final = 12_000_000


class HardCapsStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    op: Literal["get_hard_caps_status"]


class _PanelCapsBody(BaseModel):
    """Los TRES importes que el panel fija, y solo esos tres, mas la divisa
    de confirmacion. `extra="forbid"` mas `strict=True` es lo que hace que
    `floor_minor`, `max_step_pct`, `max_changes_per_day` y
    `autonomy_enabled` se rechacen RUIDOSAMENTE en vez de ignorarse en
    silencio (i14), y que `3.0`, `"3"` o `true` no pasen por un entero
    (i8). `NaN`/`Infinity` ya mueren antes, en `handle_payload`."""

    model_config = ConfigDict(extra="forbid", strict=True)

    daily_cap_minor: int = Field(ge=0, le=MAX_MINOR_AMOUNT)
    monthly_cap_minor: int = Field(ge=0, le=MAX_MINOR_AMOUNT)
    ceiling_minor: int = Field(ge=0, le=MAX_MINOR_AMOUNT)
    currency: str = Field(pattern=r"^[A-Z]{3}$")


class SetAccountCapsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    op: Literal["set_account_caps"]
    platform_account_id: str = Field(min_length=1, max_length=_MAX_PLATFORM_ACCOUNT_ID)
    caps: _PanelCapsBody
    # Identificador del dueno para el registro de decisiones. Nunca un
    # correo ni ningun otro dato personal.
    requested_by: str = Field(min_length=1, max_length=128)
    # Opaco (el nonce de confirmacion del panel), solo para correlacionar la
    # auditoria del broker con la del panel: `set`/`delete` son absolutos e
    # idempotentes por naturaleza, no hace falta clave de idempotencia.
    request_id: str | None = Field(default=None, max_length=64)


class DeleteAccountCapsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    op: Literal["delete_account_caps"]
    platform_account_id: str = Field(min_length=1, max_length=_MAX_PLATFORM_ACCOUNT_ID)
    requested_by: str = Field(min_length=1, max_length=128)
    request_id: str | None = Field(default=None, max_length=64)


class ResolveAccountCapsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    op: Literal["resolve_account_caps"]
    platform_account_id: str = Field(min_length=1, max_length=_MAX_PLATFORM_ACCOUNT_ID)


class ComposioChannelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["composio_channel"]


class ComposioLeaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["composio_lease"]
    envelope: str = Field(min_length=1, max_length=32768, repr=False)


class FetchAccountInventoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op: Literal["fetch_account_inventory"]
    platform: PlatformCode
    external_account_id: str
    business_id: str | None = None
    connection_id: str | None = None


class FetchMetricsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op: Literal["fetch_metrics"]
    platform: PlatformCode
    external_account_id: str
    business_id: str | None = None
    connection_id: str | None = None
    window_start: date
    window_end: date
    granularity: MetricGranularity
    entity_refs: list[str] | None = None


class ReadEntityStateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op: Literal["read_entity_state"]
    entity_ref: str


class UploadAssetRequest(BaseModel):
    """`upload_asset`: sube un activo real a la plataforma (Google/Meta) --
    los bytes viajan en base64 EN LA PETICION, al reves que `render_image`
    (bytes en la RESPUESTA). M-3 (revision de seguridad 0.2.22): la otra
    `op` con techo de trama ampliado en la lectura
    (`socket_server.py::_EXTENDED_FRAME_OPS`, `op` debe seguir siendo la
    PRIMERA clave del objeto para que la sonda de prefijo la reconozca).

    `width`/`height`/`package_binding`/`package_approval` (H1, revision de
    seguridad 0.2.23): obligatorios SOLO para el paso `UPLOAD_CREATIVE` de
    un paquete -- forma exacta de `PackageStepBinding.to_canonical()`/
    `PackageApprovalProof.as_claims()`, la misma que `package_binding`/
    `package_approval` de `ExecuteWriteRequest`/`SignedAuthorizationRequest`
    (`broker.domain.package_admission.admit_package_upload` los exige por
    clave). El `upload_creative_asset` independiente los deja fuera del
    payload -- nunca `null` -- y la subida sigue sin tocar ningun libro."""

    model_config = ConfigDict(extra="forbid")

    op: Literal["upload_asset"]
    platform: PlatformCode
    external_account_id: str
    file_name: str = Field(min_length=1, max_length=255)
    mime_type: str = Field(min_length=1, max_length=127)
    media_base64: str = Field(min_length=1)
    business_id: str | None = None
    connection_id: str | None = None
    width: int | None = Field(default=None, gt=0)
    height: int | None = Field(default=None, gt=0)
    package_binding: dict[str, _WireJsonValue] | None = None
    package_approval: dict[str, _WireJsonValue] | None = None


class RunGaqlRequest(BaseModel):
    """`broker/platforms/gaql_validator.py::validate_gaql` es quien decide
    si la consulta es una lectura segura (SELECT-only, sin DDL/DML, recurso
    en la lista blanca); este esquema solo fija la forma del sobre y un
    techo de saneado en `max_rows` (contracts/platform-port.md)."""

    model_config = ConfigDict(extra="forbid")

    op: Literal["run_gaql"]
    platform: PlatformCode
    external_account_id: str
    business_id: str | None = None
    connection_id: str | None = None
    query: str
    max_rows: int = Field(gt=0, le=_MAX_GAQL_ROWS)


class OAuthBeginRequest(BaseModel):
    """`accounts/infrastructure/oauth_broker_client.py::begin`. `redirect_uri`
    lo construye `ads-api` desde `ADS_PUBLIC_BASE_URL`, nunca el navegador —
    el broker no valida su forma mas alla de "no vacio", la valida el
    proveedor OAuth contra el redirect URI registrado en su consola."""

    model_config = ConfigDict(extra="forbid")

    op: Literal["oauth_begin"]
    provider: PlatformCode
    business_id: str
    redirect_uri: str
    owner_id: str | None = None
    google_customer_id: str | None = Field(default=None, max_length=32)

    @model_validator(mode="after")
    def _validate_google_customer_selection(self) -> OAuthBeginRequest:
        self.google_customer_id = normalize_google_customer_id(
            self.google_customer_id,
            provider=self.provider,
        )
        return self


class NativeAdsReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op: Literal["native_ads_read"]
    platform: PlatformCode
    external_account_id: str = Field(pattern=r"^(act_)?[0-9]+$")
    business_id: str | None = None
    connection_id: str | None = None
    tool: str | None = Field(default=None, pattern=r"^[a-z_]{1,80}$")
    arguments: dict[str, _WireJsonValue] = Field(default_factory=dict)


class PlatformReferenceReadRequest(BaseModel):
    """004 tasks-2.md R3/R4/I1: campos comunes de `meta_reference_read`/
    `google_reference_read` -- mismo sobre que `NativeAdsReadRequest`, un
    `tool`/`arguments` que el adaptador de la plataforma despacha."""

    model_config = ConfigDict(extra="forbid")

    platform: PlatformCode
    external_account_id: str
    business_id: str | None = None
    connection_id: str | None = None
    tool: str = Field(pattern=r"^[a-z_]{1,80}$")
    arguments: dict[str, _WireJsonValue] = Field(default_factory=dict)


class MetaReferenceReadRequest(PlatformReferenceReadRequest):
    op: Literal["meta_reference_read"]
    platform: Literal[PlatformCode.META]
    external_account_id: str = Field(pattern=r"^(act_)?[0-9]+$")


class GoogleReferenceReadRequest(PlatformReferenceReadRequest):
    op: Literal["google_reference_read"]
    platform: Literal[PlatformCode.GOOGLE]


class MetaGraphGetRequest(BaseModel):
    """R5 (historia 19): `get_meta_graph`, paso a traves de lectura de
    Meta. La lista blanca de aristas/campos/params vive en dominio puro
    (`mcp/domain/meta_graph_path.py`) y ya se aplico ANTES de llegar aqui
    (`BrokerGraphPassthroughPort`); este esquema solo fija la forma del
    sobre y topes de saneado (S-1: sin techo aqui, `truncate_response`
    corta la respuesta, no la peticion)."""

    model_config = ConfigDict(extra="forbid")

    op: Literal["meta_graph_get"]
    platform: Literal[PlatformCode.META]
    external_account_id: str = Field(pattern=r"^(act_)?[0-9]+$")
    business_id: str | None = None
    connection_id: str | None = None
    node: str = Field(pattern=r"^(act_)?[0-9]+$", max_length=32)
    edge: str = Field(default="", max_length=64)
    fields: list[str] = Field(default_factory=list, max_length=30)
    params: dict[str, _WireJsonValue] = Field(default_factory=dict, max_length=10)


class MetaAdsArchiveRequest(BaseModel):
    """R7 (historias 21-23): `search_competitor_ads` sobre la Biblioteca de
    Anuncios de Meta -- sin `platform` (siempre Meta, la unica plataforma
    con esta API oficial). El nodo `ads_archive` en si no cuelga de ninguna
    cuenta conectada (`broker/platforms/meta_ad_library.py`), pero
    `connection_id`/`external_account_id` SI viajan (fix/ad-library-over-
    composio): en modo Composio (companion, sin app nativa de Meta) el
    proxy necesita una cuenta ya conectada para autenticar la llamada --
    la del negocio que pregunta, resuelta por el llamante
    (`mcp/infrastructure/broker_competitor_research_port.py`), nunca por
    este esquema. El cliente nativo (app de Meta configurada) los ignora."""

    model_config = ConfigDict(extra="forbid")

    op: Literal["meta_ads_archive"]
    business_id: str | None = None
    connection_id: str | None = None
    external_account_id: str | None = Field(default=None, pattern=r"^(act_)?[0-9]+$")
    country: str = Field(pattern=r"^[A-Z]{2}$")
    search_terms: str | None = Field(default=None, max_length=200)
    search_page_ids: str | None = Field(default=None, max_length=200)
    active_status: Literal["ACTIVE", "ALL"]


class OAuthCompleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op: Literal["oauth_complete"]
    state: str
    code: str


class CredentialStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op: Literal["credential_status"]
    credential_ref_id: str


class RevokeCredentialRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op: Literal["revoke_credential"]
    credential_ref_id: str


class _ManagedContextRequest(BaseModel):
    managed_binding: dict[str, _WireJsonValue] | None = None

    @field_validator("managed_binding", mode="before")
    @classmethod
    def validate_binding(cls, value: object) -> object:
        if value is not None:
            ManagedAdsBinding.from_claims(value)
        return value


class SignedAuthorizationRequest(_ManagedContextRequest):
    """Forma exacta de `SignedAuthorization` sobre el wire
    (contracts/platform-port.md). `signature` viaja en hexadecimal, igual
    que `execution/infrastructure/broker_platform.py::_signed` la produce.
    `issued_by` viaja porque `broker.domain.write_authorization.
    authorization_signing_payload` lo incluye en el payload firmado -- un
    esquema que lo omitiera nunca podria reconstruir la misma firma.

    `package_approval` (003-paquete-de-campana contracts/api.md §R2.E,
    BL-2/BL-3): el sobre humano firmado completo, obligatorio si y solo si
    `kind == "package_step"` -- la forma exacta la exige
    `broker.domain.package_admission.admit_package_step` por clave, nunca
    por atributo de un tipo de `packages` (aciclicidad de contextos)."""

    model_config = ConfigDict(extra="forbid")

    authorization_id: str
    proposal_id: str
    kind: Literal["human_approval", "rule_authorization", "package_step"]
    diff_hash: str
    guardrail_verdict_hash: str
    issued_by: str
    expires_at: datetime
    # `003-paquete-de-campana` contracts/api.md §R2.E (BL-2/BL-3): forma
    # exacta de `SignedAuthorization.package_approval` -- obligatorio si y
    # solo si `kind == "package_step"` (`admit_package_step`, R1). Sin este
    # campo en el esquema, todo `execute_write` de un paso de publicacion
    # llegaba con `package_approval=None` sin importar lo que mandara el
    # cliente -- R1 lo denegaba siempre con `PACKAGE_BINDING_REQUIRED`.
    package_approval: dict[str, _WireJsonValue] | None = None
    signature: str


class WriteRequestFields(_ManagedContextRequest):
    """Forma exacta de `WriteIntent` mas la autorizacion firmada y la
    clave de idempotencia (contracts/platform-port.md `execute_write`).
    `operation` es el enum cerrado de `WriteOperation` -- una palanca que
    el puerto no declara ahi ni siquiera llega a validarse.

    `package_binding` (003-paquete-de-campana contracts/api.md §R2.E,
    BL-2/BL-3): la forma canonica de `PackageStepBinding.to_canonical()`,
    obligatoria si y solo si `authorization.kind == "package_step"` (R1)."""

    model_config = ConfigDict(extra="forbid")

    entity_ref: str
    operation: WriteOperation
    parametro: str
    valor_actual: _WireJsonValue
    valor_propuesto: _WireJsonValue
    diff_hash: str
    expected_state_hash: str
    authorization: SignedAuthorizationRequest
    idempotency_key: str
    business_id: str | None = None
    # `003-paquete-de-campana` contracts/api.md §R2.E (BL-2/BL-3): forma
    # canonica de `WriteIntent.package_binding` (`packages.domain.
    # step_binding.PackageStepBinding.to_canonical()`) -- mismo criterio
    # que `package_approval` de arriba, obligatorio solo para
    # `kind == "package_step"`.
    package_binding: dict[str, _WireJsonValue] | None = None


class ExecuteWriteRequest(WriteRequestFields):
    op: Literal["execute_write"]


class ReadWriteReceiptRequest(WriteRequestFields):
    op: Literal["read_write_receipt"]
    business_id: str = Field(min_length=1)


class RegisterMetaSystemUserTokenRequest(BaseModel):
    business_id: str | None = None
    owner_id: str | None = None
    """Via alternativa de Meta: pegar un System User token propio en vez de
    completar el OAuth (contracts/rest-api.md §Conexiones, `POST
    /platform-accounts/meta/system-user-token`)."""

    model_config = ConfigDict(extra="forbid")

    op: Literal["register_meta_system_user_token"]
    token: str


class SetPlatformAppCredentialsRequest(BaseModel):
    """Alta/reemplazo de credenciales de VENDOR (owner decision,
    app-credentials-ui): `platform` decide que subconjunto de campos es
    obligatorio (`AppCredentialsService.set_google`/`set_meta`) -- la forma
    ya la valido `accounts/presentation` (REST), esto solo fija el sobre
    del wire."""

    model_config = ConfigDict(extra="forbid")

    op: Literal["set_platform_app_credentials"]
    platform: PlatformCode
    client_id: str | None = None
    client_secret: str | None = None
    client_type: Literal["web", "desktop"] = "web"
    login_customer_id: str | None = None
    app_id: str | None = None
    app_secret: str | None = None


class GetPlatformAppStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op: Literal["get_platform_app_status"]
    platform: PlatformCode


class DeletePlatformAppCredentialsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op: Literal["delete_platform_app_credentials"]
    platform: PlatformCode


# --- lane 003 (paquete-de-campana): render_image, threat-model.md C-29
# ("claves cloud en el broker" -- `FAL_API_KEY`/`OPENAI_API_KEY` viven en
# `BrokerSettings`, nunca en `ads-api`). Reconstruye un `ImageSpec` con
# `reference_assets=()`: hoy ningun llamante de `GenerateCreativeAssets`
# manda activos de referencia (`creative/application/generate_creative_
# assets.py::_render_image`); llevarlos por el wire exige extender este
# esquema con bytes/claves de almacen, fuera de esta rama. ---

_RENDERER_NAME_PATTERN: Final = r"^[a-z0-9_]{1,64}$"
_IMAGE_FORMAT_PATTERN: Final = r"^\d{2,4}x\d{2,4}$"
_HEX_COLOR_PATTERN: Final = r"^#[0-9A-Fa-f]{6}$"
# Mismo tope que `creative.domain.render_specs._MAX_PROMPT_LEN`: ese
# modulo no expone la constante (privada a proposito, invariante de
# `ImageSpec`), asi que el esquema del wire fija su propio techo de
# saneado -- el mismo numero, duplicado a proposito en la frontera.
_MAX_IMAGE_PROMPT_LEN: Final = 2000


class ImageBrandKitFields(BaseModel):
    """Forma exacta de `creative.domain.brand_kit.BrandKit` + `SafeArea`
    sobre el wire: ninguno de los dos renderizadores de imagen usa la
    tipografia/colores/logo en `render()` hoy (solo `OpenAiImageRenderer`
    lee `safe_area` para recortar), pero se lleva la identidad completa en
    vez de solo `safe_area` para que un renderizador futuro (overlay de
    logo, por ejemplo) no dependa de extender este esquema."""

    model_config = ConfigDict(extra="forbid")

    primary_font: str = Field(min_length=1, max_length=100)
    secondary_font: str = Field(min_length=1, max_length=100)
    primary_color_hex: str = Field(pattern=_HEX_COLOR_PATTERN)
    secondary_color_hex: str = Field(pattern=_HEX_COLOR_PATTERN)
    logo_asset_id: str = Field(min_length=1, max_length=64)
    safe_area_top: float = Field(ge=0.0, le=0.5)
    safe_area_bottom: float = Field(ge=0.0, le=0.5)
    safe_area_left: float = Field(ge=0.0, le=0.5)
    safe_area_right: float = Field(ge=0.0, le=0.5)


class RenderImageRequest(BaseModel):
    """`render_image`: `renderer` fija cual de los `image_renderers`
    inyectados (`composition/creative_renderers.py::build_image_renderers`)
    ejecuta -- `ads-api` es quien decide el orden de la cascada
    (`RendererSelector`), este op solo ejecuta el candidato pedido, nunca
    elige uno el mismo (`broker/application/render_image.py`).

    `business_id` obligatorio (M-2, revision de seguridad 0.2.22, mismo
    criterio que `ReadWriteReceiptRequest`): `RenderImageService` aplica
    cuota por negocio con este valor -- un campo ausente o vacio cae en
    `DENIED`/`invalid_schema` aqui, antes de que el servicio llegue
    siquiera a evaluar `business_id_required`."""

    model_config = ConfigDict(extra="forbid")

    op: Literal["render_image"]
    business_id: str = Field(min_length=1)
    renderer: str = Field(pattern=_RENDERER_NAME_PATTERN)
    prompt: str = Field(min_length=1, max_length=_MAX_IMAGE_PROMPT_LEN)
    format: str = Field(pattern=_IMAGE_FORMAT_PATTERN)
    seed: int | None = None
    brand_kit: ImageBrandKitFields
