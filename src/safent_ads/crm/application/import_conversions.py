"""`ImportConversionsFromCsv` (T220, `POST /conversions/import`): el CRM del
propietario exportado a CSV entra por aqui -- columnas documentadas en
contracts/rest-api.md §Conversiones: `occurred_at, kind, amount_minor?,
currency?, offering_id?, gclid?, fbclid?, external_ref?, email?|phone?`.

Fila a fila (nunca todo o nada): una fila mal formada se rechaza con su
motivo y el resto del fichero se sigue procesando -- el propietario
exporta de un CRM ajeno, un separador decimal raro en una fila no debe
tirar las otras 500 buenas."""

from __future__ import annotations

import csv
import io
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime

from safent_ads.crm.application.conversion_ingestion import (
    ConversionRow,
    ConversionRowError,
    build_conversion_signal,
    resolve_conversion_attribution,
)
from safent_ads.crm.application.ports import IdentitySaltProvider, LeadAttributionRepository
from safent_ads.crm.domain.conversion_kind import ConversionKind
from safent_ads.shared.clock import Clock
from safent_ads.shared.errors import ApplicationError
from safent_ads.shared.ids import BusinessId

_REQUIRED_COLUMNS = frozenset({"occurred_at", "kind"})
_HEADER_LINE = 1


class CsvHeaderError(ApplicationError):
    """El CSV no trae las columnas minimas (`occurred_at`, `kind`)."""


@dataclass(frozen=True, kw_only=True, slots=True)
class RejectedRow:
    line: int
    reason: str


@dataclass(frozen=True, kw_only=True, slots=True)
class ImportResult:
    imported: int
    duplicates: int
    rejected: list[RejectedRow]


class ImportConversionsFromCsv:
    def __init__(
        self,
        *,
        attributions: LeadAttributionRepository,
        identity_salt: IdentitySaltProvider,
        clock: Clock,
    ) -> None:
        self._attributions = attributions
        self._identity_salt = identity_salt
        self._clock = clock

    async def execute(self, *, business_id: BusinessId, csv_text: str) -> ImportResult:
        reader = csv.DictReader(io.StringIO(csv_text))
        _require_expected_header(reader.fieldnames)
        salt = self._identity_salt.for_business(business_id)
        observed_at = self._clock.now()

        imported = duplicates = 0
        rejected: list[RejectedRow] = []
        for line, raw_row in enumerate(reader, start=_HEADER_LINE + 1):
            try:
                inserted = await self._import_row(business_id, raw_row, salt, observed_at)
            except ConversionRowError as exc:
                rejected.append(RejectedRow(line=line, reason=str(exc)))
                continue
            if inserted:
                imported += 1
            else:
                duplicates += 1
        return ImportResult(imported=imported, duplicates=duplicates, rejected=rejected)

    async def _import_row(
        self,
        business_id: BusinessId,
        raw_row: Mapping[str, str | None],
        salt: str,
        observed_at: datetime,
    ) -> bool:
        row = _parse_csv_row(raw_row)
        signal = build_conversion_signal(business_id, row, salt=salt, observed_at=observed_at)
        attribution = resolve_conversion_attribution(signal)
        return await self._attributions.save(attribution)


def _require_expected_header(fieldnames: Sequence[str] | None) -> None:
    present = set(fieldnames or ())
    missing = _REQUIRED_COLUMNS - present
    if missing:
        raise CsvHeaderError(f"faltan columnas obligatorias: {sorted(missing)}")


def _parse_csv_row(raw_row: Mapping[str, str | None]) -> ConversionRow:
    return ConversionRow(
        kind=_parse_kind(raw_row.get("kind")),
        occurred_at=_parse_occurred_at(raw_row.get("occurred_at")),
        amount_minor=_parse_amount_minor(raw_row.get("amount_minor")),
        currency=_clean(raw_row.get("currency")) or "",
        offering_id=_clean(raw_row.get("offering_id")),
        gclid=_clean(raw_row.get("gclid")),
        fbclid=_clean(raw_row.get("fbclid")),
        external_ref=_clean(raw_row.get("external_ref")),
        email=_clean(raw_row.get("email")),
        phone=_clean(raw_row.get("phone")),
    )


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _parse_kind(raw: str | None) -> ConversionKind:
    value = _clean(raw)
    if value is None:
        raise ConversionRowError("kind vacio")
    try:
        return ConversionKind(value)
    except ValueError as exc:
        raise ConversionRowError(f"kind desconocido: {value!r}") from exc


def _parse_occurred_at(raw: str | None) -> datetime:
    value = _clean(raw)
    if value is None:
        raise ConversionRowError("occurred_at vacio")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        parsed = _parse_date_only(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _parse_date_only(value: str) -> datetime:
    try:
        as_date = date.fromisoformat(value)
    except ValueError as exc:
        raise ConversionRowError(f"occurred_at no es una fecha/hora ISO-8601: {value!r}") from exc
    return datetime(as_date.year, as_date.month, as_date.day, tzinfo=UTC)


def _parse_amount_minor(raw: str | None) -> int:
    value = _clean(raw)
    if value is None:
        return 0
    try:
        amount = int(value)
    except ValueError as exc:
        raise ConversionRowError(f"amount_minor no es un entero: {value!r}") from exc
    if amount < 0:
        raise ConversionRowError(f"amount_minor debe ser >= 0: {amount}")
    return amount
