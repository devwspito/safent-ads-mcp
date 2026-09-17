"""Identificadores del contexto `creative` (data-model.md §CreativeAsset).

`AssetId`/`BriefId`/`JobId` son ULID (ordenables por tiempo de creacion, sin
coordinacion central) porque nombran ficheros en el almacen de activos
(threat-model.md C-28: nombre aleatorio, no derivado del contenido).

`CalendarEventId` y `SignalId` son referencias opacas a agregados de otros
contextos (`catalog`, `signals`). `plan.md §4` permite que `creative` (N2)
dependa de `catalog` (N1), pero `signals` es N3: `creative` nunca puede
importar su dominio sin invertir el grafo de dependencias. Estos VOs
locales son el equivalente opaco (mismo patron que `shared.ids.BusinessId`
y que `economics.domain.identifiers.ProductId` frente a `catalog`),
deliberado incluso ahora que `catalog.domain.calendar_event.CalendarEventId`
existe: `creative` traduce en su propio borde, nunca importa el tipo ajeno.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from ulid import ULID


class AssetIdFormatError(ValueError):
    """La cadena no es un ULID valido para `AssetId`/`BriefId`/`JobId`."""


def _new_ulid_value() -> str:
    return str(ULID())


def _parse_ulid_value(raw: str, *, kind: str) -> str:
    try:
        ULID.from_str(raw)
    except ValueError as exc:
        raise AssetIdFormatError(f"{kind} invalido, se esperaba ULID: {raw!r}") from exc
    return raw


@dataclass(frozen=True, slots=True)
class AssetId:
    """Identidad de un `CreativeAsset`. ULID: 26 caracteres, ordenable por tiempo."""

    value: str

    @classmethod
    def new(cls) -> AssetId:
        return cls(_new_ulid_value())

    @classmethod
    def parse(cls, raw: str) -> AssetId:
        return cls(_parse_ulid_value(raw, kind="AssetId"))

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class BriefId:
    """Identidad de un `CreativeBrief`."""

    value: str

    @classmethod
    def new(cls) -> BriefId:
        return cls(_new_ulid_value())

    @classmethod
    def parse(cls, raw: str) -> BriefId:
        return cls(_parse_ulid_value(raw, kind="BriefId"))

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class JobId:
    """Identidad de un `CreativeJob` (trabajo GPU)."""

    value: str

    @classmethod
    def new(cls) -> JobId:
        return cls(_new_ulid_value())

    @classmethod
    def parse(cls, raw: str) -> JobId:
        return cls(_parse_ulid_value(raw, kind="JobId"))

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class CalendarEventId:
    """Referencia opaca a un `CalendarEvent` de `catalog`."""

    value: uuid.UUID

    @classmethod
    def parse(cls, raw: str) -> CalendarEventId:
        return cls(uuid.UUID(raw))

    def __str__(self) -> str:
        return str(self.value)


@dataclass(frozen=True, slots=True)
class SignalId:
    """Referencia opaca a un `Signal` de `signals`."""

    value: uuid.UUID

    @classmethod
    def parse(cls, raw: str) -> SignalId:
        return cls(uuid.UUID(raw))

    def __str__(self) -> str:
        return str(self.value)


# `creative-port.md` usa `AssetRef` para referenciar un `CreativeAsset` ya
# existente (p.ej. `ImageSpec.reference_assets`, `VideoSpec.key_frames`,
# `PolicyCheckPort.check`). Es la misma identidad que `AssetId`: alias, no
# un tipo nuevo, para no duplicar validacion de formato.
AssetRef = AssetId
