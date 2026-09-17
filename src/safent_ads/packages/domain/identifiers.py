"""Identificadores del contexto `packages` (data-model.md `CampaignPackage`,
`PackagePublication`).

`PackageId` es ULID, mismo patron que `creative.domain.identifiers.AssetId`
(ordenable por tiempo, T012). `OfferingId`/`PackageGroupId` son referencias
opacas: `catalog` no esta entre las dependencias permitidas de `packages`
(data-model.md "Bounded contexts": `packages -> {proposals, execution,
creative, accounts, shared}"), asi que `packages` nunca importa el dominio
de `catalog` -- solo guarda el identificador opaco que el agente declara,
igual que `creative.domain.identifiers.CalendarEventId`/`SignalId` frente a
`catalog`/`signals`."""

from __future__ import annotations

import re
from dataclasses import dataclass

from ulid import ULID

_MAX_OPAQUE_ID_LENGTH = 128
_OPAQUE_ID_PATTERN = re.compile(rf"^[A-Za-z0-9_-]{{1,{_MAX_OPAQUE_ID_LENGTH}}}$")


class PackageIdFormatError(ValueError):
    """La cadena no es un ULID valido para `PackageId`."""


class OpaqueIdFormatError(ValueError):
    """La cadena no respeta el formato de identificador opaco."""


@dataclass(frozen=True, slots=True)
class PackageId:
    """Identidad de un `CampaignPackage`. ULID: 26 caracteres, ordenable
    por tiempo de creacion, sin coordinacion central."""

    value: str

    @classmethod
    def new(cls) -> PackageId:
        return cls(str(ULID()))

    @classmethod
    def parse(cls, raw: str) -> PackageId:
        try:
            ULID.from_str(raw)
        except ValueError as exc:
            raise PackageIdFormatError(f"PackageId invalido, se esperaba ULID: {raw!r}") from exc
        return cls(raw)

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class OfferingId:
    """Referencia opaca a una oferta del catalogo del negocio
    (mcp-tools.md §2 `offering_id: OpaqueId`)."""

    value: str

    def __post_init__(self) -> None:
        if not _OPAQUE_ID_PATTERN.fullmatch(self.value):
            raise OpaqueIdFormatError(f"OfferingId invalido: {self.value!r}")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class PackageGroupId:
    """Reservado: dos paquetes hermanos comparten grupo
    (data-model.md `package_group_id`)."""

    value: str

    def __post_init__(self) -> None:
        if not _OPAQUE_ID_PATTERN.fullmatch(self.value):
            raise OpaqueIdFormatError(f"PackageGroupId invalido: {self.value!r}")

    def __str__(self) -> str:
        return self.value
