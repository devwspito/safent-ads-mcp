"""Modelos pydantic estrictos de argumentos (T045, threat-model.md C-11):
enums cerrados, IDs con patron, sin URLs libres. Cada modelo es la unica
fuente de verdad de validacion para su herramienta — el `ToolDispatcher` los
usa directamente, independientemente de que el SDK MCP tambien introspeccione
la firma de la funcion montada (`presentation/mount.py`).

Regla de diseno explicita (Assumption, ver informe de la lane): TODO modelo
salvo `ListBusinessesArgs` exige `business_id`, aunque el identificador
"natural" de la herramienta sea otro (`asset_id`, `signal_id`, ...). Esto
hace literal la regla 4 del contrato ("todo argumento resuelve a un
business_id") y deja la autorizacion del `ToolDispatcher` en un unico
camino, sin la doble via de "autorizar antes" vs "autorizar despues de
resolver el id" — el handler solo tiene que comprobar que el id pedido
pertenece de verdad a ese `business_id` (si no, `ENTITY_NOT_FOUND`, nunca
`BUSINESS_FORBIDDEN`, para no filtrar existencia entre negocios)."""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Any

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    field_validator,
    model_validator,
)

from safent_ads.accounts.domain.google_customer_id import normalize_google_customer_id
from safent_ads.creative.domain.enums import MediaKind as CreativeMediaKind
from safent_ads.mcp.application.dto import (
    BrandAssetKind,
    CampaignStatus,
    Granularity,
    MediaKind,
    PlatformCode,
    ProposalState,
    SignalKind,
    WindowPreset,
)
from safent_ads.mcp.domain.native_write_payload import (
    NativeWritePayloadError,
    validate_native_write_payload,
)
from safent_ads.mcp.presentation.ad_child_args import ChildPlanArgs
from safent_ads.proposals.domain.ad_child_creation import validate_child_payload
from safent_ads.shared.ids import EntityRef, EntityRefFormatError
from safent_ads.shared.ids import PlatformCode as AccountsPlatformCode

_UUID_PATTERN = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
_OPAQUE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"
_IDENTIFIER_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"
# Catalogo M01-M24/G01-G11/X01-X02 (contracts/mcp-tools.md `Cause.rule_id`,
# `rules/domain/rule.py::Rule.code`): letra(s) + 2 digitos, MAYUSCULAS --
# distinto de `Identifier` (minusculas), que es para otra cosa (claves de
# agrupacion propias, no codigos de regla del catalogo).
_RULE_CODE_PATTERN = r"^[A-Z]{1,2}\d{2}$"
_SCHEME_SEPARATOR = "://"
# Ningun esquema IANA real supera esto (el mas largo registrado, "microsoft-edge-holographic",
# tiene 27 caracteres): basta para el veto de seguridad sin acotar el escaneo por longitud del
# string entero (004 tasks-2.md W4: `upload_creative_asset.content_base64` puede llegar a 8 MiB
# -- `[a-zA-Z][a-zA-Z0-9+.-]*://` combinado con `re.search` es O(n^2) en un string sin ':' en
# absoluto, como el alfabeto base64: 8 MiB tardaba horas, no milisegundos).
_MAX_SCHEME_LENGTH = 32
_SCHEME_CHARS_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*$")

_MIN_STRENGTH_FLOOR = 0
_MAX_STRENGTH_CEILING = 100
_MAX_PAGE_LIMIT = 200
_MAX_GAQL_QUERY_LENGTH = 4000
_FORBIDDEN_GAQL_KEYWORDS = ("mutate", "update", "insert", "delete", "drop", "truncate", "alter")


def _validate_entity_ref(value: str) -> str:
    try:
        EntityRef.parse(value)
    except EntityRefFormatError as exc:
        raise ValueError(str(exc)) from exc
    return value


BusinessId = Annotated[str, Field(pattern=_UUID_PATTERN)]
EntityRefStr = Annotated[str, AfterValidator(_validate_entity_ref)]
OpaqueId = Annotated[str, Field(pattern=_OPAQUE_ID_PATTERN, max_length=128)]
Identifier = Annotated[str, Field(pattern=_IDENTIFIER_PATTERN, max_length=64)]
RuleCode = Annotated[str, Field(pattern=_RULE_CODE_PATTERN)]


def _contains_url_scheme(value: str) -> bool:
    """Equivalente a `re.search(r"[a-zA-Z][a-zA-Z0-9+.-]*://", value)` pero
    lineal: busca el separador literal `://` (rapido, sin cuantificador) y
    solo entonces comprueba, en una ventana acotada hacia atras, si algun
    sufijo de esa ventana es un esquema valido -- nunca vuelve a escanear
    el string entero por cada `://` encontrado."""
    start = 0
    while (index := value.find(_SCHEME_SEPARATOR, start)) != -1:
        window = value[max(0, index - _MAX_SCHEME_LENGTH) : index]
        if any(_SCHEME_CHARS_PATTERN.match(window[i:]) for i in range(len(window))):
            return True
        start = index + 1
    return False


_MAX_URL_SCAN_DEPTH = 8


def _reject_free_urls(value: Any, *, depth: int = 0) -> None:  # noqa: ANN401 - validador generico
    """Barrera generica (C-11 "sin URLs libres", M-3): ningun campo de
    texto de ningun modelo de argumentos puede contener un esquema de URL,
    a CUALQUIER profundidad -- recorrido recursivo completo de dicts y
    listas, no solo los dos primeros niveles. Los campos que de verdad
    identifican algo ya llevan un patron cerrado arriba; esto cubre los
    campos de texto libre (`query`, `cause_key`, `event_type`, el `payload`
    de `propose_native_write`...) que no deberian aceptar un destino de
    red, aunque vengan anidados dentro de un objeto o una lista."""
    if depth > _MAX_URL_SCAN_DEPTH:
        raise ValueError(f"argumento demasiado anidado (profundidad > {_MAX_URL_SCAN_DEPTH})")
    if isinstance(value, str):
        if _contains_url_scheme(value):
            raise ValueError("no se admiten URLs en argumentos de herramienta")
        return
    if isinstance(value, Mapping):
        for nested in value.values():
            _reject_free_urls(nested, depth=depth + 1)
        return
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        for item in value:
            _reject_free_urls(item, depth=depth + 1)


def reject_urls_except_child_plan(data: Any) -> Any:  # noqa: ANN401 - validador generico
    """Variante de `_reject_free_urls` para los dos modelos de argumentos
    que llevan un `child_plan` (`ProposeAdChildArgs` en el MCP directo,
    `ManagedAdChildArgs` en `composition/managed_service.py`): el `native`
    de un anuncio Google lleva `final_url`/`final_urls` con esquema
    `https` a proposito (M-3), y `ad_child_creation.py` (`_url`) ya exige
    ese esquema, sin credenciales ni IP privada, con mas precision que la
    barrera generica de `ToolArgs`. El resto de campos, incluido el propio
    `entity_ref`/`cause` de estos modelos, conserva la guarda ordinaria."""
    if isinstance(data, dict):
        for key, value in data.items():
            if key != "child_plan":
                _reject_free_urls(value)
    return data


class ToolArgs(BaseModel):
    """Base comun: sin campos extra, inmutable, sin URLs libres en ningun
    string (a cualquier profundidad de nivel superior)."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    @model_validator(mode="before")
    @classmethod
    def _reject_urls_in_raw_strings(cls, data: Any) -> Any:  # noqa: ANN401
        if isinstance(data, dict):
            for value in data.values():
                _reject_free_urls(value)
        return data


class WindowArgs(BaseModel):
    """`contracts/mcp-tools.md`: `{preset, lag_days}` o `{from, to}`, nunca
    ambos ni ninguno."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    preset: WindowPreset | None = None
    lag_days: int = Field(default=0, ge=0, le=90)
    date_from: date | None = Field(default=None, alias="from")
    date_to: date | None = Field(default=None, alias="to")

    @model_validator(mode="after")
    def _exactly_one_shape(self) -> WindowArgs:
        has_preset = self.preset is not None
        has_range = self.date_from is not None or self.date_to is not None
        if has_preset and has_range:
            raise ValueError("window: usar `preset` o `from`/`to`, no ambos")
        if not has_preset and not has_range:
            raise ValueError("window: falta `preset` o `from`/`to`")
        if has_range and (self.date_from is None or self.date_to is None):
            raise ValueError("window: `from` y `to` van juntos")
        if (
            self.date_from is not None
            and self.date_to is not None
            and self.date_from > self.date_to
        ):
            raise ValueError("window: `from` no puede ser posterior a `to`")
        return self


class PageArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    limit: int = Field(default=50, ge=1, le=_MAX_PAGE_LIMIT)
    cursor: OpaqueId | None = None


def _default_page() -> PageArgs:
    return PageArgs()


# --- portfolio --------------------------------------------------------


class ListBusinessesArgs(ToolArgs):
    """Unica herramienta sin `business_id`: lista los negocios que la
    `CallerScope` ya autoriza."""


class ListPlatformAccountsArgs(ToolArgs):
    business_id: BusinessId


class GetPortfolioOverviewArgs(ToolArgs):
    business_id: BusinessId
    window: WindowArgs


class GetDataFreshnessArgs(ToolArgs):
    business_id: BusinessId


# --- entities / metrics -------------------------------------------------


class ListCampaignsArgs(ToolArgs):
    business_id: BusinessId
    platform: PlatformCode | None = None
    status: CampaignStatus | None = None
    page: PageArgs = Field(default_factory=_default_page)


class GetCampaignArgs(ToolArgs):
    business_id: BusinessId
    entity_ref: EntityRefStr


class ListAdSetsArgs(ToolArgs):
    business_id: BusinessId
    entity_ref: EntityRefStr
    page: PageArgs = Field(default_factory=_default_page)


class ListAdsArgs(ToolArgs):
    business_id: BusinessId
    entity_ref: EntityRefStr
    page: PageArgs = Field(default_factory=_default_page)


class ListCreativesArgs(ToolArgs):
    business_id: BusinessId
    media_kind: MediaKind | None = None
    page: PageArgs = Field(default_factory=_default_page)


class GetCreativeArgs(ToolArgs):
    business_id: BusinessId
    asset_id: OpaqueId


class GetEntityMetricsArgs(ToolArgs):
    business_id: BusinessId
    entity_ref: EntityRefStr
    window: WindowArgs
    granularity: Granularity


class GetInsightsArgs(ToolArgs):
    business_id: BusinessId
    entity_ref: EntityRefStr
    window: WindowArgs
    breakdown: Identifier | None = None


class RunGaqlArgs(ToolArgs):
    business_id: BusinessId
    account_ref: OpaqueId
    query: str = Field(max_length=_MAX_GAQL_QUERY_LENGTH)

    @model_validator(mode="after")
    def _only_select(self) -> RunGaqlArgs:
        normalized = self.query.strip().lower()
        if not normalized.startswith("select"):
            raise ValueError("run_gaql: la consulta debe empezar por SELECT")
        if any(keyword in normalized for keyword in _FORBIDDEN_GAQL_KEYWORDS):
            raise ValueError("run_gaql: consulta de escritura no permitida")
        return self


# --- signals / anomalies / pacing --------------------------------------


class ListSignalsArgs(ToolArgs):
    business_id: BusinessId
    kind: SignalKind | None = None
    min_strength: int | None = Field(default=None, ge=_MIN_STRENGTH_FLOOR, le=_MAX_STRENGTH_CEILING)
    since: datetime | None = None
    page: PageArgs = Field(default_factory=_default_page)


class GetSignalArgs(ToolArgs):
    business_id: BusinessId
    signal_id: OpaqueId


class ExplainSignalArgs(ToolArgs):
    business_id: BusinessId
    signal_id: OpaqueId


class ListAnomaliesArgs(ToolArgs):
    business_id: BusinessId
    since: datetime


class GetPacingArgs(ToolArgs):
    business_id: BusinessId
    entity_ref: EntityRefStr


# --- rules / guardrails ------------------------------------------------


class ListRulesArgs(ToolArgs):
    business_id: BusinessId
    platform: PlatformCode | None = None
    enabled: bool | None = None


class GetRuleArgs(ToolArgs):
    business_id: BusinessId
    rule_id: OpaqueId


class ExplainRuleArgs(ToolArgs):
    business_id: BusinessId
    rule_id: OpaqueId
    entity_ref: EntityRefStr | None = None


class ListGuardrailsArgs(ToolArgs):
    business_id: BusinessId
    scope_ref: OpaqueId


class GetKillSwitchStatusArgs(ToolArgs):
    business_id: BusinessId


# --- proposals -----------------------------------------------------------


class ListProposalsArgs(ToolArgs):
    business_id: BusinessId
    state: ProposalState | None = None
    cause_key: Identifier | None = None
    page: PageArgs = Field(default_factory=_default_page)


class GetProposalArgs(ToolArgs):
    business_id: BusinessId
    proposal_id: OpaqueId


# --- catalog ---------------------------------------------------------------


class ListOfferingsArgs(ToolArgs):
    business_id: BusinessId


class ListCalendarEventsArgs(ToolArgs):
    business_id: BusinessId
    kind: str | None = None
    open_only: bool = False


class GetCalendarEventArgs(ToolArgs):
    business_id: BusinessId
    calendar_event_id: OpaqueId


# --- audit / decision log -------------------------------------------------


class SearchDecisionLogArgs(ToolArgs):
    business_id: BusinessId
    since: datetime
    until: datetime
    event_type: Identifier | None = None
    entity_ref: EntityRefStr | None = None
    page: PageArgs = Field(default_factory=_default_page)

    @model_validator(mode="after")
    def _since_before_until(self) -> SearchDecisionLogArgs:
        if self.since > self.until:
            raise ValueError("search_decision_log: `since` no puede ser posterior a `until`")
        return self


class GetDecisionLogEntryArgs(ToolArgs):
    business_id: BusinessId
    seq: int = Field(ge=1)


# --- creative ---------------------------------------------------------


class ListCreativeBriefsArgs(ToolArgs):
    business_id: BusinessId


class GetCreativeJobArgs(ToolArgs):
    business_id: BusinessId
    job_id: OpaqueId


# --- brand ---------------------------------------------------------------


class GetBrandKitArgs(ToolArgs):
    business_id: BusinessId


class ListBrandAssetsArgs(ToolArgs):
    business_id: BusinessId
    kind: BrandAssetKind | None = None


# --- conexiones de plataforma (Anadido del dueno, 15-sep) -----------------
# Clase `CONNECTION_WRITE`: solo visible con permiso `aprobar`
# (`registry.py::_CONNECTION_WRITE_NAMES`). Reutilizan
# `accounts.application.begin_oauth_connect`/`get_oauth_connect_status`, el
# mismo flujo OAuth que ya usa el panel.


class ConnectPlatformAccountArgs(ToolArgs):
    business_id: BusinessId
    platform: PlatformCode
    account_hint: Annotated[str, Field(max_length=200)] | None = None
    google_customer_id: str | None = Field(default=None, max_length=32)

    @field_validator("google_customer_id")
    @classmethod
    def _normalize_customer_id(cls, value: str | None) -> str | None:
        # Shape-only check, same as `accounts.presentation.payloads.
        # BeginConnectRequest`: the handler enforces the real `platform`
        # (rejecting this field for Meta) once it knows which provider it is.
        return normalize_google_customer_id(value, provider=AccountsPlatformCode.GOOGLE)


class GetConnectionStatusArgs(ToolArgs):
    business_id: BusinessId
    platform: PlatformCode


# --- contexto de proyecto / capacidades -----------------------------------


class GetProjectContextArgs(ToolArgs):
    business_id: BusinessId


class GetCapabilitiesArgs(ToolArgs):
    business_id: BusinessId


# --- escrituras (US2/US3, contracts/mcp-tools.md §Escrituras) -------------
#
# `Cause`/`Evidence` aqui son la forma minima que `proposals.domain`
# necesita para construir un `Cause`/`Evidence` real -- el contrato tambien
# describe `Evidence.window` (objeto `Window`) y `.sample` (gasto +
# conversiones); se simplifica a `window_preset: str` (lo unico que
# `proposals.domain.cause.Evidence` modela hoy) en vez de inventar un
# `sample` que el dominio no usa (Assumption documentada).
#
# `urgency` usa los 3 niveles reales de `proposals.domain.priority.Urgency`
# (critical/recommended/minor) en vez del "alta|normal" simplificado del
# contrato: forzar un mapeo 2->3 inventaria una politica que spec.md no fija.


class CauseArgs(ToolArgs):
    text: Annotated[str, Field(min_length=1, max_length=140)]
    signal_id: OpaqueId | None = None
    rule_id: RuleCode | None = None


class EvidenceArgs(ToolArgs):
    metric: Annotated[str, Field(min_length=1, max_length=64)]
    actual: float
    target: float
    window_preset: WindowPreset


class ProposalUrgencyArgs(StrEnum):
    CRITICAL = "critical"
    RECOMMENDED = "recommended"
    MINOR = "minor"


class ProposeBudgetChangeArgs(ToolArgs):
    business_id: BusinessId
    entity_ref: EntityRefStr
    new_daily_budget_amount: Annotated[str, Field(pattern=r"^\d+(\.\d{1,2})?$")]
    new_daily_budget_currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")] = "EUR"
    cause: CauseArgs
    evidence: list[EvidenceArgs] = Field(default_factory=list)
    urgency: ProposalUrgencyArgs = ProposalUrgencyArgs.RECOMMENDED


class ProposePauseArgs(ToolArgs):
    business_id: BusinessId
    entity_ref: EntityRefStr
    cause: CauseArgs
    evidence: list[EvidenceArgs] = Field(default_factory=list)
    urgency: ProposalUrgencyArgs = ProposalUrgencyArgs.RECOMMENDED


class ProposeAdChildArgs(ToolArgs):
    business_id: BusinessId
    entity_ref: EntityRefStr
    child_plan: ChildPlanArgs
    cause: CauseArgs

    @model_validator(mode="before")
    @classmethod
    def _reject_urls_in_raw_strings(cls, data: Any) -> Any:  # noqa: ANN401
        return reject_urls_except_child_plan(data)

    @model_validator(mode="after")
    def validate_plan(self) -> ProposeAdChildArgs:
        validate_child_payload(
            {"child_plan": self.child_plan.model_dump(mode="json")},
            EntityRef.parse(self.entity_ref),
        )
        return self


class ProposeTargetingChangeArgs(ToolArgs):
    business_id: BusinessId
    entity_ref: EntityRefStr
    targeting_diff: dict[str, Any]
    cause: CauseArgs
    evidence: list[EvidenceArgs] = Field(default_factory=list)
    urgency: ProposalUrgencyArgs = ProposalUrgencyArgs.RECOMMENDED


class ProposeCreativePublicationArgs(ToolArgs):
    business_id: BusinessId
    ad_set_ref: EntityRefStr
    creative_asset_ids: Annotated[list[OpaqueId], Field(min_length=1, max_length=20)]
    ad_copy: dict[str, Any]
    cause: CauseArgs


# 004 tasks-2.md W2 (historia 16-17): las 5 propuestas de optimizacion que
# faltaban en el mapa `WriteOperation` <-> herramienta.

_MAX_NEGATIVE_KEYWORDS = 50
_MAX_NEGATIVE_KEYWORD_LENGTH = 80
_MONEY_AMOUNT_PATTERN = r"^\d+(\.\d{1,2})?$"
_CURRENCY_CODE_PATTERN = r"^[A-Z]{3}$"


class ProposeResumeArgs(ToolArgs):
    business_id: BusinessId
    entity_ref: EntityRefStr
    cause: CauseArgs
    evidence: list[EvidenceArgs] = Field(default_factory=list)
    urgency: ProposalUrgencyArgs = ProposalUrgencyArgs.RECOMMENDED


class ProposeBidTargetArgs(ToolArgs):
    business_id: BusinessId
    entity_ref: EntityRefStr
    bid_target_amount: Annotated[str, Field(pattern=_MONEY_AMOUNT_PATTERN)]
    bid_target_currency: Annotated[str, Field(pattern=_CURRENCY_CODE_PATTERN)] = "EUR"
    cause: CauseArgs
    evidence: list[EvidenceArgs] = Field(default_factory=list)
    urgency: ProposalUrgencyArgs = ProposalUrgencyArgs.RECOMMENDED


class ProposeNegativeKeywordsArgs(ToolArgs):
    business_id: BusinessId
    entity_ref: EntityRefStr
    keywords: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=_MAX_NEGATIVE_KEYWORD_LENGTH)]],
        Field(min_length=1, max_length=_MAX_NEGATIVE_KEYWORDS),
    ]
    cause: CauseArgs
    evidence: list[EvidenceArgs] = Field(default_factory=list)
    urgency: ProposalUrgencyArgs = ProposalUrgencyArgs.RECOMMENDED


class ProposeCreativeRotationArgs(ToolArgs):
    business_id: BusinessId
    entity_ref: EntityRefStr
    cause: CauseArgs
    evidence: list[EvidenceArgs] = Field(default_factory=list)
    urgency: ProposalUrgencyArgs = ProposalUrgencyArgs.RECOMMENDED


class ProposeDeleteArgs(ToolArgs):
    business_id: BusinessId
    entity_ref: EntityRefStr
    cause: CauseArgs
    evidence: list[EvidenceArgs] = Field(default_factory=list)
    urgency: ProposalUrgencyArgs = ProposalUrgencyArgs.RECOMMENDED


# 004 tasks-2.md W3 (historia 20): escritura nativa, siempre con aprobacion.

_NATIVE_WRITE_OPERATION_PATTERN = r"^[a-z_]{1,40}$"
_MIN_NATIVE_WRITE_WHY_LENGTH = 40


class ProposeNativeWriteArgs(ToolArgs):
    business_id: BusinessId
    entity_ref: EntityRefStr
    platform: PlatformCode
    operation: Annotated[str, Field(pattern=_NATIVE_WRITE_OPERATION_PATTERN)]
    payload: dict[str, Any]
    why: Annotated[str, Field(min_length=_MIN_NATIVE_WRITE_WHY_LENGTH, max_length=140)]

    @model_validator(mode="after")
    def _validate_payload(self) -> ProposeNativeWriteArgs:
        try:
            validate_native_write_payload(self.payload)
        except NativeWritePayloadError as exc:
            raise ValueError(str(exc)) from exc
        return self


# 004 tasks-2.md W4 (historia 13): subida de creatividad en base64 (D-2: sin
# URL, `ToolArgs` ya rechaza cualquier esquema de URL en un string).

_MAX_CREATIVE_UPLOAD_BYTES = 8 * 1024 * 1024
# M-4: tope barato (longitud de string, sin decodificar) por delante del
# tope preciso de bytes decodificados de abajo -- ~9 MiB en base64 para
# un limite decodificado de 8 MiB, con margen para el overhead del propio
# base64. Cierra el hueco junto con el middleware de content-length de
# `http.py` (defensa en profundidad: cuerpo HTTP -> string -> bytes).
_MAX_CREATIVE_UPLOAD_BASE64_CHARS = 12_000_000
_MAX_NATIVE_TOOL_USED_LENGTH = 120


class UploadCreativeAssetArgs(ToolArgs):
    business_id: BusinessId
    brief_id: OpaqueId
    media_kind: CreativeMediaKind
    content_base64: Annotated[
        str, Field(min_length=1, max_length=_MAX_CREATIVE_UPLOAD_BASE64_CHARS)
    ]
    native_tool_used: Annotated[str, Field(min_length=1, max_length=_MAX_NATIVE_TOOL_USED_LENGTH)]

    # M-4: el contenido se decodifica UNA sola vez aqui, cacheado en un
    # atributo privado (`PrivateAttr` no lo toca `frozen=True`, a
    # diferencia de un campo publico) -- el handler
    # (`creative_upload_tools.py`) reutiliza `decoded_content()` en vez de
    # decodificar el mismo base64 una segunda vez.
    _decoded_content: bytes = PrivateAttr()

    @model_validator(mode="after")
    def _content_decodes_within_limit(self) -> UploadCreativeAssetArgs:
        try:
            decoded = base64.b64decode(self.content_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("content_base64 no es base64 valido") from exc
        if len(decoded) > _MAX_CREATIVE_UPLOAD_BYTES:
            raise ValueError(
                f"la imagen decodificada supera el tope de {_MAX_CREATIVE_UPLOAD_BYTES} bytes"
            )
        self._decoded_content = decoded
        return self

    def decoded_content(self) -> bytes:
        return self._decoded_content


class WithdrawProposalArgs(ToolArgs):
    business_id: BusinessId
    proposal_id: OpaqueId
    reason: Annotated[str, Field(max_length=280)] | None = None


class ApplyDefensiveActionKindArgs(StrEnum):
    LOWER_BUDGET = "lower_budget"
    PAUSE = "pause"
    ADD_NEGATIVE_KEYWORD = "add_negative_keyword"
    ROTATE_OUT_CREATIVE = "rotate_out_creative"


class ApplyDefensiveActionArgs(ToolArgs):
    business_id: BusinessId
    entity_ref: EntityRefStr
    rule_id: RuleCode
    action: ApplyDefensiveActionKindArgs
    cause: CauseArgs
    magnitude_pct: Annotated[float, Field(gt=0, le=1)] | None = None
    keyword: Annotated[str, Field(min_length=1, max_length=80)] | None = None
