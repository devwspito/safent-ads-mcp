"""`HardCapsService`: lo que hay detras de `set_account_caps`,
`delete_account_caps` y `resolve_account_caps` (spec 008 T030,
`contracts/broker-set-account-caps.schema.json`).

`ads-api` **pide**; el broker **decide, valida contra el sobre y escribe**.
Toda la politica vive aqui, de este lado del socket: la capa REST no puede
saltarsela porque no es ella quien la aplica -- ni siquiera tiene el
directorio de estado montado.

Auditoria del broker (`broker_account_caps_set`/`_denied`): la cuenta
canonicalizada, el antes y el despues de los tres campos, el sobre vigente,
`requested_by`, el `caps_digest` y el veredicto. Es la traza que un
compromiso de `ads-api` **no puede borrar** -- por eso se escribe aqui
ademas de en el registro de decisiones del panel."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime

import structlog

from safent_ads.broker.application.errors import (
    EnvelopeAccountsExhaustedError,
    EnvelopeChangesExhaustedError,
    EnvelopeExceededError,
    EnvelopeNotDeclaredError,
    InvalidAccountCapsError,
)
from safent_ads.broker.domain.account_key import (
    InvalidPlatformAccountIdError,
    canonical_platform_account_id,
)
from safent_ads.broker.domain.hard_caps_policy import CapsResolution, PanelAmounts
from safent_ads.broker.infrastructure.caps_config import HardCapsStatus, SpendEnvelope
from safent_ads.broker.infrastructure.caps_state import (
    CapsStateStore,
    CapsStateUnwritableError,
    PanelAccountCaps,
    PanelCapsSnapshot,
    prune_change_days,
)
from safent_ads.broker.infrastructure.effective_caps import EffectiveCapsResolver
from safent_ads.shared.clock import Clock, SystemClock

logger = structlog.get_logger(__name__)

# Lo que `CapsStateStore.mutate` acepta: una transformacion pura del estado
# que puede lanzar para rechazar o devolver `None` para no escribir nada,
# evaluada bajo el cerrojo de escritura.
type _Mutation = Callable[[PanelCapsSnapshot], PanelCapsSnapshot | None]

# Codigo del contrato del socket que corresponde a cada denegacion nacida
# dentro del cerrojo de escritura.
_DENIAL_CODES: dict[type[Exception], str] = {
    EnvelopeAccountsExhaustedError: "ENVELOPE_ACCOUNTS_EXHAUSTED",
    EnvelopeChangesExhaustedError: "ENVELOPE_CHANGES_EXHAUSTED",
    CapsStateUnwritableError: "CAPS_STATE_UNWRITABLE",
}

__all__ = ["EnvelopeUsage", "HardCapsService", "PanelCapsRequest", "PanelCapsWriting"]


@dataclass(frozen=True, slots=True)
class PanelCapsWriting:
    """Sobre declarado Y almacen de estado montado: las dos cosas viajan
    juntas o no viaja ninguna. `HardCapsService` con `writing=None` es una
    instancia de solo lectura -- exactamente lo que tiene un despliegue sin
    `panel_managed`, donde `set`/`delete` responden ENVELOPE_NOT_DECLARED y
    la vista sigue sirviendo para que el panel pueda decirlo en pantalla."""

    envelope: SpendEnvelope
    store: CapsStateStore


@dataclass(frozen=True, slots=True, kw_only=True)
class PanelCapsRequest:
    """Los tres importes que el panel fija, y solo esos tres, mas la divisa
    de confirmacion. `floor_minor` no esta aqui a proposito (revision T027,
    C2): es cota inferior y no admite resolucion segura en las dos
    direcciones."""

    daily_cap_minor: int
    monthly_cap_minor: int
    ceiling_minor: int
    currency: str

    def amounts(self) -> PanelAmounts:
        return PanelAmounts(
            daily_cap_minor=self.daily_cap_minor,
            monthly_cap_minor=self.monthly_cap_minor,
            ceiling_minor=self.ceiling_minor,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class EnvelopeUsage:
    """El sobre vigente mas su consumo, para que el panel pueda decir
    cuantas cuentas y cuantos cambios quedan."""

    envelope: SpendEnvelope
    accounts_used: int
    cap_changes_today: int


@dataclass(frozen=True, slots=True, kw_only=True)
class CapsView:
    resolution: CapsResolution
    envelope: EnvelopeUsage | None
    panel_state_available: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class _AuditContext:
    """Todo lo que una traza del broker necesita, aplicada o denegada (I-2):
    la cuenta, quien lo pidio, con que `request_id`, que importes pidio y el
    sobre en vigor. `envelope=None` es el estado legitimo de un despliegue
    que no declara `panel_managed` -- y justo lo que explica ese rechazo."""

    account: str
    requested_by: str
    request_id: str | None
    requested: PanelCapsRequest | None
    envelope: SpendEnvelope | None


@dataclass(slots=True)
class _Captured:
    """El "antes" leido DENTRO del cerrojo (M-3). Mutable a proposito: es el
    unico canal por el que la mutacion, que corre bajo el cerrojo del
    almacen, devuelve lo que vio a quien escribe la traza."""

    before: PanelAccountCaps | None = None


class HardCapsService:
    def __init__(
        self,
        resolver: EffectiveCapsResolver,
        status: HardCapsStatus,
        *,
        writing: PanelCapsWriting | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._resolver = resolver
        self._status = status
        self._writing = writing
        self._clock = clock or SystemClock()

    # --- Lectura ---------------------------------------------------------

    def view(self, platform_account_id: str) -> CapsView:
        canonical = self._canonical(platform_account_id)
        snapshot = self._snapshot()
        return CapsView(
            resolution=self._resolver.resolution(canonical),
            envelope=self._usage(snapshot),
            panel_state_available=snapshot.available,
        )

    def status(self) -> dict[str, object]:
        """`caps_digest` y `accounts_count` conservan su significado (solo
        fichero, decision D14): un broker viejo no puede afirmar la
        politica nueva y el campo sigue siendo comparable con el fichero
        del host. Lo del panel entra en DOS campos nuevos y aditivos."""
        snapshot = self._snapshot()
        return {
            "caps_digest": self._status.caps_digest,
            "accounts_count": self._status.accounts_count,
            "panel_state_digest": snapshot.digest,
            "panel_accounts_count": snapshot.accounts_count,
        }

    # --- Escritura -------------------------------------------------------

    async def set_account_caps(
        self,
        platform_account_id: str,
        requested: PanelCapsRequest,
        *,
        requested_by: str,
        request_id: str | None = None,
    ) -> CapsView:
        context = self._context(
            platform_account_id,
            requested_by=requested_by,
            request_id=request_id,
            requested=requested,
        )
        writing = self._require_writing(context)
        context = replace(context, envelope=writing.envelope)
        self._assert_within_envelope(context, requested, writing.envelope)
        entry = PanelAccountCaps(
            daily_cap_minor=requested.daily_cap_minor,
            monthly_cap_minor=requested.monthly_cap_minor,
            ceiling_minor=requested.ceiling_minor,
            currency=requested.currency,
            updated_at=self._now(),
            updated_by=requested_by,
        )
        captured = _Captured()
        await self._mutate_audited(
            self._upsert(context.account, entry, writing.envelope, captured),
            writing=writing,
            context=context,
        )
        self._audit_applied("broker_account_caps_set", context, captured.before, entry)
        return self.view(context.account)

    async def delete_account_caps(
        self, platform_account_id: str, *, requested_by: str, request_id: str | None = None
    ) -> CapsView:
        context = self._context(
            platform_account_id, requested_by=requested_by, request_id=request_id, requested=None
        )
        writing = self._require_writing(context)
        context = replace(context, envelope=writing.envelope)
        snapshot = self._snapshot()
        if snapshot.available and context.account not in snapshot.accounts:
            # Retirar un tope que no existe es un no-op: ni cerrojo, ni
            # escritura, ni traza de "aplicado". Un `ads-api` comprometido
            # no puede convertir un bucle de DELETE en trabajo de disco ni
            # en ruido de auditoria.
            #
            # `snapshot.available` es la mitad que no se puede omitir: con el
            # documento ilegible la instantanea esta VACIA porque no se pudo
            # leer, no porque no haya nada. Cortar aqui responderia "ya no
            # tiene tope" sobre un fichero que quiza si lo tiene. Se sigue
            # hasta el cerrojo, que lo rechaza con CAPS_STATE_UNWRITABLE.
            return self.view(context.account)
        captured = _Captured()
        await self._mutate_audited(
            self._remove(context.account, writing.envelope, captured),
            writing=writing,
            context=context,
        )
        if captured.before is None:
            # Carrera: entre la comprobacion de arriba y el cerrojo, otra
            # peticion ya lo retiro. Nada que anotar como aplicado.
            return self.view(context.account)
        self._audit_applied("broker_account_caps_deleted", context, captured.before, None)
        return self.view(context.account)

    async def _mutate_audited(
        self, mutation: _Mutation, *, writing: PanelCapsWriting, context: _AuditContext
    ) -> None:
        """Las denegaciones que nacen DENTRO del cerrojo (plazas o cambios
        agotados, documento ilegible, fallo de escritura durable) tambien
        dejan traza en el registro del broker: es la que un compromiso de
        `ads-api` no puede borrar."""
        try:
            await writing.store.mutate(mutation)
        except (
            EnvelopeAccountsExhaustedError,
            EnvelopeChangesExhaustedError,
            CapsStateUnwritableError,
        ) as exc:
            self._audit_denied(context, _DENIAL_CODES[type(exc)])
            raise

    # --- Mutaciones bajo el cerrojo del almacen --------------------------

    def _upsert(
        self,
        canonical: str,
        entry: PanelAccountCaps,
        envelope: SpendEnvelope,
        captured: _Captured,
    ) -> _Mutation:
        def mutation(current: PanelCapsSnapshot) -> PanelCapsSnapshot:
            # M-3: el "antes" se lee DENTRO del cerrojo, no antes de pedirlo;
            # si no, dos peticiones concurrentes se auditarian con el mismo
            # estado previo y una de las dos trazas seria falsa.
            captured.before = current.accounts.get(canonical)
            if captured.before is None and current.accounts_count >= envelope.max_accounts:
                raise EnvelopeAccountsExhaustedError(
                    f"max_accounts={envelope.max_accounts} en config/caps.yaml"
                )
            return PanelCapsSnapshot(
                available=True,
                accounts={**current.accounts, canonical: entry},
                changes_by_day=self._spend_one_change(current, envelope),
            )

        return mutation

    def _remove(self, canonical: str, envelope: SpendEnvelope, captured: _Captured) -> _Mutation:
        def mutation(current: PanelCapsSnapshot) -> PanelCapsSnapshot | None:
            captured.before = current.accounts.get(canonical)
            if captured.before is None:
                # `None` = nada que escribir. Idempotente sin tocar el disco
                # ni gastar presupuesto de cambios.
                return None
            return PanelCapsSnapshot(
                available=True,
                accounts={
                    account: caps
                    for account, caps in current.accounts.items()
                    if account != canonical
                },
                changes_by_day=self._spend_one_change(current, envelope),
            )

        return mutation

    def _spend_one_change(
        self, current: PanelCapsSnapshot, envelope: SpendEnvelope
    ) -> dict[str, int]:
        """Se comprueba y se consume BAJO EL MISMO CERROJO que la escritura
        (i6): dos peticiones concurrentes con un solo cambio libre no pueden
        aceptarse las dos."""
        today = self._now().date()
        used = current.changes_on(today)
        if used >= envelope.max_cap_changes_per_day:
            raise EnvelopeChangesExhaustedError(
                f"max_cap_changes_per_day={envelope.max_cap_changes_per_day} en config/caps.yaml"
            )
        pruned = prune_change_days(current.changes_by_day, today=today)
        return {**pruned, today.isoformat(): used + 1}

    # --- Validacion ------------------------------------------------------

    def _context(
        self,
        platform_account_id: str,
        *,
        requested_by: str,
        request_id: str | None,
        requested: PanelCapsRequest | None,
    ) -> _AuditContext:
        """Canonicaliza y arma el sobre de auditoria. Un id que no tiene
        forma de id de cuenta tambien deja traza (I-2): si no, el unico
        rechazo sin registro del broker seria justo el que un atacante
        provoca a voluntad."""
        partial = _AuditContext(
            account=_safe_account_label(platform_account_id),
            requested_by=requested_by,
            request_id=request_id,
            requested=requested,
            envelope=None,
        )
        try:
            canonical = self._canonical(platform_account_id)
        except InvalidAccountCapsError:
            self._audit_denied(partial, "INVALID_CAPS")
            raise
        return replace(partial, account=canonical)

    def _canonical(self, platform_account_id: str) -> str:
        try:
            return canonical_platform_account_id(platform_account_id)
        except InvalidPlatformAccountIdError as exc:
            raise InvalidAccountCapsError(str(exc)) from exc

    def _require_writing(self, context: _AuditContext) -> PanelCapsWriting:
        if self._writing is None:
            # `envelope=None` a proposito: no hay sobre que registrar, y es
            # justamente lo que explica el rechazo.
            self._audit_denied(context, "ENVELOPE_NOT_DECLARED")
            raise EnvelopeNotDeclaredError("config/caps.yaml no declara panel_managed")
        return self._writing

    def _snapshot(self) -> PanelCapsSnapshot:
        return self._resolver.state_snapshot()

    def _assert_within_envelope(
        self, context: _AuditContext, requested: PanelCapsRequest, envelope: SpendEnvelope
    ) -> None:
        if requested.currency != envelope.currency:
            self._audit_denied(context, "INVALID_CAPS")
            raise InvalidAccountCapsError(
                f"la divisa debe ser {envelope.currency} (panel_managed.currency)"
            )
        self._assert_coherent(context, requested, envelope)
        for field, limit in (
            ("daily_cap_minor", envelope.max_daily_cap_minor),
            ("monthly_cap_minor", envelope.max_monthly_cap_minor),
            ("ceiling_minor", envelope.max_ceiling_minor),
        ):
            if getattr(requested, field) > limit:
                self._audit_denied(context, "ENVELOPE_EXCEEDED")
                raise EnvelopeExceededError(f"{field} supera el sobre de config/caps.yaml")

    def _assert_coherent(
        self, context: _AuditContext, requested: PanelCapsRequest, envelope: SpendEnvelope
    ) -> None:
        problems = []
        if requested.monthly_cap_minor < requested.daily_cap_minor:
            problems.append("monthly_cap_minor < daily_cap_minor")
        if requested.daily_cap_minor > requested.ceiling_minor:
            problems.append("daily_cap_minor > ceiling_minor")
        if requested.ceiling_minor < envelope.min_floor_minor:
            # Un techo por debajo del suelo declarado deja la cuenta
            # denegando todo: se rechaza en vez de guardar algo inaplicable.
            problems.append("ceiling_minor < panel_managed.min_floor_minor")
        if problems:
            self._audit_denied(context, "INVALID_CAPS")
            raise InvalidAccountCapsError("; ".join(problems))

    # --- Auditoria del lado del broker -----------------------------------

    def _usage(self, snapshot: PanelCapsSnapshot) -> EnvelopeUsage | None:
        if self._writing is None:
            return None
        return EnvelopeUsage(
            envelope=self._writing.envelope,
            accounts_used=snapshot.accounts_count,
            cap_changes_today=snapshot.changes_on(self._now().date()),
        )

    def _audit_applied(
        self,
        event: str,
        context: _AuditContext,
        before: PanelAccountCaps | None,
        after: PanelAccountCaps | None,
    ) -> None:
        logger.info(
            event,
            verdict="applied",
            after=_amounts_of(after),
            **self._trace(context, before),
        )

    def _audit_denied(self, context: _AuditContext, error_code: str) -> None:
        """I-2: una denegacion lleva lo MISMO que una aplicacion -- lo
        pedido, el antes y el sobre en vigor. Sin eso, la traza que sobrevive
        a un compromiso de `ads-api` no permite reconstruir que se intento."""
        logger.warning(
            "broker_account_caps_denied",
            verdict="denied",
            error_code=error_code,
            **self._trace(context, self._snapshot().accounts.get(context.account)),
        )

    def _trace(self, context: _AuditContext, before: PanelAccountCaps | None) -> dict[str, object]:
        envelope = context.envelope
        return {
            "platform_account_id": context.account,
            "requested": _requested_of(context.requested),
            "before": _amounts_of(before),
            "envelope": None if envelope is None else envelope.model_dump(mode="json"),
            "requested_by": context.requested_by,
            "request_id": context.request_id,
            "caps_digest": self._status.caps_digest,
        }

    def _now(self) -> datetime:
        return self._clock.now().astimezone(UTC)


def _amounts_of(entry: PanelAccountCaps | None) -> dict[str, int] | None:
    if entry is None:
        return None
    return {
        "daily_cap_minor": entry.daily_cap_minor,
        "monthly_cap_minor": entry.monthly_cap_minor,
        "ceiling_minor": entry.ceiling_minor,
    }


def _requested_of(requested: PanelCapsRequest | None) -> dict[str, object] | None:
    if requested is None:
        return None
    return {
        "daily_cap_minor": requested.daily_cap_minor,
        "monthly_cap_minor": requested.monthly_cap_minor,
        "ceiling_minor": requested.ceiling_minor,
        "currency": requested.currency,
    }


def _safe_account_label(raw: str) -> str:
    """Un id que no paso la canonicalizacion no se registra tal cual: se
    recorta y se le quitan los no imprimibles, para que un id manipulado no
    pueda inyectar nada en el log ni engordarlo."""
    printable = "".join(character for character in raw[:64] if character.isprintable())
    return printable or "<vacio>"
