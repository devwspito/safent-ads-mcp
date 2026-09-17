"""Puertos de `packages` (tasks.md T020; data-model.md "Bounded contexts":
`packages -> {proposals, execution, creative, accounts, shared}`, ninguna
flecha de vuelta).

`packages` no importa `catalog`/`brand`/`rules`/`opportunities`/`mcp`: esos
contextos son "fuentes de solo lectura para el AGENTE" (data-model.md), no
dependencias de este contexto. Los adaptadores que necesitan sus datos
(oferta, marca, tope diario, sobre de presupuesto) leen SQL directamente
sobre sus tablas fisicas -- el mismo criterio ya usado por
`opportunities/infrastructure/sql_repositories.py` para `guardrails`/
`platform_accounts` -- en vez de importar el modulo Python de ese contexto.

Los puertos que ya existen en `execution.application.ports`
(`GuardrailSetRepository`, `BrakeStatePort`, `SpendLedger`) y los tipos de
`execution.domain.guardrails` se REUTILIZAN tal cual: `execution` SI esta
en la lista de dependencias permitidas y `ApproveCampaignPackage` evalua
los MISMOS guardarrailes que `SubmitApproval` (T022)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from safent_ads.packages.domain.approval_envelope import PackageApprovalEnvelope
from safent_ads.packages.domain.campaign_package import CampaignPackage, PackageState
from safent_ads.packages.domain.identifiers import OfferingId, PackageId
from safent_ads.packages.domain.planned_tree import AdRef
from safent_ads.packages.domain.step_binding import PackageStepBinding
from safent_ads.proposals.domain.authorization import Authorization
from safent_ads.proposals.domain.money import Money
from safent_ads.shared.ids import BusinessId, EntityRef, PlatformCode

__all__ = [
    "AccountDailyCapPort",
    "ActiveAccountLookupPort",
    "AdRef",
    "AmbiguousActiveAccountForPlatformError",
    "BudgetEnvelopeReadPort",
    "CampaignPackageRepository",
    "CreativeAssetBytesPort",
    "CreativeAssetLookupPort",
    "CreativeAssetSnapshot",
    "LandingDomainPolicyPort",
    "OfferingExistsPort",
    "PackageAuthorizationRepository",
    "PackageBudgetEnvelope",
    "PackageListItem",
    "PackagePublicationRecord",
    "PackagePublicationRepository",
    "PackageStepExecutorPort",
    "PackageStepRecord",
    "PackageStepRepository",
    "PublishAsLookupPort",
    "ResolvedPublishAs",
    "StepExecutionOutcome",
]


# ---------------------------------------------------------------------------
# CampaignPackageRepository (T020, T027)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True, slots=True)
class PackageListItem:
    """Fila de `GET /packages` (contracts/api.md §3) -- proyeccion ligera,
    sin reconstruir el arbol completo."""

    package_id: PackageId
    state: PackageState
    platform: PlatformCode
    account_name: str | None
    campaign_name: str
    ads_count: int
    daily_budget: Money
    total_cap: Money
    created_at: datetime
    expires_at: datetime


class CampaignPackageRepository(Protocol):
    """`packages -> {proposals, execution, creative, accounts, shared}`.
    El adaptador SQL real **recalcula** `package_hash` al leer -- nunca se
    fia del valor almacenado (invariante 8, mismo criterio que
    `SqlProposalRepository` con `diff_hash`)."""

    async def add(self, package: CampaignPackage) -> None:
        """Inserta un paquete recien propuesto. Lanza
        `DuplicateOpenPackageError` (traducida desde la violacion del
        indice unico parcial `ix_campaign_packages_open_dedup`) si ya hay
        uno abierto para `(business_id, account_ref, offering_id)`."""
        ...

    async def save(self, package: CampaignPackage) -> None:
        """Persiste una transicion de estado o una edicion del arbol
        (`replace_ad_creative`) sobre un paquete YA existente."""
        ...

    async def get(
        self, package_id: PackageId, *, business_id: BusinessId
    ) -> CampaignPackage | None:
        """`None` si no existe o pertenece a otro negocio -- mismo `404`
        uniforme en el borde (contracts/api.md §2)."""
        ...

    async def find_open_duplicate(
        self, *, business_id: BusinessId, account_ref: EntityRef, offering_id: OfferingId
    ) -> PackageId | None:
        """Comprobacion proactiva de FR-20 antes de escribir -- el indice
        unico parcial de la base es la fuente de verdad final, esto solo
        evita una vuelta redonda a la base para el caso feliz de
        `DUPLICATE_OPEN_PACKAGE`."""
        ...

    async def list_for_business(
        self,
        *,
        business_id: BusinessId,
        state: PackageState | None,
        limit: int,
        cursor: str | None,
    ) -> tuple[tuple[PackageListItem, ...], str | None]:
        """`(items, next_cursor)` -- paginacion por clave sobre
        `created_at DESC` (mismo indice que sirve la deduplicacion)."""
        ...


# ---------------------------------------------------------------------------
# PackagePublicationRepository (T020, T027) -- el sobre firmado se crea al
# aprobar (T022); ejecutarlo paso a paso es `RunPackagePublication` (T023),
# deliberadamente fuera de esta entrega (depende de las correcciones de
# seguridad de `broker`/`execution` en curso en otra rama).
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True, slots=True)
class PackagePublicationRecord:
    """Lo que `ApproveCampaignPackage` persiste al firmar (data-model.md
    `PackagePublication`, Revision 2 §R2.2/§R2.10): el sobre firmado
    ENTERO, su firma, su huella y el plan archivado inmutable
    (`approved_plan`, B-R1). `RunPackagePublication` (T023, siguiente
    entrega) es quien avanza `cursor`/`state` paso a paso; esta entrega
    solo crea la fila `pending`, cursor 0."""

    publication_id: str
    package_id: PackageId
    authorization_id: str
    state: str
    cursor: int
    envelope: PackageApprovalEnvelope
    envelope_hash: str
    approval_signature: bytes
    approval_expires_at: datetime
    approved_plan: dict[str, object]
    started_at: datetime
    halt_reason: str | None = None
    failed_step_index: int | None = None
    finished_at: datetime | None = None


class PackagePublicationRepository(Protocol):
    async def add(self, record: PackagePublicationRecord) -> None: ...

    async def get_by_package_id(self, package_id: PackageId) -> PackagePublicationRecord | None: ...

    async def get_by_id(self, publication_id: str) -> PackagePublicationRecord | None:
        """`RunPackagePublication` opera por `publication_id` (el bucle de
        `ads-worker` lo trae de `list_open()`), no por `package_id`.

        M1 (revision de codigo): `SELECT ... FOR UPDATE SKIP LOCKED` --
        reclama la fila para esta transaccion; si otro worker (dos
        replicas del mismo bucle) ya la tiene bloqueada, devuelve `None`
        en vez de esperar. El llamante lo trata igual que "no existe"
        (`PublicationNotFoundError`), que aqui significa "no disponible
        AHORA", no "nunca existio" -- el siguiente tick la vuelve a
        intentar cuando la otra transaccion libere el bloqueo."""
        ...

    async def advance(
        self,
        publication_id: str,
        *,
        cursor: int,
        state: str,
        halt_reason: str | None,
        failed_step_index: int | None,
        finished_at: datetime | None,
    ) -> None:
        """Avanza el cursor/estado de la publicacion (T023). El trigger de
        `campaign_package_publications_guard` (0042) sigue siendo la
        fuente de verdad de que transiciones son un solo salto -- este
        metodo solo persiste lo que `RunPackagePublication` ya decidio."""
        ...

    async def list_open(self) -> tuple[str, ...]:
        """`publication_id` de toda publicacion `pending`/`running`, de
        CUALQUIER negocio -- lo que el bucle de `ads-worker` recorre, un
        paso por publicacion y por ciclo (T029)."""
        ...


# ---------------------------------------------------------------------------
# PackageAuthorizationRepository -- persiste la UNA `Authorization` humana
# de sujeto "package" que firma `ApproveCampaignPackage` (T022, data-model.md
# Revision 2 §R2.5). No es `proposals.application.ports.AuthorizationRepository`:
# esa asume `proposal_id` no nulo en su unico camino probado hoy: separar el
# adaptador evita tocar `proposals/infrastructure` (fuera de esta lane) para
# soportar un `subject` que esa clase todavia no sabe serializar.
# ---------------------------------------------------------------------------


class PackageAuthorizationRepository(Protocol):
    async def save(self, authorization: Authorization) -> None: ...


# ---------------------------------------------------------------------------
# CreativeAssetLookupPort (T020, T028)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True, slots=True)
class CreativeAssetSnapshot:
    """Lo que `packages` necesita de un `creative.domain.CreativeAsset` ya
    verificado (negocio, `READY`, veredicto `PASS`) para construir un
    `ImageCreativeRef` (T012). `packages` nunca copia bytes -- solo esta
    proyeccion (data-model.md "packages referencia CreativeAsset por
    AssetId, no lo copia")."""

    asset_id: str
    checksum: str
    mime_type: str
    width: int
    height: int
    preview_key: str


class CreativeAssetLookupPort(Protocol):
    async def find_usable(
        self, *, business_id: BusinessId, asset_id: str
    ) -> CreativeAssetSnapshot | None:
        """`None` para TODOS los casos de rechazo (inexistente, de otro
        negocio, no `READY`, sin veredicto `PASS`) -- mismo `details.reason`
        para los cuatro en el borde (contracts/mcp-tools.md Revision 2
        §R2.3, ME-7): enumerar activos ajenos por diferencia de mensajes
        deja de ser posible."""
        ...


# ---------------------------------------------------------------------------
# LandingDomainPolicyPort (T020, T021) -- allow-list explicita de hosts
# permitidos como destino de un anuncio. Assumption documentada: hoy solo
# se resuelve contra el sitio confirmado de la MARCA
# (`brand_kits.confirmed_website_host`); `catalog.domain.offering.
# OfferingDetails` no modela ninguna URL de oferta todavia, asi que la
# mitad de la regla de contracts/mcp-tools.md Revision 2 §R2.3
# ("host de la landing de la oferta O del sitio de la marca") queda
# pendiente de que esa clase gane un campo -- fuera del alcance de
# `packages` (`catalog` no es una dependencia permitida).
# ---------------------------------------------------------------------------


class LandingDomainPolicyPort(Protocol):
    async def allowed_hosts(self, *, business_id: BusinessId) -> frozenset[str]: ...


# ---------------------------------------------------------------------------
# ActiveAccountLookupPort / AccountDailyCapPort / OfferingExistsPort
# (T021) -- mismo contrato que `opportunities.application.ports`, declarado
# aqui de nuevo (nunca importado desde `opportunities`, que no es una
# dependencia permitida de `packages`) para no crear un acoplamiento
# lateral entre dos contextos hermanos.
# ---------------------------------------------------------------------------


class AmbiguousActiveAccountForPlatformError(Exception):
    """Mas de una cuenta `ACTIVE` del negocio en esa plataforma y
    `account_ref` nulo -- `AMBIGUOUS_ACCOUNT` (contracts/mcp-tools.md §4).
    No hereda de `PackageDomainError`: no es una violacion de invariante,
    es la forma en que este puerto reporta una consulta sin respuesta
    unica (mismo criterio que la excepcion homonima de `opportunities`)."""


class ActiveAccountLookupPort(Protocol):
    async def find_active_account(
        self,
        *,
        business_id: BusinessId,
        platform: PlatformCode,
        account_ref: EntityRef | None = None,
    ) -> EntityRef | None: ...


class AccountDailyCapPort(Protocol):
    async def get_daily_cap(self, *, account_ref: EntityRef) -> Money | None: ...


class OfferingExistsPort(Protocol):
    async def exists(self, *, business_id: BusinessId, offering_id: str) -> bool: ...


# ---------------------------------------------------------------------------
# BudgetEnvelopeReadPort (T021) -- forma propia (nunca
# `mcp.application.search_terms_ports.BudgetEnvelope`: `mcp` no es una
# dependencia permitida de `packages`, y `mcp` SI depende de `packages`
# para montar `propose_campaign_package` -- importar de vuelta crearia un
# ciclo). `composition/app.py` adapta el `SqlBudgetEnvelopeReadPort` ya
# construido a esta forma; la logica de calculo no se duplica.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True, slots=True)
class PackageBudgetEnvelope:
    monthly_cap_minor: int | None
    spent_month_to_date_minor: int | None
    headroom_minor: int | None
    currency: str | None
    reason: str | None


class BudgetEnvelopeReadPort(Protocol):
    async def get_budget_envelope(self, business_id: str) -> PackageBudgetEnvelope: ...


# ---------------------------------------------------------------------------
# PublishAsLookupPort (T101) -- la pagina de Meta ya conectada desde la que
# se publica. Nota de implementacion (gap descubierto en esta entrega): el
# esquema de `accounts` (0001_bootstrap.py) no guarda ninguna pagina de
# Meta asociada a una conexion todavia -- no existe una fuente real que
# resolver. El puerto queda declarado y `ProposeCampaignPackage` lo llama
# de verdad para Meta (falla con `PLATFORM_NATIVE_INCOMPLETE` si no hay
# resolucion, nunca inventa una pagina); el adaptador de produccion es un
# `NullPublishAsLookupPort` documentado hasta que exista esa infraestructura
# (escalado en el informe de esta rama)."""
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True, slots=True)
class ResolvedPublishAs:
    page_id: str
    page_name: str


class PublishAsLookupPort(Protocol):
    async def resolve(self, *, account_ref: EntityRef) -> ResolvedPublishAs | None: ...


# ---------------------------------------------------------------------------
# PackageStepExecutorPort (T020) -- adaptador de `chokepoint_step_executor.py`
# (T024, siguiente entrega: depende de T103/T109, y de las correcciones de
# seguridad de `broker`/`execution` en curso en otra rama ahora mismo).
# Se declara la FORMA ahora, sin implementacion: `RunPackagePublication`
# (T023) la consume un paso por invocacion.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True, slots=True)
class StepExecutionOutcome:
    """Lo que un paso de publicacion produce, releido SIEMPRE de la base
    tras `run_once` (Revision 2 §AL-3: `RunPackagePublication` nunca decide
    el estado del paso por el valor de retorno del chokepoint)."""

    state: str
    created_entity_ref: str | None
    outcome_code: str | None
    # Solo lo lleva el paso `ACTIVATE_CAMPAIGN` cuando `state == "done"`
    # (`UndoGracePolicy.pause_grace` ya se aplica sola: el diff de ese paso
    # es `parameter=status`, `after=ACTIVE`). `RunPackagePublication` lo usa
    # para `CampaignPackagePublished.undo_deadline`, sin recalcular nada.
    undo_deadline: datetime | None = None
    # T123/AL-4: el `WriteResult.confirmed_state_hash` del recibo de ESTE
    # paso (`None` para `UPLOAD_CREATIVE`, que no pasa por el chokepoint, y
    # para cualquier desenlace que no sea `done`). `RunPackagePublication`
    # lo archiva en `PackageStepRecord.confirmed_state_hash` para que el
    # HIJO de este paso pueda firmar su propio `expected_state_hash`
    # (data-model.md R2.8, generalizado de la activacion a todo paso con
    # padre).
    confirmed_state_hash: str | None = None
    # Revision de codigo B2 (15-sep): la `Proposal` que respalda este paso
    # (`None` para `UPLOAD_CREATIVE`, que no tiene una). `RunPackagePublication`
    # lo archiva en `PackageStepRecord.proposal_id` para que una reanudacion
    # posterior NUNCA vuelva a proponer/firmar/encolar -- reutiliza esta
    # MISMA `Proposal` y deja que `ExecutionChokepoint.run_once` reconcilie
    # el intento ya reservado bajo la clave de idempotencia estable
    # (BL-4). Sin esto, un segundo intento minta una `Proposal` distinta
    # sobre la MISMA clave `pkg-<publication_id>-<step_index>` y
    # `executions_idempotency_key_unique` lo rechaza con
    # `DuplicateExecutionError`.
    proposal_id: str | None = None


class PackageStepExecutorPort(Protocol):
    """Materializa la `Proposal` de paso (ya `approved`, gracia cero),
    firma la `Authorization` derivada `package_step` a partir del binding,
    encola el `ExecutionAttempt` y llama a
    `ExecutionChokepoint.run_once(proposal_id=...)` -- sin tocar ninguna
    plataforma directamente (T024).

    `human_authorization_id`/`human_approval_signature`: la `Authorization`
    humana (sujeto `package`) que `ApproveCampaignPackage` firmo -- ni
    `envelope` ni `binding` la llevan (son el CONTENIDO que ella firma, no
    la firma en si). `chokepoint_step_executor` los usa para construir el
    `PackageApprovalProof` que viaja con la `Authorization` derivada
    (contracts/api.md §R2.E `SignedPackageApproval`).

    `package`: el agregado VIVO, ya comprobado por `RunPackagePublication`
    contra el `package_hash` firmado (R3) antes de llegar aqui --
    `project_step_template` lo necesita para proyectar la carga exacta del
    paso; el sobre solo lleva `payload_template_hash` (una huella, no la
    carga). `resolutions`: los huecos `{creative_of:X}` ya resueltos a un
    manejador de plataforma confirmado (nunca `{parent}`, que viaja en
    `WriteCommand.entity_ref`, no en la carga)."""

    async def execute_step(
        self,
        *,
        package: CampaignPackage,
        envelope: PackageApprovalEnvelope,
        binding: PackageStepBinding,
        resolutions: dict[str, str],
        human_authorization_id: str,
        human_approval_signature: bytes,
        existing_proposal_id: str | None = None,
    ) -> StepExecutionOutcome:
        """`existing_proposal_id` (B2): la `Proposal` que un intento previo
        de ESTE MISMO paso ya materializo (`PackageStepRecord.proposal_id`),
        o `None` en el primer intento. Con un valor presente, la
        implementacion NUNCA vuelve a proponer/firmar/encolar -- solo pide
        a `ExecutionChokepoint.run_once` que reconcilie el intento ya
        reservado bajo la clave de idempotencia estable del paso."""
        ...


# ---------------------------------------------------------------------------
# PackageStepRepository (T023/T024) -- persistencia de `campaign_package_steps`.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True, slots=True)
class PackageStepRecord:
    publication_id: str
    step_index: int
    kind: str
    local_ref: str
    parent_local_ref: str | None
    state: str
    created_entity_ref: str | None = None
    proposal_id: str | None = None
    execution_id: str | None = None
    outcome_code: str | None = None
    # T123/AL-4: el `confirmed_state_hash` del recibo de ESTE paso, para que
    # `RunPackagePublication._resolve_parent` se lo pase al HIJO como
    # `PackageStepBinding.parent_receipt_state_hash`. `None` hasta que el
    # paso termine `done` (columna aditiva 0049, campaign_package_steps).
    confirmed_state_hash: str | None = None


class PackageStepRepository(Protocol):
    async def get(self, publication_id: str, step_index: int) -> PackageStepRecord | None: ...

    async def upsert(self, record: PackageStepRecord) -> None:
        """Inserta el paso `pending` la primera vez que se alcanza, o
        actualiza su desenlace despues de ejecutarlo. El trigger
        `campaign_package_steps_guard` (0042) sigue siendo quien impide un
        salto de estado invalido o sobrescribir un `created_entity_ref` ya
        confirmado."""
        ...


# ---------------------------------------------------------------------------
# CreativeAssetBytesPort (T111, UPLOAD_CREATIVE) -- los bytes reales del
# activo, para que `chokepoint_step_executor` verifique `sha256(media) ==
# checksum` firmado ANTES de subir nada (BL-6). Puerto propio, distinto de
# `CreativeAssetLookupPort`: ese devuelve METADATOS ya verificados; este
# devuelve BYTES, y solo lo necesita el paso de subida.
# ---------------------------------------------------------------------------


class CreativeAssetBytesPort(Protocol):
    async def get_bytes(self, *, business_id: BusinessId, asset_id: str) -> bytes | None:
        """`None` si el activo no existe, es de otro negocio, o no esta
        `READY`/`PASS` -- mismo criterio de `CreativeAssetLookupPort`
        (ME-7): nunca distingue el motivo por la respuesta."""
        ...
