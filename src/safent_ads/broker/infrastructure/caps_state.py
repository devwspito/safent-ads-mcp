"""Almacen de los topes que el propietario fija DESDE EL PANEL
(`ADS_BROKER_CAPS_STATE_DIR`, spec 008 T029). Lo escribe unicamente el
proceso `ads-broker`: `ads-api` ni lo lee ni lo tiene montado -- pide por
el socket y el broker decide, valida contra el sobre y escribe.

Referencia de despliegue: `/var/lib/ads-broker/caps-state`, DENTRO del
volumen `credential-store` que ya existe. El contenedor del broker es
`read_only: true` (hace falta un volumen real), ningun otro servicio monta
ese volumen, y `ops/backup.sh` ya lo archiva entero -- el respaldo de los
topes sale gratis.

Propiedades que este modulo sostiene, todas apoyadas en el teorema de
`broker/domain/hard_caps_policy.py` (ignorar el estado del panel es siempre
igual o mas restrictivo que aplicarlo):

- **i4**: documento corrupto, ilegible, con `schema_version` desconocida, o
  directorio con modo != 0700 u otro propietario ⇒ se ignora el documento
  ENTERO, nunca por entradas -- un parseo parcial es una palanca del
  atacante. Se registra ruidosamente, `panel_state_digest` va a `null` y
  las escrituras siguen resolviendose por fichero.
- **i5**: directorio no escribible ⇒ `CapsStateUnwritableError` y la
  instantanea en memoria **no** se actualiza. Escritura
  escribir-y-luego-intercambiar: temporal en el mismo directorio, `fsync`
  del fichero, `os.replace`, `fsync` del directorio.
- **i6**: `mutate()` mantiene el MISMO cerrojo durante la comprobacion y la
  escritura, asi que dos peticiones concurrentes con una sola plaza libre
  no pueden aceptarse las dos.
- **D13**: el documento lleva `schema_version: 1` porque SI sobrevive a un
  despliegue, a un `restore.sh` y a un rollback de imagen -- al reves que
  los sobres del socket, donde los dos extremos viajan en los mismos bytes
  de imagen."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Final

import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from safent_ads.broker.domain.account_key import (
    MAX_PLATFORM_ACCOUNT_ID_LENGTH,
    InvalidPlatformAccountIdError,
    canonical_platform_account_id,
)
from safent_ads.broker.domain.hard_caps_policy import PanelAmounts
from safent_ads.broker.infrastructure.caps_config import MAX_MINOR_AMOUNT
from safent_ads.shared.errors import InfrastructureError

logger = structlog.get_logger(__name__)

STATE_SCHEMA_VERSION: Final = 1
_STATE_FILENAME: Final = "panel-caps.json"
_REQUIRED_DIR_MODE: Final = 0o700
# Un documento de topes del panel es diminuto (decenas de cuentas como
# mucho, acotadas por `max_accounts`). Este techo evita que un fichero
# manipulado obligue a leer megabytes antes de rechazarlo.
_MAX_STATE_BYTES: Final = 1 * 1024 * 1024
# El contador de cambios solo necesita el dia en curso; se conservan unos
# pocos por diagnostico y el resto se poda en cada escritura.
_RETAINED_CHANGE_DAYS: Final = 7

__all__ = [
    "STATE_SCHEMA_VERSION",
    "CapsStateStore",
    "CapsStateUnwritableError",
    "PanelAccountCaps",
    "PanelCapsSnapshot",
    "assert_state_directory_is_private",
]


class CapsStateUnwritableError(InfrastructureError):
    """El directorio de estado no admite la escritura durable. Fail-closed:
    nada ha cambiado, ni en disco ni en la memoria del broker."""


class PanelAccountCaps(BaseModel):
    """Lo que el panel fijo para UNA cuenta. `strict=True`: un documento
    manipulado con `"3"` o `3.0` en un importe no pasa de aqui."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    daily_cap_minor: int = Field(ge=0, le=MAX_MINOR_AMOUNT)
    monthly_cap_minor: int = Field(ge=0, le=MAX_MINOR_AMOUNT)
    ceiling_minor: int = Field(ge=0, le=MAX_MINOR_AMOUNT)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    # `strict=False` SOLO aqui: en JSON una fecha es una cadena ISO-8601, y
    # el modo estricto rechazaria la que el propio broker acaba de escribir.
    # Los importes siguen estrictos, que es donde el modo laxo dolia.
    updated_at: datetime = Field(strict=False)
    updated_by: str = Field(min_length=1, max_length=128)

    def amounts(self) -> PanelAmounts:
        return PanelAmounts(
            daily_cap_minor=self.daily_cap_minor,
            monthly_cap_minor=self.monthly_cap_minor,
            ceiling_minor=self.ceiling_minor,
        )


class _PersistedState(BaseModel):
    """El documento tal cual vive en disco."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: int
    accounts: dict[str, PanelAccountCaps] = Field(default_factory=dict)
    changes_by_day: dict[str, int] = Field(default_factory=dict)


@dataclass(frozen=True, slots=True, kw_only=True)
class PanelCapsSnapshot:
    """Instantanea EN MEMORIA del estado del panel. `available=False`
    significa "no se pudo leer": se resuelve solo con el fichero de topes,
    nunca con el valor del panel."""

    available: bool
    accounts: Mapping[str, PanelAccountCaps] = field(default_factory=dict)
    changes_by_day: Mapping[str, int] = field(default_factory=dict)
    digest: str | None = None

    def amounts_for(self, canonical_account_id: str) -> PanelAmounts | None:
        entry = self.accounts.get(canonical_account_id)
        return None if entry is None else entry.amounts()

    def changes_on(self, day: date) -> int:
        return self.changes_by_day.get(day.isoformat(), 0)

    @property
    def accounts_count(self) -> int:
        return len(self.accounts)


def _unavailable(reason: str) -> PanelCapsSnapshot:
    logger.warning("broker_caps_state_unavailable", reason=reason)
    return PanelCapsSnapshot(available=False)


def assert_state_directory_is_private(directory: Path) -> None:
    """Crea el directorio 0700 si no existe y comprueba modo y propietario
    al arrancar. `mkdir(mode=...)` no basta: el umask muerde, por eso va
    ademas un `chmod` explicito."""
    directory.mkdir(mode=_REQUIRED_DIR_MODE, parents=True, exist_ok=True)
    directory.chmod(_REQUIRED_DIR_MODE)
    info = directory.stat()
    if stat.S_IMODE(info.st_mode) != _REQUIRED_DIR_MODE:
        raise CapsStateUnwritableError(
            f"{directory} debe tener modo 0700 (ADS_BROKER_CAPS_STATE_DIR)"
        )
    if info.st_uid != os.getuid():
        raise CapsStateUnwritableError(
            f"{directory} debe pertenecer al usuario de ads-broker "
            f"(ADS_BROKER_CAPS_STATE_DIR, uid {info.st_uid} != {os.getuid()})"
        )


class CapsStateStore:
    """Unico escritor del estado de topes del panel."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._path = directory / _STATE_FILENAME
        self._lock = asyncio.Lock()
        self._snapshot = _load_snapshot(directory, self._path)

    @property
    def path(self) -> Path:
        return self._path

    def snapshot(self) -> PanelCapsSnapshot:
        """Se relee en cada resolucion (nunca cacheada en el llamante), de
        modo que una bajada ya aplicada gana sobre una escritura en vuelo
        (i7)."""
        return self._snapshot

    def reload(self) -> PanelCapsSnapshot:
        self._snapshot = _load_snapshot(self._directory, self._path)
        return self._snapshot

    async def mutate(
        self, mutation: Callable[[PanelCapsSnapshot], PanelCapsSnapshot | None]
    ) -> PanelCapsSnapshot:
        """Comprobacion y escritura BAJO EL MISMO CERROJO (i6). `mutation`
        aplica la politica y puede lanzar para rechazar, o devolver `None`
        para decir "nada que escribir": en los dos casos el disco no se toca.
        La instantanea en memoria solo se sustituye cuando la escritura
        durable ha ido bien (i5).

        **I-1 (revision de seguridad T035).** Con el documento ilegible
        (`available=False`: version desconocida, corrupto, permisos) toda
        mutacion se RECHAZA. Escribir sobre el partiendo de una instantanea
        vacia lo destruiria: los topes de las demas cuentas y el contador de
        `max_cap_changes_per_day` ya gastado desaparecerian, y un `ads-api`
        comprometido tendria en ese borrado un presupuesto de cambios nuevo.
        Leer puede caer al fichero -- eso es siempre mas restrictivo --, pero
        escribir a ciegas no tiene una version segura. Se comprueba DENTRO
        del cerrojo, junto al resto."""
        async with self._lock:
            if not self._snapshot.available:
                raise CapsStateUnwritableError(
                    f"{self._path} no se puede leer: ninguna escritura de topes puede "
                    "partir de un estado desconocido (ADS_BROKER_CAPS_STATE_DIR)"
                )
            proposed = mutation(self._snapshot)
            if proposed is None:
                return self._snapshot
            durable = self._write(proposed)
            self._snapshot = durable
            return durable

    def _write(self, proposed: PanelCapsSnapshot) -> PanelCapsSnapshot:
        payload = _serialize(proposed)
        _write_then_swap(self._directory, self._path, payload)
        return PanelCapsSnapshot(
            available=True,
            accounts=dict(proposed.accounts),
            changes_by_day=dict(proposed.changes_by_day),
            digest=hashlib.sha256(payload).hexdigest(),
        )


def prune_change_days(changes_by_day: Mapping[str, int], *, today: date) -> dict[str, int]:
    """Conserva el dia en curso y unos pocos anteriores: el contador de
    `max_cap_changes_per_day` solo mira hoy, y un documento que crece sin
    limite es otra forma de fallo."""
    kept = {
        day: count
        for day, count in changes_by_day.items()
        if _is_recent(day, today=today) and count > 0
    }
    return dict(sorted(kept.items()))


def _is_recent(day: str, *, today: date) -> bool:
    try:
        parsed = date.fromisoformat(day)
    except ValueError:
        return False
    delta = (today - parsed).days
    return 0 <= delta < _RETAINED_CHANGE_DAYS


def _serialize(snapshot: PanelCapsSnapshot) -> bytes:
    document = {
        "schema_version": STATE_SCHEMA_VERSION,
        "accounts": {
            account: caps.model_dump(mode="json") for account, caps in snapshot.accounts.items()
        },
        "changes_by_day": dict(snapshot.changes_by_day),
    }
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _write_then_swap(directory: Path, path: Path, payload: bytes) -> None:
    """Temporal en el MISMO directorio (`os.replace` solo es atomico dentro
    del mismo sistema de ficheros), `fsync` del fichero, `os.replace`,
    `fsync` del directorio."""
    handle = None
    try:
        descriptor, raw_temporary = tempfile.mkstemp(dir=directory, prefix=".panel-caps-")
        temporary = Path(raw_temporary)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
        _fsync_directory(directory)
    except OSError as exc:
        raise CapsStateUnwritableError(f"no se pudo escribir {path}: {exc.strerror}") from exc


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _load_snapshot(directory: Path, path: Path) -> PanelCapsSnapshot:
    """Nunca lanza: cualquier problema se traduce en "no disponible", que
    por el teorema es el lado seguro."""
    try:
        rejection = _directory_rejection(directory)
        if rejection is not None:
            return _unavailable(rejection)
        if not path.is_file():
            # Directorio listo y todavia sin documento: estado vacio, no un
            # fallo -- la primera escritura lo creara.
            return PanelCapsSnapshot(available=True, digest=None)
        if path.stat().st_size > _MAX_STATE_BYTES:
            return _unavailable("state_too_large")
        raw = path.read_bytes()
    except OSError:
        return _unavailable("state_unreadable")
    return _parse_snapshot(raw)


def _directory_rejection(directory: Path) -> str | None:
    if not directory.is_dir():
        return "state_dir_missing"
    info = directory.stat()
    if stat.S_IMODE(info.st_mode) != _REQUIRED_DIR_MODE:
        return "state_dir_mode"
    if info.st_uid != os.getuid():
        return "state_dir_owner"
    return None


def _parse_snapshot(raw: bytes) -> PanelCapsSnapshot:
    try:
        document = json.loads(raw, parse_constant=_reject_json_constant)
    except ValueError:
        return _unavailable("state_unparseable")
    if not isinstance(document, dict):
        return _unavailable("state_not_an_object")
    if document.get("schema_version") != STATE_SCHEMA_VERSION:
        # Version desconocida: se ignora el documento ENTERO. Descartar es
        # seguro por el teorema; parsear a medias es la palanca (D13).
        return _unavailable("state_schema_version")
    try:
        parsed = _PersistedState.model_validate(document)
    except ValidationError:
        return _unavailable("state_invalid_schema")
    accounts = _canonical_accounts(parsed.accounts)
    if accounts is None:
        return _unavailable("state_non_canonical_account_key")
    return PanelCapsSnapshot(
        available=True,
        accounts=accounts,
        changes_by_day=dict(parsed.changes_by_day),
        digest=hashlib.sha256(raw).hexdigest(),
    )


def _canonical_accounts(
    accounts: Mapping[str, PanelAccountCaps],
) -> dict[str, PanelAccountCaps] | None:
    """Una clave que no venga ya canonica es un documento manipulado: el
    broker es el unico escritor y siempre canonicaliza antes de guardar.
    Dos claves distintas que colapsaran en la misma tampoco se resuelven
    "eligiendo una"."""
    canonical: dict[str, PanelAccountCaps] = {}
    for key, caps in accounts.items():
        if len(key) > MAX_PLATFORM_ACCOUNT_ID_LENGTH:
            return None
        try:
            if canonical_platform_account_id(key) != key:
                return None
        except InvalidPlatformAccountIdError:
            return None
        canonical[key] = caps
    return canonical


def _reject_json_constant(name: str) -> None:
    raise ValueError(f"json_constant_not_allowed:{name}")
