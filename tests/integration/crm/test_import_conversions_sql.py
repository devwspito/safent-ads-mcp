"""`ImportConversionsFromCsv` contra Postgres real (T220): las filas
importadas aterrizan en `lead_attributions` y la reconciliacion
(`count_by_kind_in_window`, la misma consulta que usa `get_crm_
reconciliation`) las lee sin cableado adicional."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.crm.application.import_conversions import ImportConversionsFromCsv
from safent_ads.crm.domain.conversion_kind import ConversionKind
from safent_ads.crm.infrastructure.identity_salt import HkdfIdentitySalt
from safent_ads.crm.infrastructure.sql_repositories import SqlLeadAttributionRepository
from safent_ads.shared.clock import SystemClock
from safent_ads.shared.ids import BusinessId
from tests.conftest import BusinessFactory

pytestmark = pytest.mark.integration


async def test_imported_rows_are_visible_to_reconciliation(
    db_session: AsyncSession, business_factory: BusinessFactory
) -> None:
    business_id = await business_factory.create()
    typed_business_id = BusinessId(business_id)
    use_case = ImportConversionsFromCsv(
        attributions=SqlLeadAttributionRepository(db_session),
        identity_salt=HkdfIdentitySalt(b"0" * 32),
        clock=SystemClock(),
    )
    csv_text = (
        "occurred_at,kind,amount_minor,email\n"
        "2026-03-01,lead,,a@x.com\n"
        "2026-03-02,business_conversion,110000,b@x.com\n"
    )

    result = await use_case.execute(business_id=typed_business_id, csv_text=csv_text)
    await db_session.flush()

    assert result.imported == 2
    conversions = await SqlLeadAttributionRepository(db_session).count_by_kind_in_window(
        business_id=typed_business_id,
        conversion_kind=ConversionKind.BUSINESS_CONVERSION,
        window_start=date(2026, 3, 1),
        window_end=date(2026, 3, 31),
    )
    leads = await SqlLeadAttributionRepository(db_session).count_by_kind_in_window(
        business_id=typed_business_id,
        conversion_kind=ConversionKind.LEAD,
        window_start=date(2026, 3, 1),
        window_end=date(2026, 3, 31),
    )
    assert conversions == 1
    assert leads == 1


async def test_reimport_is_idempotent_against_real_postgres(
    db_session: AsyncSession, business_factory: BusinessFactory
) -> None:
    business_id = await business_factory.create()
    typed_business_id = BusinessId(business_id)
    use_case = ImportConversionsFromCsv(
        attributions=SqlLeadAttributionRepository(db_session),
        identity_salt=HkdfIdentitySalt(b"0" * 32),
        clock=SystemClock(),
    )
    csv_text = "occurred_at,kind,email\n2026-03-01,lead,a@x.com\n"

    await use_case.execute(business_id=typed_business_id, csv_text=csv_text)
    second = await use_case.execute(business_id=typed_business_id, csv_text=csv_text)

    assert second.imported == 0
    assert second.duplicates == 1
