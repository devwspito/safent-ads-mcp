"""Puertos de `iam` (plan.md N0): `application` depende de abstracciones,
nunca de argon2-cffi/pyotp/cryptography/SQLAlchemy directamente."""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from safent_ads.iam.domain.email import Email, InvalidEmailError
from safent_ads.iam.domain.federated_identity import (
    FederatedIdentity,
    FederatedIssuer,
    FederatedSubject,
)
from safent_ads.iam.domain.federated_transaction import (
    FederatedLoginTransaction,
    ReferenceHash,
)
from safent_ads.iam.domain.owner import Owner
from safent_ads.iam.domain.session import Session


class PasswordHasher(Protocol):
    def hash(self, password: str) -> str: ...
    def verify(self, password: str, password_hash: str) -> bool: ...


class TotpCipher(Protocol):
    """Cifra/descifra el secreto TOTP con la clave de `ApiSettings.totp_enc_key`
    (data-model.md: "TOTP obligatorio... cifrado en reposo")."""

    def encrypt(self, secret: str) -> bytes: ...
    def decrypt(self, blob: bytes) -> str: ...


class OwnerRepository(Protocol):
    async def get_by_email(self, email: Email) -> Owner | None: ...
    async def get_by_id(self, owner_id: uuid.UUID) -> Owner | None: ...
    async def save(self, owner: Owner) -> None: ...


class SessionRepository(Protocol):
    async def create(self, session: Session) -> None: ...
    async def get_by_token_hash(self, token_hash: str) -> Session | None: ...
    async def save(self, session: Session) -> None: ...


class LoginAttemptRepository(Protocol):
    """`login_attempts` (0001_bootstrap.py): ledger de bloqueo 5/15 min.
    `ip_address` es `None` cuando `shared.net.client_ip.resolve_client_ip`
    no pudo resolver ninguna IP valida (`request.client` ausente, code
    review 17-sep) -- la columna es `INET` nullable para ese caso exacto;
    con `None` el bloqueo por (correo, IP) simplemente no cuenta esa fila."""

    async def record(self, *, email: str, succeeded: bool, ip_address: str | None) -> None: ...

    async def count_recent_failures(
        self, *, email: str, ip_address: str | None, since: datetime
    ) -> int: ...


class BusinessDirectory(Protocol):
    """Lectura minima de `businesses` (0001_bootstrap.py) que `iam` necesita
    para `require_business_access` (C-27) y `/auth/me`. No es el contexto
    `accounts`: solo comprueba existencia y lista id/slug/name."""

    async def exists(self, business_id: uuid.UUID) -> bool: ...
    async def list_all(self) -> tuple[tuple[uuid.UUID, str, str], ...]: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class AssertionPayload:
    """Payload verificado de la aserción de puente (sso.md §3), ya con la
    firma Ed25519 comprobada -- `ExchangeOwnerAssertion` todavía valida los
    campos literales y la ventana temporal, que son política de aplicación,
    no de criptografía."""

    v: int
    iss: str
    aud: str
    slug: str
    sub: str
    jti: str
    iat: int
    exp: int
    purpose: str
    surface: str


class AssertionVerifier(Protocol):
    """Puerto: descodifica y verifica la firma Ed25519 de una aserción
    (sso.md §4, orden fail-closed: formato -> firma). Lanza
    `AssertionMalformedError`/`AssertionInvalidError` -- nunca devuelve un
    payload sin firma válida."""

    def verify(self, assertion: str) -> AssertionPayload: ...


class AssertionReplayGuard(Protocol):
    """Puerto: antirrepetición de `jti` (sso.md §7 S-2, `sso_assertions_seen`)."""

    async def claim(self, *, jti: str, seen_at: datetime) -> bool:
        """`True` si este proceso reclamó el `jti` por primera vez; `False`
        si ya estaba visto (replay)."""
        ...


class OwnerBridgeResolution(StrEnum):
    CREATED = "created"
    BOUND = "bound"
    ALREADY_BOUND = "already_bound"


@dataclass(frozen=True, slots=True, kw_only=True)
class ResolvedBridgeOwner:
    owner_id: uuid.UUID
    resolution: OwnerBridgeResolution


class OwnerBridgeRepository(Protocol):
    """Puerto: TOFU entre el propietario único y el `sub` de Safent
    (sso.md §4). Separado de `OwnerRepository` (ISP) -- el login por
    email/password nunca necesita conocer `bridge_subject`."""

    async def resolve_for_subject(self, sub: str) -> ResolvedBridgeOwner:
        """Sin propietario -> lo crea atado a `sub`. Propietario sin atar ->
        lo ata (TOFU). Atado al mismo `sub` -> sigue. Atado a otro ->
        `OwnerBoundElsewhereError`."""
        ...


@dataclass(frozen=True, slots=True, kw_only=True)
class FederatedIdentityClaims:
    """Identidad que el proveedor afirma, YA validada
    (`federated_id_token.validate_id_token_claims`): emisor canonico, `sub`,
    correo y si el proveedor lo da por verificado. Es el equivalente federado
    de `AssertionPayload`: llega validada en la forma, pero todavia no
    autorizada -- la lista de autorizados se aplica despues."""

    issuer: FederatedIssuer
    subject: FederatedSubject
    email: Email
    email_verified: bool


@dataclass(frozen=True, slots=True)
class AuthorizedEmailList:
    """Lista de correos autorizados a CREAR SESION (002b FR-105). Conjunto
    cerrado de direcciones completas: ni dominios sueltos ni comodines. Vacia
    = nadie, jamas "vacia = todos". Es configuracion de despliegue, no estado,
    y por eso es un value object sin persistencia."""

    addresses: frozenset[str]

    @classmethod
    def from_raw(cls, raw_addresses: Iterable[str]) -> AuthorizedEmailList:
        """Normaliza con el VO `Email` y DESCARTA lo que no sea una direccion
        completa. Descartar (en vez de reventar) es deliberado: una lista mal
        escrita deja el login federado cerrado sin impedir el arranque ni la
        via clasica (edge case "lista vacia o mal escrita")."""
        addresses: set[str] = set()
        for raw in raw_addresses:
            try:
                addresses.add(Email(raw).value)
            except InvalidEmailError:
                continue
        return cls(frozenset(addresses))

    @property
    def is_empty(self) -> bool:
        return not self.addresses

    def authorizes(self, email: Email) -> bool:
        return email.value in self.addresses


class FederatedIdentityProvider(Protocol):
    """Puerto: el proveedor de identidad externo (hoy Google). El adaptador
    solo hace transporte -- la validacion de claims es una funcion pura
    (`iam/application/federated_id_token.py`), probada sin red."""

    def authorization_url(self, *, state: str, nonce: str, redirect_uri: str) -> str:
        """URL a la que navega el NAVEGADOR (no hay egreso del servidor aqui).
        `state` y `nonce` viajan en claro solo en esta URL."""
        ...

    async def exchange_code(
        self, *, code: str, redirect_uri: str, expected_nonce_hash: ReferenceHash
    ) -> FederatedIdentityClaims:
        """Canjea el `code` y devuelve la identidad validada.

        `expected_nonce_hash` es la HUELLA del nonce, no el nonce: en el
        callback ya no existe el valor en claro (solo se persistio su sha256),
        asi que la comparacion es `sha256(nonce del id_token)` contra esta
        huella, igual que PKCE compara `sha256(verifier)` con el challenge.
        """
        ...


class FederatedTransactionRepository(Protocol):
    """Puerto: `federated_login_transactions`. El consumo es de un solo uso y
    atomico (FR-115) -- desconocida, caducada o ya consumida devuelve `None`
    y el caso de uso rechaza sin efectos."""

    async def create(self, transaction: FederatedLoginTransaction) -> None: ...

    async def count_pending_for_ip(self, *, ip_address: str, now: datetime) -> int:
        """threat-model.md C-79: cuantas transacciones (ni consumidas ni
        caducadas) tiene abiertas esta IP ahora mismo -- el tope de 5 que
        `StartFederatedLogin` aplica ANTES de crear una sexta. Tope BASTO
        (TOCTOU, nota de docstring, code review 17-sep): contar y crear no
        son la misma transaccion de base de datos, asi que peticiones
        CONCURRENTES de la misma IP pueden colarse por encima de 5 -- no es
        un limite de tasa exacto, es una defensa contra un barrido de
        estado sin fondo (C-79), y ese uso lo tolera."""
        ...

    async def consume(
        self, *, state_hash: ReferenceHash, now: datetime
    ) -> FederatedLoginTransaction | None: ...

    async def purge_expired(self, now: datetime) -> int:
        """Poda de filas caducadas, para el janitor que ya existe. Devuelve
        cuantas se borraron."""
        ...


class OwnerFederatedIdentityResolution(StrEnum):
    CREATED = "created"
    BOUND = "bound"
    ALREADY_BOUND = "already_bound"


@dataclass(frozen=True, slots=True, kw_only=True)
class ResolvedFederatedOwner:
    owner_id: uuid.UUID
    resolution: OwnerFederatedIdentityResolution


class OwnerFederatedIdentityRepository(Protocol):
    """Puerto: TOFU entre el propietario unico y el `sub` del proveedor
    (002b research.md Decision A), calcado de `OwnerBridgeRepository`.
    Separado de `OwnerRepository` (ISP): el login por email/password nunca
    necesita conocer identidades federadas."""

    async def resolve_for_subject(
        self,
        *,
        issuer: FederatedIssuer,
        subject: FederatedSubject,
        email: Email,
        now: datetime,
    ) -> ResolvedFederatedOwner:
        """Sin propietario -> lo crea atado a `(issuer, subject)` con una
        contrasena imposible de adivinar (FR-109). Propietario sin atar -> lo
        ata (TOFU). Atado al mismo `subject` -> sigue y refresca `last_seen_at`.
        Atado a otro -> `OwnerBoundElsewhereError` (FR-108)."""
        ...

    async def find_for_owner(self, *, owner_id: uuid.UUID) -> FederatedIdentity | None:
        """Identidad atada a ese dueno, si la hay. La necesita la capa de
        presentacion para saber si el dueno puede producir identificacion
        fresca por la via federada (`details.methods`, FR-106)."""
        ...


class SessionByIdRepository(SessionRepository, Protocol):
    """`SessionRepository` mas las dos capacidades que anade 002b: resolver
    la sesion por su id, y escribir la marca de identificacion federada por
    SU PROPIA via -- nunca por el `save()` generico (T064 security review,
    re-verificacion de C-82: `save()` lo compartian `current_owner`/
    `logout.py`, y su escritura `GREATEST` podia resucitar una marca que
    `grants_router.py` acababa de invalidar). El callback de Google NO puede
    leer la cookie (`SameSite=strict` no viaja en una vuelta desde otro
    sitio), asi que `get_by_id` es la atadura por `session_id` guardado en
    la transaccion. `SqlSessionRepository` y el doble en memoria satisfacen
    este puerto."""

    async def get_by_id(self, session_id: uuid.UUID) -> Session | None: ...
    async def save_federated_mark(self, session_id: uuid.UUID, at: datetime) -> None: ...


class OpenConsentTransactions(Protocol):
    """T085 (plan.md "mcp_oauth -> iam, nunca al reves"): `POST /auth/
    federated/start` necesita comprobar que un `txn_id` de consentimiento
    -- una `AuthorizationRequest` de `mcp_oauth` -- sigue `PENDING` y sin
    caducar ANTES de abrir el salto a Google, pero ese dato pertenece a
    `mcp_oauth`. Este puerto deja a `iam` pedir la respuesta sin importar
    `mcp_oauth` en ninguna capa; la implementacion real (que SI conoce los
    dos modulos) vive en `composition/federated_routes.py`, el UNICO punto
    autorizado a conocerlos a ambos."""

    async def require_open(self, txn_id: uuid.UUID) -> None:
        """Levanta `ConsentTransactionNotFoundError` si `txn_id` no existe,
        o `ConsentTransactionNotOpenError` si existe pero ya no esta
        `PENDING` o ya caduco. No devuelve nada: `iam` no necesita ver la
        forma de una `AuthorizationRequest`, solo saber si sigue abierta."""
        ...
