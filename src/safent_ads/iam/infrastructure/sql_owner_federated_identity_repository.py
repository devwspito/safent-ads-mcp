"""Adaptador SQL de `OwnerFederatedIdentityRepository` sobre
`owner_federated_identities` (0052, spec 002b S1/FR-107): TOFU entre el
propietario unico y el `sub` de Google.

Calcado de `sql_owner_bridge_repository.py`, con las mismas tres
resoluciones (`CREATED` / `BOUND` / `ALREADY_BOUND`) y la misma contrasena
imposible de adivinar para el dueno que nace por aqui: Argon2id sobre 32
bytes aleatorios que se descartan de inmediato -- ni siquiera este proceso
los conserva (FR-109). Quien nazca asi entra por Google y por ningun otro
sitio, y si alguien prueba su correo con una contrasena solo obtiene
"credencial invalida", sin enterarse de que la cuenta es federada.

Una diferencia deliberada con el puente: aqui la carrera de creacion se
serializa con `pg_advisory_xact_lock` (`owner_singleton_lock.py`, spec 002b
threat-model.md C-59) en vez de apoyarse en el `ON CONFLICT` del correo. El
puente siempre escribia el MISMO correo sintetico, asi que el choque de
unicidad le bastaba antes; con correos reales, dos direcciones autorizadas
distintas entrando a la vez en una instalacion vacia NO chocarian y crearian
dos duenos, rompiendo "como mucho un dueno por despliegue". El MISMO lock lo
toma tambien `SqlOwnerBridgeRepository.resolve_for_subject()` (C-59): sin
eso, el puente y el federado podrian crear cada uno su propio dueno en la
misma carrera cruzada. El lock cubre tambien el hueco que el `SELECT ...
FOR UPDATE` no puede cubrir: una tabla vacia no tiene fila que bloquear.

El correo verificado y en la lista de autorizados es condicion de ENTRADA a
este adaptador (la comprueba el caso de uso contra el `id_token` de esa
entrada, nunca contra la fila): aqui no se vuelve a decidir quien puede
pasar, solo a que dueno corresponde el `subject`."""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.iam.application.errors import OwnerBoundElsewhereError
from safent_ads.iam.application.ports import (
    OwnerFederatedIdentityResolution,
    ResolvedFederatedOwner,
)
from safent_ads.iam.domain.email import Email
from safent_ads.iam.domain.federated_identity import (
    FederatedIdentity,
    FederatedIssuer,
    FederatedSubject,
)
from safent_ads.iam.infrastructure.argon2_password_hasher import Argon2PasswordHasher
from safent_ads.iam.infrastructure.owner_singleton_lock import LOCK_SOLE_OWNER_SQL
from safent_ads.shared.errors import InfrastructureError

logger = structlog.get_logger(__name__)

_RANDOM_PASSWORD_BYTES = 32


class OwnerFederatedIdentityRaceError(InfrastructureError):
    """No se pudo resolver el propietario tras la carrera de creacion --
    inalcanzable con el advisory lock tomado, pero falla alto en vez de un
    `TypeError` opaco."""


_SELECT_SOLE_OWNER_SQL = text("SELECT id FROM owners ORDER BY created_at ASC LIMIT 1")

_SELECT_IDENTITY_FOR_SUBJECT_SQL = text(
    "SELECT subject FROM owner_federated_identities WHERE owner_id = :owner_id AND issuer = :issuer"
)

_INSERT_OWNER_SQL = text(
    """
    INSERT INTO owners (id, email, password_hash)
    VALUES (:id, :email, :password_hash)
    ON CONFLICT (email) DO NOTHING
    RETURNING id
    """
)

_INSERT_IDENTITY_SQL = text(
    """
    INSERT INTO owner_federated_identities (owner_id, issuer, subject, email_at_binding,
                                            bound_at, last_seen_at)
    VALUES (:owner_id, :issuer, :subject, :email, :now, :now)
    """
)

_TOUCH_IDENTITY_SQL = text(
    "UPDATE owner_federated_identities SET last_seen_at = :now "
    "WHERE issuer = :issuer AND subject = :subject"
)

_SELECT_IDENTITY_FOR_FIND_SQL = text(
    "SELECT owner_id, issuer, subject, email_at_binding, bound_at, last_seen_at "
    "FROM owner_federated_identities WHERE owner_id = :owner_id"
)


class SqlOwnerFederatedIdentityRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def resolve_for_subject(
        self,
        *,
        issuer: FederatedIssuer,
        subject: FederatedSubject,
        email: Email,
        now: datetime,
    ) -> ResolvedFederatedOwner:
        await self._session.execute(LOCK_SOLE_OWNER_SQL)
        owner_id = await self._select_sole_owner()
        if owner_id is None:
            owner_id = await self._create_federated_owner(email)
            if owner_id is None:
                raise OwnerFederatedIdentityRaceError(
                    "no se pudo crear el propietario federado pese al advisory lock"
                )
            await self._bind_identity(
                owner_id, issuer=issuer, subject=subject, email=email, now=now
            )
            await self._session.commit()
            self._log_owner_registered(owner_id, issuer=issuer, email=email, at=now)
            return ResolvedFederatedOwner(
                owner_id=owner_id, resolution=OwnerFederatedIdentityResolution.CREATED
            )

        resolution = await self._bind_or_check(
            owner_id, issuer=issuer, subject=subject, email=email, now=now
        )
        await self._session.commit()
        return ResolvedFederatedOwner(owner_id=owner_id, resolution=resolution)

    async def find_for_owner(self, *, owner_id: uuid.UUID) -> FederatedIdentity | None:
        row = (
            (
                await self._session.execute(
                    _SELECT_IDENTITY_FOR_FIND_SQL, {"owner_id": str(owner_id)}
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        return FederatedIdentity(
            owner_id=owner_id,
            issuer=FederatedIssuer.parse(row["issuer"]),
            subject=FederatedSubject(str(row["subject"])),
            email_at_binding=Email(row["email_at_binding"]),
            bound_at=row["bound_at"],
            last_seen_at=row["last_seen_at"],
        )

    async def _select_sole_owner(self) -> uuid.UUID | None:
        row = (await self._session.execute(_SELECT_SOLE_OWNER_SQL)).mappings().one_or_none()
        return None if row is None else row["id"]

    async def _create_federated_owner(self, email: Email) -> uuid.UUID | None:
        """El correo real de Google, no uno sintetico: es la direccion del
        dueno y es lo que `GET /auth/me` debe poder ensenar. La contrasena
        es Argon2id sobre 32 bytes aleatorios que nadie conserva, asi que la
        via clasica queda cerrada para esta cuenta hasta que alguien le de
        credenciales deliberadamente (FR-109)."""
        owner_id = uuid.uuid4()
        password_hash = Argon2PasswordHasher().hash(secrets.token_urlsafe(_RANDOM_PASSWORD_BYTES))
        row = (
            (
                await self._session.execute(
                    _INSERT_OWNER_SQL,
                    {"id": str(owner_id), "email": email.value, "password_hash": password_hash},
                )
            )
            .mappings()
            .one_or_none()
        )
        return owner_id if row is not None else None

    @staticmethod
    def _log_owner_registered(
        owner_id: uuid.UUID, *, issuer: FederatedIssuer, email: Email, at: datetime
    ) -> None:
        """threat-model.md C-61/MENOR-2: el alta del primer dueno es el acto
        mas privilegiado de la instalacion -- WARNING, no INFO -- y lleva el
        correo (C-78/NFR-107 lo permite explicitamente para ESTE evento).
        Solo se llama desde la rama `CREATED`, que el advisory lock
        (`LOCK_SOLE_OWNER_SQL`) garantiza que corre como mucho una vez por
        despliegue: nunca `state`, `code`, `nonce` ni `id_token`."""
        logger.warning(
            "federated_owner_registered",
            owner_id=str(owner_id),
            issuer=str(issuer),
            email=email.value,
            at=at.isoformat(),
        )

    async def _bind_or_check(
        self,
        owner_id: uuid.UUID,
        *,
        issuer: FederatedIssuer,
        subject: FederatedSubject,
        email: Email,
        now: datetime,
    ) -> OwnerFederatedIdentityResolution:
        bound_subject = await self._select_bound_subject(owner_id, issuer)
        if bound_subject is None:
            await self._bind_identity(
                owner_id, issuer=issuer, subject=subject, email=email, now=now
            )
            return OwnerFederatedIdentityResolution.BOUND
        if bound_subject == subject.value:
            await self._session.execute(
                _TOUCH_IDENTITY_SQL,
                {"now": now, "issuer": str(issuer), "subject": subject.value},
            )
            return OwnerFederatedIdentityResolution.ALREADY_BOUND
        # Otro `subject` del mismo emisor: cuenta de Google recreada o un
        # segundo correo autorizado (FR-108). Re-vincular exige intervencion
        # administrativa, nunca ocurre sola.
        raise OwnerBoundElsewhereError(f"owner {owner_id} atado a otra identidad de {issuer}")

    async def _select_bound_subject(
        self, owner_id: uuid.UUID, issuer: FederatedIssuer
    ) -> str | None:
        row = (
            (
                await self._session.execute(
                    _SELECT_IDENTITY_FOR_SUBJECT_SQL,
                    {"owner_id": str(owner_id), "issuer": str(issuer)},
                )
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else str(row["subject"])

    async def _bind_identity(
        self,
        owner_id: uuid.UUID,
        *,
        issuer: FederatedIssuer,
        subject: FederatedSubject,
        email: Email,
        now: datetime,
    ) -> None:
        await self._session.execute(
            _INSERT_IDENTITY_SQL,
            {
                "owner_id": str(owner_id),
                "issuer": str(issuer),
                "subject": subject.value,
                "email": email.value,
                "now": now,
            },
        )
