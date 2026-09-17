"""`Session` aggregate (data-model.md §Owner/Session, tabla `sessions`):
"token de sesion almacenado hasheado". Caducidad absoluta+por inactividad
(tasks.md T011) implementadas sobre las dos columnas que ya trae
`0001_bootstrap.py` -- sin migracion nueva: `created_at` es el ancla
absoluta y `expires_at` es el limite de inactividad, que `touch()` desliza
hacia delante sin poder superar `created_at + absolute_ttl`.

002b (data-model.md §Session) anade COMO nacio la sesion (`origin`, que es
auditoria y nunca autoridad) y CUANDO se identifico por ultima vez ante el
proveedor federado (`last_federated_auth_at`). La marca de frescura es
independiente de la caducidad: registrarla no mueve `created_at` ni empuja
`expires_at` -- 002b no alarga ninguna sesion."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from enum import StrEnum

from safent_ads.iam.domain.errors import (
    FederatedSessionWithoutIdentificationError,
    SessionExpiredError,
    SessionRevokedError,
)


class SessionOrigin(StrEnum):
    """Como nacio la sesion (spec 002b FR-104, `sessions.origin`). Es
    AUDITORIA, no autoridad: lo que habilita una accion sensible es la
    prueba que el dueno presenta, nunca el origen de su sesion. No se
    reescribe jamas tras la creacion. `PASSWORD` es el defecto por ser el
    origen menos capaz -- igual que el `DEFAULT` de la columna (0052): un
    llamador que se olvide de declararlo deja al dueno fuera, nunca dentro."""

    FEDERATED = "federated"
    # S105: es el nombre de un origen de sesion, no una contrasena.
    PASSWORD = "password"  # noqa: S105
    BRIDGE = "bridge"

# Perf (medido 16-sep en la instancia de produccion, item 2): el panel sondea
# varios endpoints cada 10-60 s, y cada peticion autenticada pasaba por
# `touch()` + un UPDATE. `touch()` sigue moviendo `expires_at` en cada
# llamada (la semantica de idle/absolute TTL no cambia), pero solo
# devuelve `True` cuando el movimiento supera este umbral, para que el
# llamador (`iam/presentation/dependencies.py::current_owner`) se salte
# la escritura cuando no vale la pena.
TOUCH_PERSISTENCE_THRESHOLD = timedelta(seconds=60)


class Session:
    def __init__(
        self,
        *,
        session_id: uuid.UUID,
        owner_id: uuid.UUID,
        token_hash: str,
        created_at: datetime,
        expires_at: datetime,
        revoked_at: datetime | None,
        origin: SessionOrigin = SessionOrigin.PASSWORD,
        last_federated_auth_at: datetime | None = None,
    ) -> None:
        if origin is SessionOrigin.FEDERATED and last_federated_auth_at is None:
            raise FederatedSessionWithoutIdentificationError(
                f"sesion {session_id} nacida de Google sin identificacion federada"
            )
        self.id = session_id
        self.owner_id = owner_id
        self.token_hash = token_hash
        self.created_at = created_at
        self.expires_at = expires_at
        self.revoked_at = revoked_at
        self.origin = origin
        self.last_federated_auth_at = last_federated_auth_at

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    def is_expired(self, now: datetime, absolute_ttl: timedelta) -> bool:
        if now > self.expires_at:
            return True
        return now > self.created_at + absolute_ttl

    def require_active(self, now: datetime, absolute_ttl: timedelta) -> None:
        if self.is_revoked:
            raise SessionRevokedError(f"sesion {self.id} revocada")
        if self.is_expired(now, absolute_ttl):
            raise SessionExpiredError(f"sesion {self.id} caducada")

    def touch(self, now: datetime, idle_ttl: timedelta, absolute_ttl: timedelta) -> bool:
        """Desliza `expires_at` hacia `now + idle_ttl`, sin superar el tope
        absoluto anclado en `created_at`. Devuelve `True` solo si el nuevo
        valor se aleja del anterior mas de `TOUCH_PERSISTENCE_THRESHOLD`
        -- la senal que usa el llamador para decidir si merece la pena
        persistir este toque."""
        absolute_deadline = self.created_at + absolute_ttl
        new_expires_at = min(now + idle_ttl, absolute_deadline)
        moved_significantly = abs(new_expires_at - self.expires_at) > TOUCH_PERSISTENCE_THRESHOLD
        self.expires_at = new_expires_at
        return moved_significantly

    def has_fresh_federated_identification(
        self, now: datetime, window: timedelta, absolute_ttl: timedelta
    ) -> bool:
        """Prueba de PRESENCIA reciente ante el proveedor federado (NFR-104).
        Fail-closed: una sesion revocada o caducada nunca la concede, y una
        marca en el futuro (reloj movido hacia atras) tampoco."""
        if self.is_revoked or self.is_expired(now, absolute_ttl):
            return False
        if self.last_federated_auth_at is None:
            return False
        elapsed = now - self.last_federated_auth_at
        return timedelta(0) <= elapsed <= window

    def record_fresh_identification(self, now: datetime) -> None:
        """Marca la identificacion federada (C-81, 002b tasks.md T064: la
        columna sigue llamandose `last_federated_auth_at` -- una migracion
        para renombrarla no compensa el riesgo por un nombre; el metodo si
        se renombra porque hoy es el UNICO llamador el que escribe aqui, y
        es exclusivamente federado). NO desliza `expires_at` ni toca
        `created_at` ni reescribe `origin`: la presencia no alarga la sesion
        (data-model.md §Session, NFR-105)."""
        self.last_federated_auth_at = now

    def revoke(self, now: datetime) -> None:
        self.revoked_at = now
