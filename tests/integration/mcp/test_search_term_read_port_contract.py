"""`BrokerSearchTermReadPort` (P2, tool-surface.md §2.1): igual que
`test_gaql_read_port_contract.py`, la comprobacion IDOR es SQL-only (pide
filas reales de `platform_accounts`), asi que este banco prueba el puerto
real con un `AdsPlatformPort` falso inyectado -- nunca toca un socket de
verdad (`mcp/testing/fakes.py` documenta el mismo principio)."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.contracts.sql_fixtures import account_external_id, campaign_ref, seed_entity

from safent_ads.accounts.domain.refs import AccountRef
from safent_ads.mcp.application.dto import Window, WindowPreset
from safent_ads.mcp.application.errors import EntityNotFoundError
from safent_ads.mcp.infrastructure.broker_search_term_read_port import BrokerSearchTermReadPort
from safent_ads.shared.clock import FixedClock

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 9, 15, 12, tzinfo=UTC)
_WINDOW = Window(preset=WindowPreset.SEVEN_DAYS, lag_days=0, date_from=None, date_to=None)
_QUERY_TEMPLATE = (
    "SELECT search_term_view.search_term, campaign.resource_name, ad_group.resource_name, "
    "metrics.cost_micros, metrics.conversions, customer.currency_code FROM search_term_view"
)
_ROW: dict[str, Any] = {
    "search_term_view.search_term": "zapatillas running baratas",
    "campaign.resource_name": "customers/111/campaigns/1",
    "ad_group.resource_name": "customers/111/adGroups/2",
    "metrics.cost_micros": 1_000_000,
    "metrics.conversions": 1.0,
    "customer.currency_code": "EUR",
}


class _FakeAdsPlatformPort:
    def __init__(self) -> None:
        self.calls: list[tuple[AccountRef, str, int]] = []

    async def run_gaql(
        self, account_ref: AccountRef, query: str, *, max_rows: int
    ) -> Sequence[Mapping[str, Any]]:
        self.calls.append((account_ref, query, max_rows))
        return [_ROW]


async def _seed_account(
    factory: async_sessionmaker[AsyncSession], *, platform_value: str
) -> tuple[str, str]:
    entity_ref = campaign_ref(f"st{uuid.uuid4().hex[:8]}", platform_value=platform_value)
    async with factory() as session:
        business_id = await seed_entity(session, entity_ref)
        await session.commit()
    account_ref = f"{platform_value}:{account_external_id(entity_ref)}"
    return str(business_id), account_ref


async def test_list_search_terms_delegates_to_the_platform_port_for_a_google_account(
    mcp_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    business_id, account_ref = await _seed_account(mcp_session_factory, platform_value="google")
    platform_port = _FakeAdsPlatformPort()
    port = BrokerSearchTermReadPort(
        platform_port, mcp_session_factory, FixedClock(_NOW), query_template=_QUERY_TEMPLATE
    )

    result = await port.list_search_terms(business_id, account_ref, window=_WINDOW)

    assert result.is_supported is True
    assert result.reason is None
    assert len(result.terms) == 1
    assert result.terms[0].term == "zapatillas running baratas"
    assert len(platform_port.calls) == 1
    sent_ref, sent_query, max_rows = platform_port.calls[0]
    assert str(sent_ref) == account_ref
    assert sent_query.startswith(_QUERY_TEMPLATE)
    assert "segments.date BETWEEN" in sent_query
    assert "LIMIT 500" in sent_query
    assert max_rows == 500


async def test_list_search_terms_returns_a_typed_not_supported_result_for_meta(
    mcp_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    business_id, account_ref = await _seed_account(mcp_session_factory, platform_value="meta")
    platform_port = _FakeAdsPlatformPort()
    port = BrokerSearchTermReadPort(
        platform_port, mcp_session_factory, FixedClock(_NOW), query_template=_QUERY_TEMPLATE
    )

    result = await port.list_search_terms(business_id, account_ref, window=_WINDOW)

    assert result.is_supported is False
    assert result.reason == "platform_not_supported"
    assert result.terms == []
    assert platform_port.calls == []


async def test_list_search_terms_rejects_an_account_from_another_business(
    mcp_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _owner_business_id, account_ref = await _seed_account(
        mcp_session_factory, platform_value="google"
    )
    platform_port = _FakeAdsPlatformPort()
    port = BrokerSearchTermReadPort(
        platform_port, mcp_session_factory, FixedClock(_NOW), query_template=_QUERY_TEMPLATE
    )

    with pytest.raises(EntityNotFoundError):
        await port.list_search_terms(str(uuid.uuid4()), account_ref, window=_WINDOW)

    assert platform_port.calls == []


async def test_list_search_terms_rejects_an_unknown_account(
    mcp_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    platform_port = _FakeAdsPlatformPort()
    port = BrokerSearchTermReadPort(
        platform_port, mcp_session_factory, FixedClock(_NOW), query_template=_QUERY_TEMPLATE
    )

    with pytest.raises(EntityNotFoundError):
        await port.list_search_terms(
            str(uuid.uuid4()), "google:does-not-exist", window=_WINDOW
        )

    assert platform_port.calls == []
