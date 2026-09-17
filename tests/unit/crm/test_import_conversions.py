"""`ImportConversionsFromCsv` (T220, `POST /conversions/import`): parseo
fila a fila, dedupe (`save()` idempotente sobre la clave natural, ver
`tests/contracts/crm/test_lead_attribution_repository.py`) y rechazo con
motivo sin tirar el resto del fichero."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from safent_ads.crm.application.import_conversions import (
    CsvHeaderError,
    ImportConversionsFromCsv,
)
from safent_ads.crm.testing.in_memory_repositories import InMemoryLeadAttributionRepository
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import BusinessId

_BUSINESS_ID = BusinessId.new()
_NOW = datetime(2026, 9, 10, tzinfo=UTC)


class _FixedIdentitySalt:
    def for_business(self, business_id: BusinessId) -> str:  # noqa: ARG002
        return "fixed-salt"


def _use_case() -> tuple[ImportConversionsFromCsv, InMemoryLeadAttributionRepository]:
    attributions = InMemoryLeadAttributionRepository()
    use_case = ImportConversionsFromCsv(
        attributions=attributions, identity_salt=_FixedIdentitySalt(), clock=FixedClock(_NOW)
    )
    return use_case, attributions


async def test_imports_valid_rows() -> None:
    use_case, attributions = _use_case()
    csv_text = (
        "occurred_at,kind,amount_minor,email\n"
        "2026-09-01,lead,,a@x.com\n"
        "2026-09-02,business_conversion,110000,b@x.com\n"
    )

    result = await use_case.execute(business_id=_BUSINESS_ID, csv_text=csv_text)

    assert result.imported == 2
    assert result.duplicates == 0
    assert result.rejected == []
    assert len(attributions._rows) == 2  # noqa: SLF001 - inspeccion directa del doble


async def test_reimporting_the_same_file_reports_duplicates() -> None:
    use_case, _ = _use_case()
    csv_text = "occurred_at,kind,email\n2026-09-01,lead,a@x.com\n"

    await use_case.execute(business_id=_BUSINESS_ID, csv_text=csv_text)
    second = await use_case.execute(business_id=_BUSINESS_ID, csv_text=csv_text)

    assert second.imported == 0
    assert second.duplicates == 1


async def test_rejects_bad_rows_without_stopping_the_rest() -> None:
    use_case, _ = _use_case()
    csv_text = (
        "occurred_at,kind,email\n"
        "2026-09-01,lead,a@x.com\n"
        "not-a-date,lead,b@x.com\n"
        "2026-09-03,unknown_kind,c@x.com\n"
        "2026-09-04,lead,\n"
    )

    result = await use_case.execute(business_id=_BUSINESS_ID, csv_text=csv_text)

    assert result.imported == 1
    assert [row.line for row in result.rejected] == [3, 4, 5]
    assert "occurred_at" in result.rejected[0].reason
    assert "kind" in result.rejected[1].reason
    assert "email" in result.rejected[2].reason.lower() or "phone" in result.rejected[2].reason


async def test_missing_required_columns_rejects_the_whole_file() -> None:
    use_case, _ = _use_case()

    with pytest.raises(CsvHeaderError):
        await use_case.execute(business_id=_BUSINESS_ID, csv_text="email\na@x.com\n")


async def test_date_only_occurred_at_defaults_to_midnight_utc() -> None:
    use_case, attributions = _use_case()
    csv_text = "occurred_at,kind,email\n2026-09-01,lead,a@x.com\n"

    await use_case.execute(business_id=_BUSINESS_ID, csv_text=csv_text)

    (saved,) = attributions._rows.values()  # noqa: SLF001
    assert saved.occurred_at == datetime(2026, 9, 1, tzinfo=UTC)
