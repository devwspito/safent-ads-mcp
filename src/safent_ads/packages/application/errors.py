"""Errores tipados de `packages.application` (contracts/mcp-tools.md §4,
contracts/api.md §4). Cada uno lleva el `code` estable del contrato para
que MCP (`mcp/presentation/package_tools.py`) y REST
(`packages/presentation/rest.py`) lo traduzcan de forma independiente --
ninguna de las dos capas de presentacion decide el codigo, solo lo copia
(mismo principio que `mcp.application.errors.ToolDispatchError`, pero sin
que `packages.application` dependa de `mcp`)."""

from __future__ import annotations

from safent_ads.shared.errors import ApplicationError


class PackageApplicationError(ApplicationError):
    """Raiz de los errores tipados de casos de uso de `packages`."""

    code: str = "UNKNOWN"


class PackageStructureInvalidError(PackageApplicationError):
    code = "PACKAGE_STRUCTURE_INVALID"


class CreativeNotUsableError(PackageApplicationError):
    code = "CREATIVE_NOT_USABLE"

    def __init__(self, *, ad_local_ref: str, reason: str = "creative_not_usable") -> None:
        super().__init__(f"{ad_local_ref}: {reason}")
        self.ad_local_ref = ad_local_ref
        self.reason = reason


class LandingUrlNotAllowedError(PackageApplicationError):
    code = "LANDING_URL_NOT_ALLOWED"

    def __init__(self, *, ad_local_ref: str, expected_domains: frozenset[str]) -> None:
        super().__init__(f"{ad_local_ref}: fuera de {sorted(expected_domains)}")
        self.ad_local_ref = ad_local_ref
        self.expected_domains = expected_domains


class BudgetEnvelopeExceededError(PackageApplicationError):
    code = "BUDGET_ENVELOPE_EXCEEDED"

    def __init__(self, *, headroom: str, requested: str) -> None:
        super().__init__(f"headroom={headroom} requested={requested}")
        self.headroom = headroom
        self.requested = requested


class DailyBudgetExceedsCapError(PackageApplicationError):
    code = "DAILY_BUDGET_EXCEEDS_CAP"


class AmbiguousAccountError(PackageApplicationError):
    code = "AMBIGUOUS_ACCOUNT"

    def __init__(self, *, candidates: tuple[str, ...]) -> None:
        super().__init__(f"candidatos: {candidates}")
        self.candidates = candidates


class OfferingNotFoundError(PackageApplicationError):
    code = "OFFERING_NOT_FOUND"


class NoActiveAccountForPlatformError(PackageApplicationError):
    """No hay ninguna cuenta `ACTIVE` del negocio en esa plataforma.
    Codigo propio (`contracts/mcp-tools.md §4` no cubre este caso, solo el
    de varias cuentas): sin el, el modelo no puede distinguir "ninguna
    cuenta" de un fallo generico."""

    code = "NO_ACTIVE_ACCOUNT"


class PlatformNativeIncompleteError(PackageApplicationError):
    code = "PLATFORM_NATIVE_INCOMPLETE"

    def __init__(self, *, missing: tuple[str, ...]) -> None:
        super().__init__(f"falta: {missing}")
        self.missing = missing


class DuplicateOpenPackageError(PackageApplicationError):
    code = "DUPLICATE_OPEN_PACKAGE"

    def __init__(self, *, package_id: str) -> None:
        super().__init__(f"paquete abierto: {package_id}")
        self.package_id = package_id


# --- Tipos de campana de Google (contracts/mcp-tools.md §5) -----
#
# `CHANNEL_TYPE_NOT_SUPPORTED` no vive aqui (revision 0.2.24): el discriminador
# de canal de `ProposeCampaignPackageArgs.campaign.native` es una union
# cerrada de cuatro `Literal`, `extra="forbid"`/`strict=True` -- un canal
# fuera de la tabla nunca pasa la validacion de Pydantic, asi que ningun
# camino de `propose_campaign_package` podia levantar este codigo (ver
# `contracts/mcp-tools.md §5`, fila retirada).


class ChannelTypeNotEnabledError(PackageApplicationError):
    """tasks.md T076 (POLISH, "canales por configuracion"): distinto de
    `CHANNEL_TYPE_NOT_SUPPORTED` de arriba -- el canal SI es una de las
    cuatro filas de `GoogleAdvertisingChannelType` (pasa el discriminador),
    pero esta instalacion no lo tiene encendido
    (`ADS_GOOGLE_CHANNELS_ENABLED`, settings.py; el gate de seguridad T035
    publica DISPLAY/DEMAND_GEN/PERFORMANCE_MAX apagados). `package_tools.py`
    lo levanta pronto, antes de cualquier E/S (`mcp.presentation.
    campaign_creation_args.require_enabled_google_channel`); `execute()`
    de abajo lo repite en profundidad por si algun otro llamador construye
    el arbol sin pasar por ese borde."""

    code = "CHANNEL_TYPE_NOT_ENABLED"

    def __init__(self, *, channel: str) -> None:
        super().__init__(channel)
        self.channel = channel


class BiddingNotAllowedForChannelError(PackageApplicationError):
    code = "BIDDING_NOT_ALLOWED_FOR_CHANNEL"

    def __init__(self, *, channel: str, allowed: tuple[str, ...]) -> None:
        super().__init__(f"{channel}: permitidas {allowed}")
        self.channel = channel
        self.allowed = allowed


class ConversionActionRequiredError(PackageApplicationError):
    code = "CONVERSION_ACTION_REQUIRED"


class ConversionActionNotUsableError(PackageApplicationError):
    """`reason` es identico para meta inexistente, pausada o de otra
    cuenta (mismo criterio anti-enumeracion que `CreativeNotUsableError`,
    003 §R2.3) -- lo verifica el bróker releyendo por GAQL (T032)."""

    code = "CONVERSION_ACTION_NOT_USABLE"

    def __init__(self, *, reason: str = "conversion_action_not_usable") -> None:
        super().__init__(reason)
        self.reason = reason


class AssetGroupIncompleteError(PackageApplicationError):
    code = "ASSET_GROUP_INCOMPLETE"

    def __init__(self, *, missing: tuple[str, ...]) -> None:
        super().__init__(f"falta: {missing}")
        self.missing = missing


class CreativeAspectRatioInvalidError(PackageApplicationError):
    code = "CREATIVE_ASPECT_RATIO_INVALID"

    def __init__(self, *, expected: str, ad_local_ref: str) -> None:
        super().__init__(f"{ad_local_ref}: se esperaba {expected}")
        self.expected = expected
        self.ad_local_ref = ad_local_ref


class DailyBudgetBelowChannelMinimumError(PackageApplicationError):
    code = "DAILY_BUDGET_BELOW_CHANNEL_MINIMUM"

    def __init__(self, *, minimum: str, channel: str) -> None:
        super().__init__(f"{channel}: minimo {minimum}")
        self.minimum = minimum
        self.channel = channel


class DurationBelowChannelMinimumError(PackageApplicationError):
    code = "DURATION_BELOW_CHANNEL_MINIMUM"

    def __init__(self, *, minimum: int) -> None:
        super().__init__(f"minimo {minimum} dias")
        self.minimum = minimum


# --- Decidir (contracts/api.md §4) -----------------------------------------


class PackageNotFoundError(PackageApplicationError):
    """`404 NOT_FOUND` -- tambien para un paquete de otro negocio (nunca se
    distingue, contracts/api.md §2)."""

    code = "NOT_FOUND"


class PackageNotProposedError(PackageApplicationError):
    code = "PACKAGE_NOT_PROPOSED"


class PackageExpiredError(PackageApplicationError):
    code = "PACKAGE_EXPIRED"


class PackageChangedError(PackageApplicationError):
    """`package_hash` enviado != el vivo (invariante 8 en el borde HTTP)."""

    code = "PACKAGE_CHANGED"


class PackageBrakeEngagedError(PackageApplicationError):
    code = "BRAKE_ENGAGED"


class PackageCreativeUnavailableError(PackageApplicationError):
    code = "CREATIVE_UNAVAILABLE"

    def __init__(self, *, ad_local_ref: str) -> None:
        super().__init__(ad_local_ref)
        self.ad_local_ref = ad_local_ref


class PackageGuardrailBlockedError(PackageApplicationError):
    code = "GUARDRAIL_BLOCKED"

    def __init__(self, *, reasons: tuple[str, ...]) -> None:
        super().__init__(",".join(reasons))
        self.reasons = reasons


class PackageNotEditableError(PackageApplicationError):
    """`replace_ad_creative`/`owner-context` fuera de `proposed`/`approved`."""

    code = "PACKAGE_NOT_EDITABLE"


# --- Publicar / reanudar / deshacer (contracts/api.md §R2.C/§5) ------------


class PublicationNotFoundError(PackageApplicationError):
    """Un paquete `approved` en adelante siempre tiene publicacion; si no
    la tiene, algo esta corrupto -- nunca un 404 de negocio."""

    code = "PUBLICATION_NOT_FOUND"


class PackageNotResumableError(PackageApplicationError):
    """`resume` solo desde `partially_published`/`verifying`."""

    code = "PACKAGE_NOT_RESUMABLE"


class PackageApprovalExpiredError(PackageApplicationError):
    """El sobre humano caduco: hay que volver a aprobar sobre la misma
    huella (AL-2/AL-6) -- reanudar nunca re-acuña la firma en silencio."""

    code = "PACKAGE_APPROVAL_EXPIRED"


class UndoWindowClosedError(PackageApplicationError):
    code = "UNDO_WINDOW_CLOSED"


class UndoNoConfirmedCampaignError(PackageApplicationError):
    """M5 (revision de codigo): distinto de `UNDO_WINDOW_CLOSED` -- aqui la
    ventana de deshacer sigue abierta (el paquete esta `published`/
    `partially_published`), pero todavia no hay un recibo `done` de la
    campana que pausar. Confundirlo con "ventana cerrada" le diria al
    dueño que ya no puede deshacer cuando en realidad solo tiene que
    esperar a que se confirme la campana."""

    code = "UNDO_NO_CONFIRMED_CAMPAIGN"


class UndoPauseDeniedError(PackageApplicationError):
    """M5 (revision de codigo): distinto de `UNDO_WINDOW_CLOSED` -- la
    campana SI esta confirmada, pero `PauseEntity` la denego (freno,
    guardarrail, entidad ya en otro estado). Es un fallo del paso de
    pausa, no una ventana temporal cerrada."""

    code = "UNDO_PAUSE_DENIED"
