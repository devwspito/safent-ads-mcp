"""Settings via pydantic-settings, una clase por frontera de confianza
(plan.md §3). `ApiSettings`/`WorkerSettings` nunca declaran credenciales de
plataforma; `BrokerSettings` nunca declara secretos de sesion o el token MCP.
Ningun secreto tiene valor por defecto: falta en el entorno -> arranque falla
(fail loud, threat-model.md C-24) -- con una unica excepcion documentada en
`CommonSettings.telegram_bot_token`/`telegram_owner_chat_ids`: Telegram no es
un secreto que una instalacion limpia genere sola, lo crea el propietario
despues, asi que vacio degrada a "desactivado" en vez de tumbar el arranque."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Annotated
from urllib.parse import SplitResult, urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from safent_ads.composition.managed_settings import ManagedAdsSettings
from safent_ads.mcp_oauth.domain.errors import InvalidResourceError
from safent_ads.mcp_oauth.domain.resource import ResourceIndicator
from safent_ads.proposals.domain.google_channel_spec import GoogleAdvertisingChannelType
from safent_ads.shared.net.loopback import is_loopback_http_origin

_MIN_SESSION_SECRET_BYTES = 32
_ACTIVE_HOURS_PATTERN = re.compile(r"^\d{2}:\d{2}-\d{2}:\d{2}$")
_MAX_INSTANCE_NAME_LENGTH = 64
_DEFAULT_INSTANCE_NAME = "Ads MCP"
_DEFAULT_PORTS = {"https": 443, "http": 80}


@dataclass(frozen=True, slots=True)
class InstanceIdentity:
    """Como se presenta ESTA instancia a quien la autoriza (data-model.md
    §InstanceIdentity, plan.md §3): metadatos OAuth del recurso protegido,
    titulo del panel, pantalla de consentimiento, fallback sin JS de
    `index.html`. Distinto de `ApiSettings.brand_name` (`ADS_BRAND_NAME`,
    "tu negocio" por defecto), que es el NEGOCIO del que se anuncia -- ese
    lo citan `MCP_INSTRUCTIONS` y las descripciones del catalogo, nunca
    esta identidad.

    `panel_host` se DERIVA de `public_base_url`, nunca es un valor aparte
    que pueda divergir (mismo criterio que `mcp.presentation.http.
    build_mcp_instructions`)."""

    name: str
    public_base_url: str

    @property
    def panel_host(self) -> str:
        return urlsplit(self.public_base_url).netloc

    @classmethod
    def from_settings(cls, settings: ApiSettings) -> InstanceIdentity:
        return cls(name=settings.instance_name, public_base_url=settings.public_base_url)


def _parse_comma_or_json_string_list(value: object) -> object:
    """CSV (`a,b`) o JSON (`["a","b"]`) para campos `Annotated[list[str],
    NoDecode]`: pydantic-settings ya no decodifica JSON por si solo antes de
    este validador `mode="before"` (bug real, `CLOUDFLARE_ALLOWED_ZONES`
    reventaba `SettingsError` en la forma CSV -- pydantic-settings
    JSON-decodificaba el valor de entorno de un `list[str]` ANTES de que
    este validador lo viera, mismo motivo documentado en
    `CommonSettings._parse_chat_ids`). `NoDecode` deja pasar el string
    crudo hasta aqui; ambas formas se reconocen a mano."""
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if stripped.startswith("["):
        return json.loads(stripped)
    return [item.strip() for item in stripped.split(",") if item.strip()]


def _require_readable_file(env_var: str, path: Path | None) -> None:
    """Falla cerrado (`ValueError`, `model_validator` lo envuelve en
    `ValidationError`): en modo companion, TLS es obligatorio -- un
    fichero ausente o sin permiso de lectura no puede degradar a texto
    plano en silencio (spec.md INV-7)."""
    if path is None:
        raise ValueError(f"{env_var} requerido cuando ADS_COMPANION_MODE=true")
    if not path.is_file() or not os.access(path, os.R_OK):
        raise ValueError(f"{env_var} ({path}) no existe o no es legible")


class CommonSettings(ManagedAdsSettings):
    """Compartido por `ads-api` y `ads-worker`. Sin credenciales de plataforma
    (plan.md §3.1/§3.2).

    `approval_signing_key` vive aquí, no solo en `ApiSettings`: `RuleCycle`
    (plan.md §7) corre en `ads-worker` y acuña `rule_authorization` para las
    reglas `AUTO` (`AuthorizeRuleAction`), así que también necesita firmar.
    `compose.yaml` ya comparte `secrets/api.env` entre `ads-api` y
    `ads-worker` (mismo usuario del SO `adsapi`); esto solo hace explícito
    en el código un acceso que el despliegue ya concede. `ads-broker`
    (usuario `adsbroker`, frontera de confianza real) sigue sin declarar
    este campo ni ningún otro secreto de `ads-api`/`ads-worker`
    (`BrokerSettings` es una jerarquía aparte). Pendiente de confirmación
    en la revisión de seguridad ya agendada (tasks.md T075, threat-model.md
    C-3)."""

    model_config = SettingsConfigDict(
        env_file=".env", case_sensitive=True, extra="ignore", populate_by_name=True
    )

    # `SecretStr`, no `str`: la cadena de conexion lleva la contrasena de
    # Postgres embebida (`.env.example`: `postgresql+asyncpg://ads:<pwd>@...`)
    # -- sin este tipo, `repr()`/`str()`/`model_dump(mode="json")` la dejaban
    # en claro (bug real, `tests/unit/composition/
    # test_settings_never_reprs_secrets.py`). `.get_secret_value()` en los
    # tres puntos que la usan de verdad (`composition/database.py`,
    # `tools/seed_owner.py`, `tools/load_brand_kit.py`).
    database_url: SecretStr = Field(validation_alias="ADS_DATABASE_URL")
    session_secret: SecretStr = Field(validation_alias="ADS_SESSION_SECRET")
    totp_enc_key: SecretStr = Field(validation_alias="ADS_TOTP_ENC_KEY")
    # Opcional a proposito (seguimiento de T022, spec 008 fase E): con la
    # via estatica apagada -- lo normal en el producto estandar,
    # `mcp_static_token_enabled=False` -- no hay bearer de dueño que
    # configurar, y exigirlo obligaba al primer arranque a generar uno que
    # quedaba DORMIDO en `secrets/api.env`: un secreto eterno, con permiso
    # `aprobar`, que nadie usa ni rota (threat-model.md 002 C-53, R-10).
    # `ApiSettings._require_the_static_bearer_only_when_its_path_is_open`
    # lo exige cuando de verdad hace falta; `ads-worker` no lo usa nunca.
    mcp_token: SecretStr | None = Field(default=None, validation_alias="ADS_MCP_TOKEN")
    approval_signing_key: SecretStr = Field(validation_alias="ADS_APPROVAL_SIGNING_KEY")
    broker_socket_path: Path = Field(validation_alias="ADS_BROKER_SOCKET")
    public_base_url: str = Field(validation_alias="ADS_PUBLIC_BASE_URL")
    # Spec 002 (mcp_oauth): apagar `mcp_oauth_enabled` devuelve el
    # comportamiento actual exacto (solo bearer estatico). Los TTLs del
    # flujo OAuth NO son variables de entorno -- viven en
    # `mcp_oauth/application/policy.py` como constantes (mismo motivo que
    # `iam/application/session_policy.py`).
    mcp_oauth_enabled: bool = Field(default=True, validation_alias="ADS_MCP_OAUTH_ENABLED")
    # threat-model.md C-53: via de emergencia mientras los agentes migran a
    # OAuth. `False` por defecto (M6 de la revision de seguridad, 16-sep):
    # el instalador y ambos agentes ya usan OAuth, asi que el bearer de
    # dueño solo debe activarse a proposito, nunca por omision.
    mcp_static_token_enabled: bool = Field(
        default=False, validation_alias="ADS_MCP_STATIC_TOKEN_ENABLED"
    )
    # H1 de la revision de seguridad (16-sep): cuantos saltos de proxy DE
    # CONFIANZA anteponen su propia IP a `X-Forwarded-For` antes de que la
    # peticion llegue a este proceso. `0` (por defecto, y el valor correcto
    # para `compose.companion.yaml`: Safent conecta directo, sin proxy
    # intermedio) significa "no hay proxy de confianza" -- la cabecera,
    # que cualquier cliente puede falsear, se ignora por completo y se usa
    # la IP del socket. `1` es el valor en una instancia detras de Caddy
    # (el host de `ADS_PUBLIC_BASE_URL`) o de `tailscale serve`/`tailscale funnel`:
    # ambos anaden exactamente un salto, asi que el valor de confianza es
    # el ULTIMO de la lista (el resto, a su izquierda, lo escribe el
    # cliente). Documentado en README.md ("Exposicion por Tailscale" /
    # "Companion preinstalado").
    trusted_proxy_hops: int = Field(default=0, ge=0, validation_alias="ADS_TRUSTED_PROXY_HOPS")
    timezone: str = Field(default="Europe/Madrid", validation_alias="ADS_TZ")
    active_hours: str = Field(default="08:00-21:00", validation_alias="ADS_ACTIVE_HOURS")
    # `businesses.digest_hour` (0023_owner_settings) gana cuando el
    # propietario lo personaliza via `PUT /settings`; este es el valor por
    # defecto para el negocio que nunca lo toco -- 8 == el arranque de
    # `ADS_ACTIVE_HOURS` de arriba, mismo momento del dia que el digest
    # enviaba antes de tener hora propia (notifications/infrastructure/
    # sql_repositories.py::SqlPendingDigest).
    digest_hour_default: int = Field(default=8, ge=0, le=23, validation_alias="ADS_DIGEST_HOUR")
    # Unica excepcion a "ningun secreto tiene valor por defecto" de este
    # modulo: una instalacion limpia genera el resto de secrets/api.env
    # automaticamente, pero el bot de Telegram lo crea el propietario a
    # mano con @BotFather *despues*. Exigirlo en el arranque tumbaria
    # ads-api/ads-worker en cualquier instalacion limpia sin bot todavia.
    # Vacio = desactivado; `composition/api.py::_telegram_configured` y
    # `composition/worker.py` ya trataban "vacio" como "sin Telegram" --
    # esto solo deja que el valor por defecto llegue a construirse.
    telegram_bot_token: SecretStr = Field(
        default=SecretStr(""), validation_alias="TELEGRAM_BOT_TOKEN"
    )
    telegram_owner_chat_ids: list[int] = Field(
        default_factory=list, validation_alias="TELEGRAM_OWNER_CHAT_IDS"
    )

    @field_validator("session_secret")
    @classmethod
    def _require_strong_session_secret(cls, value: SecretStr) -> SecretStr:
        # Firma la cookie de sesion, el reto TOTP y (por HKDF) las URLs de
        # previsualizacion: un secreto corto degrada todo eso a la vez
        # (revision F4, condicion 6). El instalador genera 32 bytes en base64.
        if len(value.get_secret_value().encode()) < _MIN_SESSION_SECRET_BYTES:
            raise ValueError(
                "ADS_SESSION_SECRET debe tener al menos 32 bytes (p.ej. `openssl rand -base64 32`)"
            )
        return value

    @field_validator("active_hours")
    @classmethod
    def _validate_active_hours(cls, value: str) -> str:
        if not _ACTIVE_HOURS_PATTERN.match(value):
            raise ValueError(f"ADS_ACTIVE_HOURS invalido, esperado HH:MM-HH:MM: {value!r}")
        return value

    @field_validator("public_base_url")
    @classmethod
    def _normalize_public_base_url(cls, value: str) -> str:
        # RFC 8414 compara el emisor como cadena exacta (plan.md
        # "Ajustes"): una barra de mas o un path colgando rompe el
        # descubrimiento del AS en silencio.
        normalized = value[:-1] if value.endswith("/") else value
        parsed = urlsplit(normalized)
        if parsed.path or parsed.query or parsed.fragment:
            raise ValueError(f"ADS_PUBLIC_BASE_URL no admite path/query/fragment: {value!r}")
        if parsed.username is not None or parsed.password is not None:
            # Sin eco del valor: aqui el valor lleva credenciales. Antes se
            # descartaban en silencio al reconstruir el origen canonico.
            raise ValueError("ADS_PUBLIC_BASE_URL no admite usuario ni contraseña en la URL")
        cls._require_a_valid_port(parsed, original=value)
        if not cls._is_allowed_public_base_url_origin(parsed, normalized=normalized):
            raise ValueError(
                f"ADS_PUBLIC_BASE_URL debe ser https o http de bucle local "
                f"(localhost, 127.0.0.1 o [::1]): {value!r}"
            )
        canonical = cls._canonical_origin(parsed)
        cls._require_a_valid_oauth_resource(canonical, original=value)
        return canonical

    @staticmethod
    def _require_a_valid_port(parsed: SplitResult, *, original: str) -> None:
        """Revision de seguridad (PR 44): un puerto fuera de 1-65535 (mismo
        rango que `mcp_oauth/domain/client.py::RedirectUri`) se rechaza
        aqui, con un mensaje que NOMBRA `ADS_PUBLIC_BASE_URL` -- antes
        `:999999` colaba hasta `_canonical_origin`, que revienta
        con un `ValueError` incidental de `urlsplit(...).port` (correcto,
        pero opaco) al reconstruir el valor."""
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError(f"ADS_PUBLIC_BASE_URL con puerto invalido: {original!r}") from exc
        if port == 0:
            raise ValueError(f"ADS_PUBLIC_BASE_URL con puerto invalido: {original!r}")

    @staticmethod
    def _canonical_origin(parsed: SplitResult) -> str:
        """Revision de PR 44 (T049): `urlsplit` ya devuelve `scheme`/
        `hostname` en minuscula (RFC 3986 SS3.1/SS3.2.2, no distinguen
        mayusculas), asi que `http://LOCALHOST:8410` pasaba
        `_is_allowed_public_base_url_origin` sin problema -- pero el valor
        GUARDADO seguia siendo la cadena original con mayusculas.
        `ResourceIndicator.canonical()` la concatena tal cual dentro de
        `resource`, que Postgres compara con un `~` case-SENSITIVE
        (0054_mcp_oauth_loopback_resource): el primer `/authorize` volvia
        a reventar con el mismo `IntegrityError` opaco de T049, solo que
        por mayusculas en vez de por bucle local. Reconstruye desde
        `hostname`/`port` (ya normalizados) en vez de `netloc.lower()`:
        el `userinfo` ya se rechazo antes de llegar aqui. `hostname` pierde los corchetes de un
        literal IPv6
        (`[::1]` -> `::1`) -- hay que devolverselos, o el resultado ni
        siquiera separa host de puerto.

        Revision de seguridad (PR 44, MINOR a): tambien quita el puerto
        cuando es el por defecto del esquema (443 https / 80 http). El
        SDK anuncia `issuer`/`resource` como `pydantic.AnyHttpUrl`
        (`mcp_oauth/presentation/routes.py::issuer_url`,
        `build_oauth_routes`), que SIEMPRE serializa sin un puerto por
        defecto explicito (WHATWG URL, la libreria Rust de pydantic-core);
        `ResourceIndicator.canonical()` en cambio concatena
        `public_base_url` tal cual, sin pasar por `AnyHttpUrl`. Con
        `ADS_PUBLIC_BASE_URL=https://host:443` sin esta normalizacion, los
        metadatos anunciarian `resource=https://host/mcp` pero el
        `resource` REALMENTE persistido seria `https://host:443/mcp` --
        un cliente que pide exactamente lo que descubrio en los metadatos
        chocaria SIEMPRE con `invalid_target` (`StartAuthorization.
        execute()` compara cadena exacta)."""
        host = parsed.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        port = None if parsed.port == _DEFAULT_PORTS.get(parsed.scheme) else parsed.port
        port_suffix = f":{port}" if port is not None else ""
        return f"{parsed.scheme}://{host}{port_suffix}"

    @staticmethod
    def _is_allowed_public_base_url_origin(parsed: SplitResult, *, normalized: str) -> bool:
        if parsed.scheme == "https":
            return True
        return is_loopback_http_origin(normalized)

    @staticmethod
    def _require_a_valid_oauth_resource(canonical_base_url: str, *, original: str) -> None:
        """Revision de seguridad (PR 44): aserta EN EL ARRANQUE que
        `ResourceIndicator.canonical()` -- el `resource` que TODO
        `/authorize` intenta persistir -- pasa el mismo criterio que el
        CHECK de Postgres (`0054_mcp_oauth_loopback_resource`, agreement
        probado en `tests/integration/migrations/
        test_0054_mcp_oauth_loopback_resource.py`). Si algun dia divergen,
        esto revienta AQUI, con un mensaje que nombra `ADS_PUBLIC_BASE_URL`,
        en vez de en el primer `/authorize` real contra un
        `IntegrityError`/`invalid_request` opaco."""
        try:
            ResourceIndicator.canonical(canonical_base_url)
        except InvalidResourceError as exc:
            raise ValueError(
                f"ADS_PUBLIC_BASE_URL produce un resource OAuth invalido: {original!r}"
            ) from exc

    @field_validator("telegram_owner_chat_ids", mode="before")
    @classmethod
    def _parse_chat_ids(cls, value: object) -> object:
        # pydantic-settings JSON-decodes complex-typed env vars before this
        # validator runs: `TELEGRAM_OWNER_CHAT_IDS=12345` already arrives as
        # int, `=[12345,67890]` already arrives as list[int]. A
        # comma-separated string (`12345,67890`) never reaches this
        # validator -- pydantic-settings raises `SettingsError` on it first
        # trying to JSON-decode a `list[int]` field. Only the single-id
        # convenience needs wrapping.
        if isinstance(value, int):
            return [value]
        return value

    # --- 003-paquete-de-campana: gate de lanzamiento (Companion 0.2.21 esta
    # a punto de cortarse desde esta rama y la saga de publicacion del
    # paquete todavia no existe -- Meta falla con `PLATFORM_NATIVE_
    # INCOMPLETE`, `_build_package_tool_services` docstring). `False` por
    # defecto: sin esto, `propose_campaign_package` no se registra en el
    # MCP y `/api/v1/packages/**` no se monta -- 404 uniforme, igual que
    # un paquete inexistente (`composition/app.py`). En `CommonSettings`,
    # no solo en `ApiSettings` (H2, revision de codigo 2026-09-15):
    # `ads-worker` gatea con el MISMO flag el bucle
    # `run_package_publications_forever` (`composition/worker.py`) -- con
    # el flag apagado no existe la saga que ese bucle consume, asi que
    # sondear cada 5 s solo arriesgaria mover una fila de publicacion que
    # quedo abierta de antes de apagarlo.
    campaign_packages_enabled: bool = Field(
        default=False, validation_alias="ADS_CAMPAIGN_PACKAGES_ENABLED"
    )

    # --- tasks.md T076 (POLISH, "canales por
    # configuracion"): el gate de seguridad T035 solo permite publicar 0.2.24
    # con los tres canales nuevos de Google (DISPLAY/DEMAND_GEN/
    # PERFORMANCE_MAX) APAGADOS. `SEARCH` es el unico habilitado por defecto
    # -- fail closed, igual criterio que `creative_local_enabled`. CSV o
    # JSON, mismo patron que `mcp_extra_allowed_hosts`/
    # `cloudflare_allowed_zones` (`_parse_comma_or_json_string_list`); un
    # valor que no sea una fila de `GoogleAdvertisingChannelType` tumba el
    # arranque (`ValidationError` de pydantic sobre el enum), nunca se
    # ignora en silencio. `mcp.presentation.campaign_creation_args`/
    # `package_tools.py`/`campaign_draft_tools.py` leen este valor a traves
    # de `PackageToolServices`/`OpportunityToolServices`/`CampaignDraftStore`
    # (composition/app.py), nunca del entorno directamente. En `CommonSettings`,
    # no solo en `ApiSettings` (T035 security re-check 2026-09-15, mismo
    # criterio que `campaign_packages_enabled` arriba): `ads-worker` gatea con
    # el MISMO valor `SubmitApproval`/`ExecutionChokepoint`/
    # `RunPackagePublication`, el chokepoint de escritura unico que tambien
    # corre en ese proceso -- sin esto, un canal recien apagado seguia
    # escribiendose de verdad en la plataforma desde el worker aunque
    # `ads-api` ya lo rechazara en el borde REST/MCP.
    google_channels_enabled: Annotated[frozenset[GoogleAdvertisingChannelType], NoDecode] = Field(
        default=frozenset({GoogleAdvertisingChannelType.SEARCH}),
        validation_alias="ADS_GOOGLE_CHANNELS_ENABLED",
    )

    @field_validator("google_channels_enabled", mode="before")
    @classmethod
    def _parse_google_channels_enabled(cls, value: object) -> object:
        return _parse_comma_or_json_string_list(value)


class ApiSettings(CommonSettings):
    """`ads-api`: REST + MCP + bot de Telegram + panel estatico (plan.md
    §3.1). `approval_signing_key` vive en `CommonSettings` -- ver su
    docstring."""

    # El panel (React/Vite) se compila aparte (`panel/`, no este paquete) y
    # se sirve como estaticos si el directorio existe -- ausente en
    # desarrollo/CI sin build del panel, `composition/app.py` lo trata como
    # opcional, nunca como fallo de arranque.
    panel_dist_dir: Path = Field(default=Path("panel/dist"), validation_alias="ADS_PANEL_DIST_DIR")
    # Como se llama ESTE servidor para quien lo autoriza (InstanceIdentity,
    # arriba) -- NO el negocio del que se anuncia (`brand_name`, mas abajo).
    # Defecto neutro: cada despliegue fija el suyo en su propio `.env`;
    # nunca el nombre de un cliente concreto a pie de letra en el motor
    # (plan.md §2/§3).
    instance_name: str = Field(default=_DEFAULT_INSTANCE_NAME, validation_alias="ADS_INSTANCE_NAME")

    @field_validator("instance_name")
    @classmethod
    def _normalize_instance_name(cls, value: str) -> str:
        # data-model.md §InstanceIdentity: 1..64 caracteres tras recortar
        # espacios -- el mismo espacio en blanco que un operador pega sin
        # querer no debe colarse en metadatos OAuth ni en el `<title>`.
        stripped = value.strip()
        if not stripped or len(stripped) > _MAX_INSTANCE_NAME_LENGTH:
            raise ValueError(
                f"ADS_INSTANCE_NAME debe tener 1..{_MAX_INSTANCE_NAME_LENGTH} "
                f"caracteres tras recortar espacios: {value!r}"
            )
        return stripped

    # `LocalBrandAssetStorage` (logos/iconos subidos o rastreados): mismo
    # trato que `panel_dist_dir`, un directorio con valor por defecto en
    # vez de un fallo de arranque -- no es un secreto ni credencial.
    brand_asset_storage_dir: Path = Field(
        default=Path("data/brand-assets"), validation_alias="ADS_BRAND_ASSET_STORAGE_DIR"
    )
    # `LocalAssetStorage` (creative/infrastructure): mismo trato que
    # `brand_asset_storage_dir` -- directorio con valor por defecto, no un
    # fallo de arranque. `CreativeSettings` (mas abajo, todavia sin cablear
    # en `composition/app.py`) declara sus propios campos para el perfil
    # `creative-local`/ComfyUI (`ads-worker`, respaldo cloud con clave de
    # `BrokerSettings`); estos dos son los UNICOS que `ads-api` necesita
    # para servir `GET /creatives*` sin ese perfil.
    #
    # `GET /api/v1/creative-previews/{key}` (gap-creative-preview) ya sirve
    # estos bytes: `StorageUri.key` (`media_kind/ULID.ext`) NO codifica
    # `business_id` ni propietario, asi que la firma HMAC + sesion valida
    # (`CallerDep`) son la UNICA puerta -- no hay `ensure_business_access`
    # que comprobar en este recurso (modelo de un unico propietario,
    # aceptable hoy; si el producto pasa a multi-propietario, esta clave
    # deja de ser la unica defensa y hace falta repensar el esquema de
    # clave/IDOR). No hay un campo `creative_preview_signing_key` propio
    # (security-review-f4.md B-1 lo tenia con `default=` publico, sin
    # ninguna instalacion real fijandolo): `composition/app.py` deriva la
    # clave de firma con HKDF-SHA256 (`shared/crypto/hkdf.py`) a partir de
    # `session_secret`, que ya es un secreto obligatorio sin default -- un
    # secreto derivado nunca puede versionarse porque no existe como valor
    # propio.
    creative_asset_storage_dir: Path = Field(
        default=Path("data/creative-assets"), validation_alias="ADS_CREATIVE_ASSET_STORAGE_DIR"
    )
    # `LocalKitStore` (kit de marketing del negocio, encargo del dueno
    # 14-sep): montado de solo lectura por instalacion. `None` por defecto
    # a proposito (no un directorio con default como los dos de arriba):
    # `list_kit_files`/`get_kit_text`/`get_kit_file` siguen registradas y
    # reportan `KIT_NOT_CONFIGURED` mientras esto no se fije -- nunca un
    # fallo de arranque.
    kit_dir: Path | None = Field(default=None, validation_alias="ADS_KIT_DIR")

    # Imagen publica generica por defecto (lane 006-cloudflare-ui, "publicar
    # la web antes que la API deja el chat roto" -- ver tambien esa leccion
    # para no filtrar el nombre de un cliente concreto en texto que un
    # tercero puede leer): `MCP_INSTRUCTIONS`, la descripcion de `list_
    # businesses` y las del Kit de Marketing citan esto en vez del nombre
    # de un cliente a pie de letra. Cada despliegue fija el suyo en su
    # propio `.env`.
    brand_name: str = Field(default="tu negocio", validation_alias="ADS_BRAND_NAME")

    # --- companion preinstalado (diseñado en el runtime) ---------------------
    # `False` por defecto: el arranque de siempre (`make up`, dev local)
    # sigue en `127.0.0.1:8410` sin TLS, sin tocar nada de lo existente.
    # Encenderlo exige `ADS_TLS_CERTFILE`/`ADS_TLS_KEYFILE` legibles --
    # fail loud en la construccion de `ApiSettings`, antes de que
    # `composition/app.py` construya nada (mismo criterio que el resto de
    # este modulo: ningun secreto/requisito de arranque tiene un default
    # que lo esconda).
    # Interfaz de escucha en modo claro. En un contenedor debe ser 0.0.0.0 para
    # que la publicacion de puerto de compose (127.0.0.1:8410 en el host) llegue;
    # en desarrollo local, 127.0.0.1. Companion mode ignora este valor (0.0.0.0).
    bind_host: str = Field(default="127.0.0.1", validation_alias="ADS_BIND_HOST")
    companion_mode: bool = Field(default=False, validation_alias="ADS_COMPANION_MODE")
    tls_certfile: Path | None = Field(default=None, validation_alias="ADS_TLS_CERTFILE")
    tls_keyfile: Path | None = Field(default=None, validation_alias="ADS_TLS_KEYFILE")
    # Puente de sesion Safent -> safent-ads (026, contracts/sso.md §3/§4):
    # clave publica Ed25519 del par que aprovisiona `ops/container/companions/
    # ads/provision.sh` junto al bearer MCP (T001, otra rama). Solo la
    # publica -- el verificador nunca toca la privada.
    sso_public_key: str | None = Field(default=None, validation_alias="ADS_SSO_PUBLIC_KEY")

    # --- 004 tasks-2.md Q1 (contracts/mcp.md §6): cuota por clase de
    # herramienta. Ninguno es un secreto -- valores por defecto iguales a
    # los del contrato, configurables sin recompilar. `composition/app.py`
    # (`_build_mcp_registry_and_dispatcher`, I1) los cablea en `InMemoryQuota`.
    mcp_quota_writes_per_minute: int = Field(
        default=10, ge=1, validation_alias="ADS_MCP_QUOTA_WRITES_PER_MINUTE"
    )
    mcp_quota_get_meta_graph_per_minute: int = Field(
        default=30, ge=1, validation_alias="ADS_MCP_QUOTA_GET_META_GRAPH_PER_MINUTE"
    )
    mcp_quota_search_competitor_ads_per_minute: int = Field(
        default=10, ge=1, validation_alias="ADS_MCP_QUOTA_SEARCH_COMPETITOR_ADS_PER_MINUTE"
    )
    mcp_quota_get_google_keyword_ideas_per_minute: int = Field(
        default=10, ge=1, validation_alias="ADS_MCP_QUOTA_GET_GOOGLE_KEYWORD_IDEAS_PER_MINUTE"
    )
    mcp_quota_upload_creative_asset_per_minute: int = Field(
        default=3, ge=1, validation_alias="ADS_MCP_QUOTA_UPLOAD_CREATIVE_ASSET_PER_MINUTE"
    )

    # Defecto real en produccion (instancia de produccion, 0.2.21): `_transport_
    # security_for` (mcp/presentation/http.py) solo deriva `allowed_hosts`/
    # `allowed_origins` de `ADS_PUBLIC_BASE_URL`, asi que un hostname
    # provisional servido por el MISMO Caddy (uno de esos que resuelven la
    # IP en el propio nombre, antes de que el DNS del dominio propio
    # apunte) se rechaza
    # con 403 en `/mcp`. Vacia (default) = ningun host extra, mismo
    # comportamiento que antes. CSV o JSON, igual que
    # `CLOUDFLARE_ALLOWED_ZONES` (`_parse_comma_or_json_string_list`).
    # `mcp/presentation/http.py` los trata siempre como origen `https://`
    # -- nunca `http://`, ni siquiera para estos hosts provisionales.
    mcp_extra_allowed_hosts: Annotated[list[str], NoDecode] = Field(
        default_factory=list, validation_alias="ADS_MCP_EXTRA_ALLOWED_HOSTS"
    )

    @field_validator("mcp_extra_allowed_hosts", mode="before")
    @classmethod
    def _parse_mcp_extra_allowed_hosts(cls, value: object) -> object:
        return _parse_comma_or_json_string_list(value)

    # --- integrations/cloudflare (Anadido del dueno, 14-sep): conector propio
    # contra la API v4 de Cloudflare -- "MCP=acceso+control+auditoria, no
    # coaching". `None` por defecto (igual que `OPENAI_API_KEY`/`FAL_API_KEY`):
    # integracion opcional, nunca un secreto de arranque obligatorio -- sin
    # el, las cuatro herramientas responden "Cloudflare no configurado".
    cloudflare_api_token: SecretStr | None = Field(
        default=None, validation_alias="CLOUDFLARE_API_TOKEN"
    )
    # Nombres de zona (p.ej. "example.com") que las herramientas pueden
    # tocar. Vacia (default) = sin restriccion propia -- el token de
    # Cloudflare ya trae su propio alcance; esto es una segunda capa,
    # opcional, de defensa en profundidad. Acepta CSV o JSON (bug real en
    # produccion 0.2.21: `Annotated[..., NoDecode]` es necesario para que
    # la forma CSV llegue viva al validador, ver
    # `_parse_comma_or_json_string_list`).
    cloudflare_allowed_zones: Annotated[list[str], NoDecode] = Field(
        default_factory=list, validation_alias="CLOUDFLARE_ALLOWED_ZONES"
    )

    @field_validator("cloudflare_allowed_zones", mode="before")
    @classmethod
    def _parse_cloudflare_allowed_zones(cls, value: object) -> object:
        return _parse_comma_or_json_string_list(value)

    @model_validator(mode="after")
    def _require_exactly_one_mcp_authority_mode(self) -> ApiSettings:
        # 004 tasks.md A6/A10 (aclaracion del dueno): `/mcp` siempre resuelve
        # el alcance de una de dos formas -- Enterprise (produccion/alojado)
        # o el propietario unico (Safent local, motor Hermes) -- nunca
        # ninguna, nunca las dos. `managed_central` es un tercer perfil que
        # ni siquiera monta `/mcp` (`create_managed_app`), asi que queda
        # fuera de esta regla.
        if self.managed_central:
            return self
        if self.companion_mode and not self.seat_authority_enabled and not self.single_owner_mode:
            # Regresion 0.2.21 → 0.2.23 (18-sep-2026): la app Safent lanza el
            # companion con ADS_COMPANION_MODE=true y su provision.sh nunca
            # escribio ADS_SINGLE_OWNER_MODE, asi que ads-api moria al arrancar
            # (2447 reinicios en el Mac del dueno). El modo companion ES el
            # propietario unico: se asume, sin exigirlo al instalador.
            object.__setattr__(self, "single_owner_mode", True)
            return self
        if self.seat_authority_enabled == self.single_owner_mode:
            raise ValueError(
                "exactamente uno de ADS_SEAT_AUTHORITY_ENABLED/ADS_SINGLE_OWNER_MODE "
                "debe estar activo"
            )
        return self

    @model_validator(mode="after")
    def _companion_mode_keeps_the_static_bearer(self) -> ApiSettings:
        # 0.2.30 (16-sep-2026): el motor Hermes de la app Safent se autentica
        # al `/mcp` del companion SOLO con el bearer estatico (`ADS_MCP_TOKEN`,
        # que escribe su provision.sh); ahi no hay agente externo que pueda
        # hacer OAuth. Con M6 (estatico apagado por omision) el companion
        # 0.2.29 dejaba a la app fuera de su propio MCP. El modo companion
        # enciende el bearer salvo que el instalador lo apague a proposito.
        if self.companion_mode and "mcp_static_token_enabled" not in self.model_fields_set:
            object.__setattr__(self, "mcp_static_token_enabled", True)
        return self

    @model_validator(mode="after")
    def _require_the_static_bearer_only_when_its_path_is_open(self) -> ApiSettings:
        # Declarado DESPUES de `_companion_mode_keeps_the_static_bearer` a
        # proposito: pydantic corre los validadores "after" en orden de
        # declaracion, asi que el modo companion ya encendio el interruptor
        # cuando se llega aqui -- y ahi el bearer si es obligatorio.
        # Vacio cuenta como ausente: un `ADS_MCP_TOKEN=` en `secrets/api.env`
        # abriria la puerta estatica con una comparacion contra "".
        if not self.mcp_static_token_enabled:
            return self
        if self.mcp_token is None or not self.mcp_token.get_secret_value():
            raise ValueError("ADS_MCP_TOKEN requerido cuando ADS_MCP_STATIC_TOKEN_ENABLED=true")
        return self

    @model_validator(mode="after")
    def _require_readable_tls_material_in_companion_mode(self) -> ApiSettings:
        if not self.companion_mode:
            return self
        _require_readable_file("ADS_TLS_CERTFILE", self.tls_certfile)
        _require_readable_file("ADS_TLS_KEYFILE", self.tls_keyfile)
        return self

    @model_validator(mode="after")
    def _require_valid_sso_public_key_in_companion_mode(self) -> ApiSettings:
        if not self.companion_mode:
            return self
        # Import local (evita que este modulo de configuracion arrastre
        # `cryptography` a quien solo necesita leer `ApiSettings`).
        from safent_ads.iam.infrastructure.ed25519_assertion_verifier import (  # noqa: PLC0415
            SsoPublicKeyError,
            decode_ed25519_public_key,
        )

        if not self.sso_public_key:
            raise ValueError("ADS_SSO_PUBLIC_KEY requerido cuando ADS_COMPANION_MODE=true")
        try:
            decode_ed25519_public_key(self.sso_public_key)
        except SsoPublicKeyError as exc:
            raise ValueError(str(exc)) from exc
        return self

    # --- Login federado con Google (spec 002b, research.md Decision E) ---
    # Bloque ADITIVO: no toca ningun campo ni validador de arriba.
    #
    # `False` por defecto (default-deny, FR-124): apagado, `iam/presentation/
    # federated_router.py` ni se registra y el comportamiento es, linea por
    # linea, el del spec 002. Encenderlo sin el resto de la configuracion NO
    # rompe el arranque -- lo prohibe el spec ("lista vacia o mal escrita: el
    # login federado queda cerrado, el servicio arranca") --: `federated_login_
    # active` se queda en `False` y `composition/app.py` lo delata con un
    # evento de diagnostico, sin valores.
    federated_login_enabled: bool = Field(
        default=False, validation_alias="ADS_FEDERATED_LOGIN_ENABLED"
    )
    # Cliente OAuth de Google. El despliegue inicial REUTILIZA el de Safent
    # Enterprise (spec 002b S4): el dueno solo anade el redirect URI
    # `<ADS_PUBLIC_BASE_URL>/api/v1/auth/federated/callback` -- bajo
    # `/api` porque la cookie `ads_session` se emite con `path=/api`.
    google_oidc_client_id: str | None = Field(
        default=None, validation_alias="ADS_GOOGLE_OIDC_CLIENT_ID"
    )
    # `SecretStr` (no `str`): nunca en `repr`, `str` ni `model_dump(mode=
    # "json")` -- FR-118/SC-107. Sin default, como el resto de secretos.
    google_oidc_client_secret: SecretStr | None = Field(
        default=None, validation_alias="ADS_GOOGLE_OIDC_CLIENT_SECRET"
    )
    # Direcciones COMPLETAS que pueden crear sesion (FR-105): ni dominios ni
    # comodines -- cualquiera con una cuenta del dominio seria dueno del gasto
    # publicitario. VACIA = NADIE, nunca "vacia = todos". CSV o JSON, igual
    # que `CLOUDFLARE_ALLOWED_ZONES`: `Annotated[..., NoDecode]` es
    # obligatorio para que la forma CSV llegue viva al validador (bug real en
    # produccion 0.2.21, ver `_parse_comma_or_json_string_list`).
    federated_allowed_emails: Annotated[list[str], NoDecode] = Field(
        default_factory=list, validation_alias="ADS_FEDERATED_ALLOWED_EMAILS"
    )

    @field_validator("federated_allowed_emails", mode="before")
    @classmethod
    def _parse_federated_allowed_emails(cls, value: object) -> object:
        """CSV/JSON + normalizacion `strip()` y minusculas. El claim `email`
        de Google ya llega canonico, asi que no se normalizan puntos ni alias
        de Gmail: eso ensancharia la lista en vez de cerrarla."""
        parsed = _parse_comma_or_json_string_list(value)
        if not isinstance(parsed, list):
            return parsed
        normalized = [item.strip().lower() if isinstance(item, str) else item for item in parsed]
        return [item for item in normalized if item != ""]

    @property
    def federated_login_active(self) -> bool:
        """Interruptor EFECTIVO: encendido y configurado por completo. Es lo
        que consultan el registro del router y la pantalla de entrada --
        `federated_login_enabled` a secas nunca decide nada."""
        secret = self.google_oidc_client_secret
        return bool(
            self.federated_login_enabled
            and (self.google_oidc_client_id or "").strip()
            and secret is not None
            and secret.get_secret_value().strip()
            and self.federated_allowed_emails
        )


class WorkerSettings(CommonSettings):
    """`ads-worker`: scheduler de ciclos + trabajador de ejecucion
    (plan.md §3.2). Firma `rule_authorization` desde `RuleCycle`
    (`approval_signing_key` en `CommonSettings`)."""


class BrokerSettings(ManagedAdsSettings):
    """`ads-broker`: unico poseedor de credenciales de plataforma
    (plan.md §3.3). Aislado de `CommonSettings` a proposito: no declara
    secretos de sesion, TOTP ni el token MCP."""

    model_config = SettingsConfigDict(
        env_file="secrets/broker.env", case_sensitive=True, extra="ignore", populate_by_name=True
    )

    companion_mode: bool = Field(default=False, validation_alias="ADS_COMPANION_MODE")
    sso_public_key: str | None = Field(default=None, validation_alias="ADS_SSO_PUBLIC_KEY")

    @model_validator(mode="after")
    def _require_companion_lease_verifier(self) -> BrokerSettings:
        if self.companion_mode:
            from safent_ads.iam.infrastructure.ed25519_assertion_verifier import (  # noqa: PLC0415
                decode_ed25519_public_key,
            )

            if not self.sso_public_key:
                raise ValueError("ADS_SSO_PUBLIC_KEY required in companion mode")
            decode_ed25519_public_key(self.sso_public_key)
        return self

    broker_socket_path: Path = Field(validation_alias="ADS_BROKER_SOCKET")
    approval_public_key: str = Field(validation_alias="ADS_APPROVAL_PUBLIC_KEY")
    allowed_uids: list[int] = Field(validation_alias="ADS_BROKER_ALLOWED_UIDS")
    hard_caps_file: Path = Field(validation_alias="ADS_BROKER_HARD_CAPS_FILE")
    # spec 008 T029: sin defecto, y obligatoria SOLO si `caps.yaml` declara
    # `panel_managed`. Un defecto la haria aparecer en despliegues que no
    # usan la funcion, y el arranque no puede elegir un directorio de
    # estado por el operador. Referencia: /var/lib/ads-broker/caps-state.
    caps_state_dir: Path | None = Field(default=None, validation_alias="ADS_BROKER_CAPS_STATE_DIR")
    google_native_mcp_executable: Path | None = Field(
        default=None, validation_alias="ADS_GOOGLE_NATIVE_MCP_EXECUTABLE"
    )
    google_native_mcp_project: str | None = Field(
        default=None, validation_alias="ADS_GOOGLE_NATIVE_MCP_PROJECT"
    )
    meta_native_mcp_enabled: bool = Field(
        default=False, validation_alias="ADS_META_NATIVE_MCP_ENABLED"
    )
    # Credenciales del VENDOR (Safent), unicas para todo el proceso: el
    # cliente OAuth del proyecto de Google Cloud y la app de Meta que
    # negocian el OAuth "Conectar" de cada cliente. Las credenciales del
    # CLIENTE (refresh token de Google, token
    # de sistema de Meta) ya NO viven en el entorno — el propietario las
    # conecta desde la UI (`accounts/presentation/connections_router.py`) y
    # quedan cifradas en `credential_store_dir`, nunca en variables de
    # entorno ni en la base de datos (threat-model.md C-24).
    google_ads_client_id: SecretStr | None = Field(
        default=None, validation_alias="GOOGLE_ADS_CLIENT_ID"
    )
    google_ads_client_secret: SecretStr | None = Field(
        default=None, validation_alias="GOOGLE_ADS_CLIENT_SECRET"
    )
    google_ads_login_customer_id: str | None = Field(
        default=None, validation_alias="GOOGLE_ADS_LOGIN_CUSTOMER_ID"
    )
    meta_app_id: SecretStr | None = Field(default=None, validation_alias="META_APP_ID")
    meta_app_secret: SecretStr | None = Field(default=None, validation_alias="META_APP_SECRET")
    # Broker only: never an API/worker/frontend setting or a per-customer token.
    composio_api_key: SecretStr | None = Field(
        default=None, validation_alias="ADS_COMPOSIO_API_KEY", repr=False
    )
    composio_googleads_auth_config_id: str | None = Field(
        default=None, validation_alias="ADS_COMPOSIO_GOOGLEADS_AUTH_CONFIG_ID"
    )
    composio_metaads_auth_config_id: str | None = Field(
        default=None, validation_alias="ADS_COMPOSIO_METAADS_AUTH_CONFIG_ID"
    )
    # --- lane: oauth-connect ---
    # `broker/infrastructure/credential_store.py`: clave maestra AES-256-GCM
    # del almacen cifrado de tokens de cliente. Sin valor por defecto, igual
    # que `ADS_TOTP_ENC_KEY`/`ADS_APPROVAL_SIGNING_KEY` — falta en el
    # entorno -> el broker no arranca (fail closed, threat-model.md C-24).
    credential_master_key: SecretStr = Field(validation_alias="ADS_CREDENTIAL_MASTER_KEY")
    credential_store_dir: Path = Field(validation_alias="ADS_CREDENTIAL_STORE_DIR")
    # --- end lane: oauth-connect ---
    # --- lane: creative ---
    # threat-model.md C-29: "claves cloud en el broker" — el respaldo cloud
    # de creative-port.md (gpt-image-1.5, fal.ai) usa las mismas credenciales
    # de pago que las plataformas de anuncios, con la misma frontera de
    # confianza. `None` por defecto: sin respaldo cloud configurado, el
    # selector de renderizadores solo puede elegir local (research.md D7).
    openai_api_key: SecretStr | None = Field(default=None, validation_alias="OPENAI_API_KEY")
    fal_api_key: SecretStr | None = Field(default=None, validation_alias="FAL_API_KEY")
    # `fal_image_adapter.FalImageRenderer`: mismo modelo por defecto que
    # Hermes ya usa para generar imagenes (encargo del propietario
    # 2026-09-15, "total parity"). Configurable, nunca una constante, por
    # si fal.ai retira/renombra el modelo.
    fal_image_model: str = Field(
        default="fal-ai/flux-2/klein/9b", validation_alias="ADS_FAL_IMAGE_MODEL"
    )
    # Precio de lista por imagen, USD -- fal.ai no publica uno propio para
    # `flux-2/klein/9b` todavia (ver docstring de `fal_image_adapter.py`):
    # 0.02 es una estimacion documentada, ajustable sin desplegar codigo en
    # cuanto haya factura real que medir.
    fal_image_price_usd: Decimal = Field(
        default=Decimal("0.02"), validation_alias="ADS_FAL_IMAGE_PRICE_USD"
    )
    # M-2 (revision de seguridad 0.2.22): OpenAI factura `gpt-image-1.5` por
    # tokens de SALIDA, no por imagen -- el coste real por imagen no esta
    # verificado todavia (research/creative-via-codex.md: "el numero de
    # tokens... no aparece en la pagina de precios"). Esta NO es la cifra
    # que se factura (`openai_image_adapter.py::_cost_from_usage` sigue
    # leyendo `usage` de la respuesta real) -- es solo una estimacion
    # conservadora para el tope de coste PREVIO a la llamada
    # (`RenderImageService`, nunca se asume gratis un renderizador sin
    # precio conocido). Ajustable sin desplegar codigo en cuanto haya
    # factura real que medir, igual que `fal_image_price_usd`.
    openai_image_price_usd_estimate: Decimal = Field(
        default=Decimal("0.20"), validation_alias="ADS_OPENAI_IMAGE_PRICE_USD_ESTIMATE"
    )
    # M-2: tope de coste por llamada a `render_image`, comprobado contra el
    # precio de lista ANTES de llamar al proveedor -- `RenderImageService`
    # rechaza con `RENDER_COST_CAP` si el precio del renderizador pedido lo
    # supera (o no tiene precio conocido).
    render_image_max_cost_usd: Decimal = Field(
        default=Decimal("0.50"), gt=0, validation_alias="ADS_RENDER_IMAGE_MAX_COST_USD"
    )
    # M-2: el tope de 10/min de `RenderImageService` es GLOBAL, compartido
    # por todos los negocios -- estas dos cuotas son POR NEGOCIO, para que
    # uno solo no pueda agotar el presupuesto de todos los demas.
    render_image_quota_per_minute: int = Field(
        default=6, ge=1, validation_alias="ADS_RENDER_IMAGE_QUOTA_PER_MINUTE"
    )
    render_image_quota_per_day: int = Field(
        default=120, ge=1, validation_alias="ADS_RENDER_IMAGE_QUOTA_PER_DAY"
    )
    # --- end lane: creative ---

    @field_validator("allowed_uids", mode="before")
    @classmethod
    def _parse_allowed_uids(cls, value: object) -> object:
        # Mismo criterio que `CommonSettings._parse_chat_ids`: pydantic-settings
        # ya JSON-decodifica `ADS_BROKER_ALLOWED_UIDS` antes de llegar aqui, asi
        # que una lista separada por comas (`10001,10002`) nunca la ve este
        # validador -- levanta `SettingsError` antes. Solo el uid suelto
        # (`10001`) necesita envolverse en una lista de uno.
        if isinstance(value, int):
            return [value]
        return value


# --- lane: creative ---
class CreativeSettings(BaseSettings):
    """Config no secreta de `creative` (compartida por `ads-api`/`ads-worker`).
    Las claves de proveedores cloud de pago viven en `BrokerSettings`
    (threat-model.md C-29), no aqui."""

    model_config = SettingsConfigDict(
        env_file=".env", case_sensitive=True, extra="ignore", populate_by_name=True
    )

    creative_assets_dir: Path = Field(validation_alias="ADS_CREATIVE_ASSETS_DIR")
    creative_preview_signing_key: SecretStr = Field(
        validation_alias="ADS_CREATIVE_PREVIEW_SIGNING_KEY"
    )
    # renderer_selector.py `build_registry`: `False` por defecto deja los
    # descriptores `LOCAL_GPU` (ComfyUI) estructuralmente ausentes del
    # registro — cierre en falso, no una bandera que un `if` pueda saltarse.
    # research.md D7 / threat-model.md C-29: 4 caidas termicas de la DGX el
    # 9-sep-2026 (0%->96%, 70->94 C en 1-2 minutos). Reactivar exige permiso
    # explicito por tanda, no un valor persistente en `.env`.
    creative_local_enabled: bool = Field(
        default=False, validation_alias="ADS_CREATIVE_LOCAL_ENABLED"
    )
    comfyui_base_url: str = Field(
        default="http://127.0.0.1:8188", validation_alias="ADS_COMFYUI_BASE_URL"
    )
    comfyui_workflows_dir: Path = Field(validation_alias="ADS_CREATIVE_WORKFLOWS_DIR")
    comfyui_timeout_s: int = Field(default=180, validation_alias="ADS_COMFYUI_TIMEOUT_S")
    # threat-model.md C-11/C-12: allow-list de host exacto para
    # `import_creative_asset` (activo que el agente trae de una herramienta
    # nativa). Vacia por defecto — nada permitido hasta configurar.
    creative_import_allowed_hosts: list[str] = Field(
        default_factory=list, validation_alias="ADS_CREATIVE_IMPORT_ALLOWED_HOSTS"
    )
    chatterbox_tts_base_url: str | None = Field(
        default=None, validation_alias="ADS_CHATTERBOX_TTS_BASE_URL"
    )
    acestep_music_base_url: str | None = Field(
        default=None, validation_alias="ADS_ACESTEP_MUSIC_BASE_URL"
    )
    creative_destination_allowed_domains: list[str] = Field(
        default_factory=list, validation_alias="ADS_CREATIVE_DESTINATION_ALLOWED_DOMAINS"
    )

    @field_validator(
        "creative_destination_allowed_domains",
        "creative_import_allowed_hosts",
        mode="before",
    )
    @classmethod
    def _parse_allowed_domains(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value


# --- end lane: creative ---
