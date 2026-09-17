"""`exchange_federated_code`/`ResolveFederatedLogin` (002b contracts/
federated-login.md §1, research.md Decisiones A/B/C, threat-model.md C-75
pieza 2): fases (b) y (c) de la vuelta del proveedor de identidad. La fase
(a) -- consumir la referencia -- vive en `consume_federated_transaction.py`
y corre bajo su PROPIA sesion de BD, ya cerrada para cuando esta fase (b)
llama a Google (T084 hardening: "la conexion de Postgres no se mantiene
abierta durante la llamada a Google", hasta 10s, NFR-103).

Orden fail-closed, sin excepcion: [fase a, en otro modulo] consumir la
referencia -> canjear el codigo (b) -> correo verificado -> correo en la
lista -> resolver al dueno (TOFU) -> emitir sesion o marcar frescura (c). Si
algo falla en (b) o (c) no queda ni sesion, ni alta, ni marca (FR-102,
FR-115) -- la unica escritura que sobrevive a un fallo posterior es el
consumo de la referencia, ya comprometido en la fase (a): una fila quemada
sin ningun otro efecto es exactamente lo que exige threat-model.md C-66 en
el camino de fallo, y descarta cualquier reintento del canje (C-75 pieza 3).

No sabe que existe HTTP: devuelve un desenlace y lanza errores tipados. La
presentacion los traduce a `federated_error=<codigo>`, colapsando las dos
denegaciones en `denied` para no delatar si el correo estaba en la lista
(SC-103).

`FederatedCallbackFailureContext` (002b tasks.md T053, checkpoints/us1.md
"Abierto, no arreglado aqui"): todo fallo que ocurre DESPUES de consumir la
referencia conoce ya el proposito y el `txn_id` de esa transaccion, para
que la presentacion decida a donde vuelve el dueno (contracts/
federated-login.md §1: un fallo de re-identificacion vuelve a la MISMA
transaccion de consentimiento, nunca a `/login`). Tanto `exchange_federated_
code` como `ResolveFederatedLogin.execute()` envuelven CUALQUIER fallo en
`FederatedCallbackFailed` (T064, revision de codigo: nada de adjuntar
atributos a una excepcion ajena por `setattr` -- un tipo propio, encadenado
con `raise ... from exc`, deja el tipo ORIGINAL disponible en `.cause` para
quien siga clasificando por tipo (`federated_router.py::_classify_failure`).
Un fallo ANTES de consumir (referencia desconocida, caducada o repetida) no
lleva contexto: no hay transaccion que atribuirle, y por eso escapa SIN
envolver (`consume_federated_transaction.py`)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import NoReturn

from safent_ads.iam.application.errors import (
    FederatedDenialReason,
    FederatedIdentityMismatchError,
    FederatedIdentityNotAuthorizedError,
    FederatedTransactionInvalidError,
)
from safent_ads.iam.application.ports import (
    AuthorizedEmailList,
    FederatedIdentityClaims,
    FederatedIdentityProvider,
    OwnerFederatedIdentityRepository,
    OwnerFederatedIdentityResolution,
    ResolvedFederatedOwner,
    SessionByIdRepository,
)
from safent_ads.iam.application.session_issuance import AuthenticatedSession, issue_session
from safent_ads.iam.application.session_policy import SESSION_ABSOLUTE_TTL
from safent_ads.iam.domain.federated_transaction import (
    FederatedLoginTransaction,
    TransactionPurpose,
)
from safent_ads.iam.domain.session import Session, SessionOrigin
from safent_ads.shared.clock import Clock
from safent_ads.shared.errors import ApplicationError
from safent_ads.shared.ids import IdGenerator


class FederatedLoginOutcome(StrEnum):
    SESSION_ISSUED = "session_issued"
    IDENTIFICATION_REFRESHED = "identification_refreshed"


@dataclass(frozen=True, slots=True, kw_only=True)
class FederatedSessionIssued:
    """Desenlace del proposito `entrar`: hay sesion nueva que entregar al
    navegador."""

    owner_id: uuid.UUID
    authenticated_session: AuthenticatedSession
    resolution: OwnerFederatedIdentityResolution
    txn_id: uuid.UUID | None

    @property
    def outcome(self) -> FederatedLoginOutcome:
        return FederatedLoginOutcome.SESSION_ISSUED


@dataclass(frozen=True, slots=True, kw_only=True)
class FederatedIdentificationRefreshed:
    """Desenlace del proposito `re-identificar`: la sesion que abrio el salto
    queda marcada como fresca. No nace ninguna sesion."""

    owner_id: uuid.UUID
    session_id: uuid.UUID
    txn_id: uuid.UUID | None

    @property
    def outcome(self) -> FederatedLoginOutcome:
        return FederatedLoginOutcome.IDENTIFICATION_REFRESHED


CompletedFederatedLogin = FederatedSessionIssued | FederatedIdentificationRefreshed


@dataclass(frozen=True, slots=True, kw_only=True)
class ProviderDeniedConsent:
    """Desenlace cuando Google volvio con `error=` (el dueno cancelo, o
    denego el consentimiento en la pantalla de Google): la referencia YA
    se consumio (fase a) -- de un solo uso tambien en el camino de fallo,
    threat-model.md C-66 -- pero no hay codigo que canjear ni claims que
    resolver. SIEMPRE `denied`, nunca un desenlace con sesion. Tipo propio
    (code review 17-sep, nit) en vez de reusar `FederatedLoginTransaction`
    como senal ad-hoc: la presentacion no deberia tener que distinguir "la
    transaccion consumida" de "el desenlace del callback" por `isinstance`
    sobre un objeto de dominio."""

    purpose: TransactionPurpose
    txn_id: uuid.UUID | None


@dataclass(frozen=True, slots=True, kw_only=True)
class FederatedCallbackFailureContext:
    """Ver docstring del modulo. `txn_id` puede ser `None` (un `start` sin
    transaccion de consentimiento que retomar, p.ej. una re-identificacion
    espontanea desde el panel)."""

    purpose: TransactionPurpose
    txn_id: uuid.UUID | None


class FederatedCallbackFailed(ApplicationError):
    """Envoltorio tipado (T064, revision de codigo) de cualquier fallo
    ocurrido DESPUES de consumir la referencia: `cause` es la excepcion
    ORIGINAL (`FederatedIdentityNotAuthorizedError`, `FederatedIdentity
    MismatchError`, `OwnerBoundElsewhereError`, `GoogleOidcError` o
    cualquier otra) -- `federated_router.py` sigue clasificando por el
    TIPO de `cause`, nunca por este envoltorio."""

    def __init__(self, *, cause: Exception, context: FederatedCallbackFailureContext) -> None:
        super().__init__(str(cause))
        self.cause = cause
        self.context = context


def _reraise_wrapped(transaction: FederatedLoginTransaction, exc: Exception) -> NoReturn:
    context = FederatedCallbackFailureContext(
        purpose=transaction.purpose, txn_id=transaction.txn_id
    )
    raise FederatedCallbackFailed(cause=exc, context=context) from exc


async def exchange_federated_code(
    provider: FederatedIdentityProvider,
    transaction: FederatedLoginTransaction,
    *,
    code: str,
    redirect_uri: str,
) -> FederatedIdentityClaims:
    """Fase (b): SIN ninguna sesion de BD abierta -- la fase (a) ya cerro la
    suya al comprometer el consumo. Un fallo del proveedor (lento, caido,
    respuesta ilegible) se envuelve con el `purpose`/`txn_id` de la
    transaccion YA consumida, igual que antes de que existiera esta fase
    separada."""
    try:
        return await provider.exchange_code(
            code=code, redirect_uri=redirect_uri, expected_nonce_hash=transaction.nonce_hash
        )
    except Exception as exc:  # noqa: BLE001 - threat-model.md C-75: fail closed, nunca 500
        _reraise_wrapped(transaction, exc)


class ResolveFederatedLogin:
    """Fase (c): con la transaccion YA consumida (fase a) y los claims YA
    canjeados (fase b), bajo una sesion de BD NUEVA -- resuelve al dueno y
    emite sesion o marca frescura."""

    def __init__(
        self,
        *,
        owner_identities: OwnerFederatedIdentityRepository,
        sessions: SessionByIdRepository,
        allowed_emails: AuthorizedEmailList,
        id_generator: IdGenerator,
        clock: Clock,
    ) -> None:
        self._owner_identities = owner_identities
        self._sessions = sessions
        self._allowed_emails = allowed_emails
        self._id_generator = id_generator
        self._clock = clock

    async def execute(
        self, transaction: FederatedLoginTransaction, claims: FederatedIdentityClaims
    ) -> CompletedFederatedLogin:
        try:
            return await self._resolve(transaction, claims)
        except Exception as exc:  # noqa: BLE001 - threat-model.md C-75: fail closed, nunca 500
            _reraise_wrapped(transaction, exc)

    async def _resolve(
        self, transaction: FederatedLoginTransaction, claims: FederatedIdentityClaims
    ) -> CompletedFederatedLogin:
        now = self._clock.now()
        self._require_authorized(claims)
        resolved = await self._owner_identities.resolve_for_subject(
            issuer=claims.issuer, subject=claims.subject, email=claims.email, now=now
        )
        if transaction.purpose is TransactionPurpose.REIDENTIFY:
            return await self._refresh_identification(transaction, resolved, now)
        return await self._issue_federated_session(transaction, resolved, now)

    def _require_authorized(self, claims: FederatedIdentityClaims) -> None:
        """Correo verificado es NECESARIO pero nunca suficiente: la lista
        cerrada decide (FR-102). Se comprueba antes de tocar la base de datos,
        asi que un correo no autorizado no crea ni modifica ningun dueno."""
        if not claims.email_verified:
            raise FederatedIdentityNotAuthorizedError(
                FederatedDenialReason.EMAIL_NOT_VERIFIED, claims.email
            )
        if not self._allowed_emails.authorizes(claims.email):
            raise FederatedIdentityNotAuthorizedError(
                FederatedDenialReason.EMAIL_NOT_ALLOWED, claims.email
            )

    async def _issue_federated_session(
        self,
        transaction: FederatedLoginTransaction,
        resolved: ResolvedFederatedOwner,
        now: datetime,
    ) -> FederatedSessionIssued:
        authenticated = await issue_session(
            owner_id=resolved.owner_id,
            sessions=self._sessions,
            id_generator=self._id_generator,
            clock=self._clock,
            origin=SessionOrigin.FEDERATED,
            federated_auth_at=now,
        )
        return FederatedSessionIssued(
            owner_id=resolved.owner_id,
            authenticated_session=authenticated,
            resolution=resolved.resolution,
            txn_id=transaction.txn_id,
        )

    async def _refresh_identification(
        self,
        transaction: FederatedLoginTransaction,
        resolved: ResolvedFederatedOwner,
        now: datetime,
    ) -> FederatedIdentificationRefreshed:
        session = await self._require_active_session(transaction.session_id, now)
        if session.owner_id != resolved.owner_id:
            raise FederatedIdentityMismatchError(
                f"la identidad federada no pertenece al dueno de la sesion {session.id}"
            )
        session.record_fresh_identification(now)
        # `save_federated_mark`, no `save()` (T064, re-verificacion de
        # C-82): esa marca tiene su propia via de escritura, ajena a
        # cualquier `save()` generico que otra peticion concurrente pueda
        # hacer con una copia en memoria mas vieja.
        await self._sessions.save_federated_mark(session.id, now)
        return FederatedIdentificationRefreshed(
            owner_id=session.owner_id, session_id=session.id, txn_id=transaction.txn_id
        )

    async def _require_active_session(self, session_id: uuid.UUID | None, now: datetime) -> Session:
        session = None if session_id is None else await self._sessions.get_by_id(session_id)
        if session is None:
            raise FederatedTransactionInvalidError("la sesion del salto federado ya no existe")
        if session.is_revoked or session.is_expired(now, SESSION_ABSOLUTE_TTL):
            raise FederatedTransactionInvalidError("la sesion del salto federado ya no vive")
        return session
