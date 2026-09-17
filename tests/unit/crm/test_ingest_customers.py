"""`IngestCustomers` (spec 027 T016): `IDENTITY_NOT_HASHED` en 400, lote
> 1000 rechazado, reingestar la misma identidad no duplica la fila."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from safent_ads.crm.application.ingest_customers import (
    CustomerBatchTooLargeError,
    CustomerIngestItem,
    IngestCustomers,
)
from safent_ads.crm.testing.in_memory_repositories import (
    InMemoryCustomerRepository,
    InMemoryIdentityMappingRepository,
)
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import BusinessId

_BUSINESS_ID = BusinessId.new()
_VALID_DIGEST = "a" * 64
_CONNECTOR_ID = "connector-crm"


def _use_case() -> IngestCustomers:
    return IngestCustomers(
        customers=InMemoryCustomerRepository(),
        identity_mappings=InMemoryIdentityMappingRepository(),
        clock=FixedClock(datetime(2026, 3, 1, tzinfo=UTC)),
    )


def _item(**overrides: object) -> CustomerIngestItem:
    defaults: dict[str, object] = {
        "identity_digest": _VALID_DIGEST,
        "salt_version": 1,
        "entity_ref": None,
        "attribution_rung": "aggregate",
    }
    defaults.update(overrides)
    return CustomerIngestItem(**defaults)  # type: ignore[arg-type]


async def test_new_identity_is_accepted() -> None:
    result = await _use_case().execute(
        business_id=_BUSINESS_ID, connector_id=_CONNECTOR_ID, items=[_item()]
    )

    assert result.accepted == 1
    assert result.merged == 0
    assert result.rejected == ()


async def test_reingesting_the_same_identity_merges_instead_of_duplicating() -> None:
    use_case = _use_case()
    await use_case.execute(business_id=_BUSINESS_ID, connector_id=_CONNECTOR_ID, items=[_item()])

    result = await use_case.execute(
        business_id=_BUSINESS_ID, connector_id=_CONNECTOR_ID, items=[_item()]
    )

    assert result.accepted == 0
    assert result.merged == 1


async def test_raw_identifier_is_rejected_as_identity_not_hashed() -> None:
    result = await _use_case().execute(
        business_id=_BUSINESS_ID,
        connector_id=_CONNECTOR_ID,
        items=[_item(identity_digest="cliente@example.com")],
    )

    assert result.accepted == 0
    assert len(result.rejected) == 1
    assert result.rejected[0].code == "IDENTITY_NOT_HASHED"


async def test_batch_over_the_limit_is_rejected() -> None:
    items = [_item(identity_digest=f"{i:064x}") for i in range(1_001)]

    with pytest.raises(CustomerBatchTooLargeError):
        await _use_case().execute(business_id=_BUSINESS_ID, connector_id=_CONNECTOR_ID, items=items)
