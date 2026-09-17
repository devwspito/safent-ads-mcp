"""Identificadores canonicos que cruzan bounded contexts: BusinessId y
EntityRef (data-model.md), y el puerto IdGenerator (plan.md N0)."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class PlatformCode(StrEnum):
    """Plataforma publicitaria soportada."""

    GOOGLE = "google"
    META = "meta"


class EntityLevel(StrEnum):
    """Nivel de una entidad publicitaria dentro de su jerarquia."""

    ACCOUNT = "account"
    CAMPAIGN = "campaign"
    AD_SET = "ad_set"
    AD = "ad"
    CREATIVE = "creative"


_ENTITY_REF_PATTERN = re.compile(r"^(?P<platform>[a-z]+):(?P<level>[a-z_]+):(?P<external_id>.+)$")


class EntityRefFormatError(ValueError):
    """La cadena no respeta el formato `<platform>:<level>:<external_id>`."""


@dataclass(frozen=True, slots=True)
class EntityRef:
    """Referencia canonica a una entidad publicitaria (data-model.md).

    Unico identificador que cruza contextos: `<platform>:<level>:<external_id>`.
    """

    platform: PlatformCode
    level: EntityLevel
    external_id: str
    business_id: uuid.UUID | None = None
    connection_id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        if not self.external_id:
            raise EntityRefFormatError("external_id vacio")
        if (self.business_id is None) != (self.connection_id is None):
            raise EntityRefFormatError("business_id y connection_id deben viajar juntos")

    @classmethod
    def parse(cls, raw: str) -> EntityRef:
        match = _ENTITY_REF_PATTERN.match(raw)
        if match is None:
            raise EntityRefFormatError(f"formato invalido: {raw!r}")
        try:
            platform = PlatformCode(match.group("platform"))
            level = EntityLevel(match.group("level"))
        except ValueError as exc:
            raise EntityRefFormatError(f"formato invalido: {raw!r}") from exc
        external_id = match.group("external_id")
        scoped = external_id.split(":", 2)
        if len(scoped) == 3:  # noqa: PLR2004 - business, connection, remote identifier
            try:
                business_id, connection_id = uuid.UUID(scoped[0]), uuid.UUID(scoped[1])
            except ValueError:
                pass
            else:
                return cls(platform, level, scoped[2], business_id, connection_id)
        return cls(platform=platform, level=level, external_id=external_id)

    def __str__(self) -> str:
        if self.connection_id is not None:
            return (
                f"{self.platform.value}:{self.level.value}:"
                f"{self.business_id}:{self.connection_id}:{self.external_id}"
            )
        return f"{self.platform.value}:{self.level.value}:{self.external_id}"


@dataclass(frozen=True, slots=True)
class BusinessId:
    """Identidad de un negocio. Opaca fuera de `accounts`."""

    value: uuid.UUID

    @classmethod
    def new(cls) -> BusinessId:
        return cls(uuid.uuid4())

    @classmethod
    def parse(cls, raw: str) -> BusinessId:
        return cls(uuid.UUID(raw))

    def __str__(self) -> str:
        return str(self.value)


class IdGenerator(Protocol):
    """Puerto: genera identificadores opacos para nuevos agregados."""

    def new_id(self) -> uuid.UUID: ...


class UuidIdGenerator:
    """Implementacion por defecto de `IdGenerator` sobre `uuid.uuid4`."""

    def new_id(self) -> uuid.UUID:
        return uuid.uuid4()
