"""Adaptador SQL de `OwnerBridgeRepository` sobre `owners.bridge_subject`
(0030_sso_assertions_seen, 026 sso.md §4). Modelo de propietario único: como
mucho una fila en `owners` nace por este camino, con email sintético fijo
(nunca uno real -- nadie inicia sesión con contraseña en esa cuenta, plan.md
§8 "queda como cuenta solo-puente + TOTP enrolable") y una contraseña
imposible de adivinar (Argon2id sobre 32 bytes aleatorios, descartados de
inmediato: ni siquiera este proceso los conserva).

`SELECT ... FOR UPDATE` sobre la fila existente serializa dos canjes
concurrentes para el mismo propietario; el `INSERT ... ON CONFLICT` sobre el
email sintético fijo hace lo propio cuando la tabla está vacía (no hay fila
que bloquear todavía) -- si dos arranques concurrentes compiten por crearla,
el perdedor vuelve a leer y encuentra lo que el ganador acaba de atar.

spec 002b threat-model.md C-59: ese `ON CONFLICT` solo protege esta clase
de carrera cuando los dos competidores escriben el MISMO email. El login
federado (`sql_owner_federated_identity_repository.py`) escribe el correo
REAL de Google, distinto del sintético del puente, así que las dos vías
podrían crear cada una su propio dueño en una instalación vacía sin que
`owners.email UNIQUE` llegue a chocar. `LOCK_SOLE_OWNER_SQL`
(`owner_singleton_lock.py`), tomado aquí como primera sentencia de la
transacción -- igual que en el adaptador federado --, cierra ese hueco."""

from __future__ import annotations

import secrets
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.iam.application.errors import OwnerBoundElsewhereError
from safent_ads.iam.application.ports import (
    OwnerBridgeResolution,
    ResolvedBridgeOwner,
)
from safent_ads.iam.infrastructure.argon2_password_hasher import Argon2PasswordHasher
from safent_ads.iam.infrastructure.owner_singleton_lock import LOCK_SOLE_OWNER_SQL
from safent_ads.shared.errors import InfrastructureError

_BRIDGE_OWNER_EMAIL = "bridge-owner@safent-ads.internal"


class OwnerBridgeRaceError(InfrastructureError):
    """No se pudo resolver el propietario tras perder la carrera de
    creacion concurrente -- deberia ser inalcanzable en el modelo de
    propietario unico, pero falla alto en vez de un `TypeError` opaco."""

_SELECT_SOLE_OWNER_FOR_UPDATE_SQL = text(
    "SELECT id, bridge_subject FROM owners ORDER BY created_at ASC LIMIT 1 FOR UPDATE"
)
_BIND_SUBJECT_SQL = text("UPDATE owners SET bridge_subject = :sub WHERE id = :id")
_INSERT_BRIDGED_OWNER_SQL = text(
    """
    INSERT INTO owners (id, email, password_hash, bridge_subject)
    VALUES (:id, :email, :password_hash, :sub)
    ON CONFLICT (email) DO NOTHING
    RETURNING id
    """
)


class SqlOwnerBridgeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def resolve_for_subject(self, sub: str) -> ResolvedBridgeOwner:
        await self._session.execute(LOCK_SOLE_OWNER_SQL)
        existing = await self._select_sole_owner()
        if existing is None:
            created_id = await self._create_bridged_owner(sub)
            if created_id is not None:
                await self._session.commit()
                return ResolvedBridgeOwner(
                    owner_id=created_id, resolution=OwnerBridgeResolution.CREATED
                )
            # Un canje concurrente gano la carrera de creacion: relee lo que
            # ya existe y resuelve sobre esa fila en vez de fallar.
            existing = await self._select_sole_owner()
        if existing is None:
            raise OwnerBridgeRaceError(
                "no se pudo resolver el propietario tras la carrera de creacion"
            )
        owner_id, bridge_subject = existing
        resolution = await self._bind_or_check(owner_id, bridge_subject, sub)
        await self._session.commit()
        return ResolvedBridgeOwner(owner_id=owner_id, resolution=resolution)

    async def _select_sole_owner(self) -> tuple[uuid.UUID, str | None] | None:
        row = (
            await self._session.execute(_SELECT_SOLE_OWNER_FOR_UPDATE_SQL)
        ).mappings().one_or_none()
        if row is None:
            return None
        return row["id"], row["bridge_subject"]

    async def _create_bridged_owner(self, sub: str) -> uuid.UUID | None:
        owner_id = uuid.uuid4()
        password_hash = Argon2PasswordHasher().hash(secrets.token_urlsafe(32))
        row = (
            await self._session.execute(
                _INSERT_BRIDGED_OWNER_SQL,
                {
                    "id": str(owner_id),
                    "email": _BRIDGE_OWNER_EMAIL,
                    "password_hash": password_hash,
                    "sub": sub,
                },
            )
        ).mappings().one_or_none()
        return owner_id if row is not None else None

    async def _bind_or_check(
        self, owner_id: uuid.UUID, bridge_subject: str | None, sub: str
    ) -> OwnerBridgeResolution:
        if bridge_subject is None:
            await self._session.execute(_BIND_SUBJECT_SQL, {"id": str(owner_id), "sub": sub})
            return OwnerBridgeResolution.BOUND
        if bridge_subject == sub:
            return OwnerBridgeResolution.ALREADY_BOUND
        raise OwnerBoundElsewhereError(f"owner {owner_id} atado a otro sujeto")
