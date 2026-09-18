import base64
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.integrations.store_api import service as module
from safent_ads.integrations.store_api.service import StoreApiError, StoreApiService
from tests.conftest import BusinessFactory, OwnerFactory

pytestmark = pytest.mark.integration


async def test_connection_persists_encrypted_and_is_scoped(
    db_session: AsyncSession,
    business_factory: BusinessFactory,
    owner_factory: OwnerFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business = str(await business_factory.create())
    other = str(await business_factory.create())
    owner = await owner_factory.create()

    @asynccontextmanager
    async def sessions():
        yield db_session

    service = StoreApiService(
        sessions, base64.b64encode(b"0" * 32).decode(), "https://catalog.example/api"
    )
    probe = AsyncMock(return_value={"data": [], "pagination": {}})
    monkeypatch.setattr(module, "fetch_page", probe)
    assert (await service.status(business))["configured"] is False
    status = await service.connect(business, "test-private-value", owner)
    assert status["configured"] is True
    assert "test-private-value" not in str(status)
    row = (
        await db_session.execute(
            text("SELECT token_encrypted FROM store_api_connections WHERE business_id=:id"),
            {"id": business},
        )
    ).first()
    assert b"test-private-value" not in row[0]
    assert (await service.status(other))["configured"] is False
    with pytest.raises(StoreApiError, match="Conecta"):
        await service.read(other, "stock", 1, 20)
    await service.read(business, "stock", 2, 20)
    assert probe.call_args.args == (
        "https://catalog.example/api",
        "test-private-value",
        "stock",
        2,
        20,
    )
    count = await db_session.scalar(
        text(
            "SELECT count(*) FROM decision_log WHERE event_type='store_api_connected' "
            "AND business_id=:id"
        ),
        {"id": business},
    )
    assert count == 1
    service.base = "https://another.example/api"
    assert (await service.status(business))["configured"] is False
    with pytest.raises(StoreApiError, match="Conecta"):
        await service.read(business, "catalog", 1, 20)
