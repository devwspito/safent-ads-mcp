"""`SqlWebhookTokenRepository` sobre `conversion_webhook_tokens`
(0029_economics_inputs, T220): un token activo por negocio, Postgres
real."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.crm.infrastructure.sql_webhook_token_repository import SqlWebhookTokenRepository
from safent_ads.shared.ids import BusinessId
from tests.conftest import BusinessFactory

pytestmark = pytest.mark.integration


async def test_upsert_then_find_by_hash_round_trips(
    db_session: AsyncSession, business_factory: BusinessFactory
) -> None:
    business_id = BusinessId(await business_factory.create())
    repo = SqlWebhookTokenRepository(db_session)

    await repo.upsert(business_id=business_id, token_hash="hash-1")

    assert await repo.find_business_id_by_token_hash("hash-1") == business_id
    assert await repo.find_business_id_by_token_hash("never-issued") is None


async def test_regenerating_replaces_the_previous_hash(
    db_session: AsyncSession, business_factory: BusinessFactory
) -> None:
    business_id = BusinessId(await business_factory.create())
    repo = SqlWebhookTokenRepository(db_session)

    await repo.upsert(business_id=business_id, token_hash="hash-1")
    await repo.upsert(business_id=business_id, token_hash="hash-2")

    assert await repo.find_business_id_by_token_hash("hash-1") is None
    assert await repo.find_business_id_by_token_hash("hash-2") == business_id
