"""`BrokerGaqlReadPort` (lane `gaql`): la comprobacion IDOR contra
`platform_accounts` es SQL-only (necesita filas reales), asi que este
banco no compara contra un doble en memoria como el resto de
`tests/integration/mcp/test_*_contract.py` -- solo prueba el puerto real
con un `AdsPlatformPort` falso inyectado (nunca toca un socket de verdad,
mismo principio que `mcp/testing/fakes.py` documenta para IDOR)."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.contracts.sql_fixtures import account_external_id, campaign_ref, seed_entity

from safent_ads.accounts.domain.refs import AccountRef
from safent_ads.mcp.application.errors import EntityNotFoundError
from safent_ads.mcp.infrastructure.broker_gaql_read_port import BrokerGaqlReadPort

pytestmark = pytest.mark.integration


class _FakeAdsPlatformPort:
    def __init__(self) -> None:
        self.calls: list[tuple[AccountRef, str, int]] = []

    async def run_gaql(
        self, account_ref: AccountRef, query: str, *, max_rows: int
    ) -> Sequence[Mapping[str, str]]:
        self.calls.append((account_ref, query, max_rows))
        return [{"campaign.id": "111"}]


async def _seed_account(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[str, str]:
    entity_ref = campaign_ref(f"gaql{uuid.uuid4().hex[:8]}", platform_value="google")
    async with factory() as session:
        business_id = await seed_entity(session, entity_ref)
        await session.commit()
    account_ref = f"google:{account_external_id(entity_ref)}"
    return str(business_id), account_ref


async def test_run_gaql_delegates_to_the_platform_port_for_the_owning_business(
    mcp_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    business_id, account_ref = await _seed_account(mcp_session_factory)
    platform_port = _FakeAdsPlatformPort()
    port = BrokerGaqlReadPort(platform_port, mcp_session_factory)

    result = await port.run_gaql(business_id, account_ref, "SELECT campaign.id FROM campaign")

    assert result.account_ref == account_ref
    assert result.rows == [{"campaign.id": "111"}]
    assert result.row_count == 1
    assert len(platform_port.calls) == 1
    assert str(platform_port.calls[0][0]) == account_ref


async def test_run_gaql_rejects_an_account_from_another_business(
    mcp_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _owner_business_id, account_ref = await _seed_account(mcp_session_factory)
    platform_port = _FakeAdsPlatformPort()
    port = BrokerGaqlReadPort(platform_port, mcp_session_factory)

    with pytest.raises(EntityNotFoundError):
        await port.run_gaql(str(uuid.uuid4()), account_ref, "SELECT campaign.id FROM campaign")

    assert platform_port.calls == []


async def test_run_gaql_rejects_an_unknown_account(
    mcp_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    platform_port = _FakeAdsPlatformPort()
    port = BrokerGaqlReadPort(platform_port, mcp_session_factory)

    with pytest.raises(EntityNotFoundError):
        await port.run_gaql(
            str(uuid.uuid4()), "google:does-not-exist", "SELECT campaign.id FROM campaign"
        )

    assert platform_port.calls == []
