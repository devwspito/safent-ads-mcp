"""`DecisionLogEntry` (data-model.md §DecisionLogEntry, threat-model.md
C-19): la fila persistida y el sobre `PendingDecision` que cualquier
contexto entrega para anexar una entrada. El agregado no depende de SQL ni
de la biblioteca de cifrado: solo tipos del kernel compartido y validacion
propia."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from safent_ads.shared.errors import DomainError
from safent_ads.shared.ids import BusinessId, EntityRef

JsonValue = str | int | float | bool | None | Sequence["JsonValue"] | Mapping[str, "JsonValue"]

# Claves que jamas deben aparecer en un payload de decision_log
# (data-model.md: "el payload no contiene PII ni secretos"). Coincidencia
# exacta, no por subcadena: un match por subcadena de "key" rechazaria
# campos de negocio legitimos como "cause_key".
_FORBIDDEN_PAYLOAD_KEYS = frozenset(
    {
        "password",
        "password_hash",
        "secret",
        "totp_secret",
        "totp_secret_encrypted",
        "token",
        "token_hash",
        "authorization",
        "api_key",
        "credit_card",
        "ssn",
        "email",
        "phone",
    }
)


class DecisionLogPayloadError(DomainError):
    """El payload de una entrada de `decision_log` contiene una clave
    prohibida (PII o secreto)."""


class DecisionKind(StrEnum):
    """Tipos de decision que cruzan al `decision_log` (tasks.md T010)."""

    SIGNAL = "signal"
    PROPOSAL = "proposal"
    APPROVAL = "approval"
    EXECUTION = "execution"
    UNDO = "undo"
    RULE_CHANGE = "rule_change"
    GUARDRAIL_CHANGE = "guardrail_change"
    BRAND_CLAIMS_UPDATED = "brand_claims_updated"
    BRAKE = "brake"
    LOGIN = "login"
    LOGIN_FAILED = "login_failed"
    LOGOUT = "logout"
    TELEGRAM_PAIRING_SUCCEEDED = "telegram_pairing_succeeded"
    TELEGRAM_PAIRING_FAILED = "telegram_pairing_failed"
    TELEGRAM_CALLBACK_UNPAIRED = "telegram_callback_unpaired"
    TELEGRAM_UNPAIRED = "telegram_unpaired"
    TELEGRAM_UNPAIRED_COMMAND_DENIED = "telegram_unpaired_command_denied"
    CREDENTIAL_HEALTH_CHANGED = "credential_health_changed"
    PLATFORM_APP_CREDENTIALS_SET = "platform_app_credentials_set"
    PLATFORM_APP_CREDENTIALS_DELETED = "platform_app_credentials_deleted"
    CUSTOMER_FORGOTTEN = "customer_forgotten"
    CRM_BRIDGE_HEALTH_RECORDED = "crm_bridge_health_recorded"
    # M3 (repaso de seguridad 0.2.23): `packages.presentation.rest` --
    # `reject`/`resume` no dejaban ningun rastro queryable (solo un log de
    # structlog para `resume`, nada en absoluto para `reject`); `undo`
    # dejaba pasar la firma de `Authorization` de `PauseEntity` (persona,
    # solo en la rama `campaign_paused`) pero nunca `reason`, en ninguna
    # rama. Las tres rutas ahora anexan al `decision_log`, igual que
    # `approve` ya hace via su propia `Authorization` firmada.
    PACKAGE_REJECTED = "package_rejected"
    PACKAGE_RESUMED = "package_resumed"
    # 004 tasks.md A7: una fila por llamada MCP de un puesto de anuncios,
    # exito o fallo (contracts/mcp.md §4 paso 8). `actor_id` lleva
    # `person:<user_id>`, `actor_kind='agent'` (ya en el CHECK).
    MCP_TOOL_CALL = "mcp_tool_call"
    # Revision de seguridad de la conexion Cloudflare (2026-09-15,
    # hallazgo medio): conectar/desconectar el token de Cloudflare no
    # dejaba ningun rastro queryable, mismo criterio que
    # `PLATFORM_APP_CREDENTIALS_SET`/`_DELETED` -- payload lleva
    # `account_id`/`zone_count`, nunca el token.
    CLOUDFLARE_CONNECTED = "cloudflare_connected"
    CLOUDFLARE_DISCONNECTED = "cloudflare_disconnected"
    # spec 008 T031 (auditoria doble de los topes desde el panel): el
    # broker ya deja `broker_account_caps_set`/`_denied` en SU registro --
    # la traza que un compromiso de `ads-api` no puede borrar --; estas dos
    # son la narrativa del panel, encadenada como cualquier otra decision
    # del dueno. `event_type` es TEXT sin CHECK desde `0002_audit_chain`,
    # asi que anadirlas NO necesita migracion (punto 3 del plan de
    # migracion de `data-model.md`, verificado antes de escribir T031).
    ACCOUNT_HARD_CAPS_SET = "account_hard_caps_set"
    ACCOUNT_HARD_CAPS_DELETED = "account_hard_caps_deleted"


class ActorKind(StrEnum):
    """Debe coincidir con el `CHECK` de `0002_audit_chain` byte a byte."""

    OWNER = "owner"
    RULE_ENGINE = "rule_engine"
    AGENT = "agent"
    SYSTEM = "system"


def _assert_no_forbidden_keys(payload: Mapping[str, JsonValue]) -> None:
    for key, value in payload.items():
        if key.lower() in _FORBIDDEN_PAYLOAD_KEYS:
            raise DecisionLogPayloadError(f"clave prohibida en payload de decision_log: {key!r}")
        if isinstance(value, Mapping):
            _assert_no_forbidden_keys(value)
        elif isinstance(value, Sequence) and not isinstance(value, str):
            for item in value:
                if isinstance(item, Mapping):
                    _assert_no_forbidden_keys(item)


@dataclass(frozen=True, slots=True, kw_only=True)
class PendingDecision:
    """Lo que cualquier contexto entrega para anexar una decision. Sin
    `seq`/`prev_hash`/`entry_hash`: esos los calcula la base de datos."""

    business_id: BusinessId
    kind: DecisionKind
    actor_kind: ActorKind
    payload: Mapping[str, JsonValue]
    entity_ref: EntityRef | None = None
    actor_id: str | None = None
    proposal_id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        _assert_no_forbidden_keys(self.payload)


@dataclass(frozen=True, slots=True, kw_only=True)
class DecisionLogEntry:
    """Fila ya persistida y encadenada."""

    seq: int
    business_id: BusinessId
    kind: DecisionKind
    actor_kind: ActorKind
    payload: Mapping[str, JsonValue]
    prev_hash: str
    entry_hash: str
    occurred_at: datetime
    entity_ref: EntityRef | None = None
    actor_id: str | None = None
    proposal_id: uuid.UUID | None = None
