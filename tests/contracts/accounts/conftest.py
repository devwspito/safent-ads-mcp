"""Banco de pruebas de contrato de `AccountRepository`/`AdEntityRepository`.

Los mismos casos corren dos veces: contra los dobles en memoria (rapido, sin
docker) y contra los repositorios SQL sobre Postgres real (marcados
`integration`). Si una implementacion se desvia de la otra, el caso falla en
una de las dos ejecuciones: eso es lo que significa "sustituible" (LSP).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.accounts.application.ports import AccountRepository, AdEntityRepository
from safent_ads.accounts.domain.ad_entity import AdEntity, AdEntityStatus
from safent_ads.accounts.domain.budget import Budget, BudgetKind
from safent_ads.accounts.domain.learning_state import LearningState
from safent_ads.accounts.domain.money import Money
from safent_ads.accounts.domain.platform_account import (
    ApiTier,
    PlatformAccount,
    PlatformAccountStatus,
)
from safent_ads.accounts.domain.platform_state_hash import PlatformStateHash
from safent_ads.accounts.domain.refs import AccountRef, CredentialRefId
from safent_ads.accounts.infrastructure.sql_repositories import (
    SqlAccountRepository,
    SqlAdEntityRepository,
)
from safent_ads.accounts.testing.in_memory_repositories import (
    InMemoryAccountRepository,
    InMemoryAdEntityRepository,
)
from safent_ads.shared.ids import BusinessId, EntityLevel, EntityRef, PlatformCode
from tests.conftest import rolled_back_session

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


class RepositoryFixture(Protocol):
    accounts: AccountRepository
    entities: AdEntityRepository

    async def given_business(self, business_id: BusinessId, platform: PlatformCode) -> None:
        """Prerrequisitos de integridad referencial: el negocio y el alias de
        credencial existen antes de guardar una cuenta. En memoria no hay
        nada que preparar."""


@dataclass(slots=True)
class InMemoryFixture:
    accounts: AccountRepository
    entities: AdEntityRepository

    async def given_business(
        self,
        business_id: BusinessId,  # noqa: ARG002 - forma exacta del contrato
        platform: PlatformCode,  # noqa: ARG002
    ) -> None:
        """En memoria no hay integridad referencial que preparar."""
        return None


@dataclass(slots=True)
class SqlFixture:
    accounts: AccountRepository
    entities: AdEntityRepository
    session: AsyncSession

    async def given_business(self, business_id: BusinessId, platform: PlatformCode) -> None:
        await self.session.execute(
            text(
                """
                INSERT INTO businesses (id, slug, name, timezone, reference_currency)
                VALUES (:id, :slug, 'Negocio de contrato', 'Europe/Madrid', 'EUR')
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {"id": business_id.value, "slug": f"neg-{business_id.value.hex[:12]}"},
        )
        await self.session.execute(
            text(
                """
                INSERT INTO credential_refs (id, platform, alias)
                VALUES (:id, :platform, :alias)
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {
                "id": CREDENTIAL_REF_ID.value,
                "platform": platform.value,
                "alias": f"alias-{CREDENTIAL_REF_ID.value.hex[:12]}",
            },
        )
        await self.session.flush()


CREDENTIAL_REF_ID = CredentialRefId(UUID("11111111-1111-4111-8111-111111111111"))


@pytest.fixture(
    params=[
        pytest.param("in_memory", id="in_memory"),
        pytest.param("sql", id="sql", marks=pytest.mark.integration),
    ]
)
async def repositories(request: pytest.FixtureRequest) -> AsyncIterator[RepositoryFixture]:
    if request.param == "in_memory":
        yield InMemoryFixture(
            accounts=InMemoryAccountRepository(), entities=InMemoryAdEntityRepository()
        )
        return
    # `database_url` es una fixture sincrona: pedirla aqui solo levanta el
    # contenedor cuando corre la variante SQL, nunca para la de memoria.
    database_url: str = request.getfixturevalue("database_url")
    async with rolled_back_session(database_url) as session:
        yield SqlFixture(
            accounts=SqlAccountRepository(session),
            entities=SqlAdEntityRepository(session),
            session=session,
        )


def new_business_id() -> BusinessId:
    return BusinessId(uuid4())


def account_ref(platform: PlatformCode = PlatformCode.META, suffix: str = "1") -> AccountRef:
    return AccountRef(platform=platform, external_account_id=f"act_{suffix}")


def build_account(
    business_id: BusinessId,
    *,
    ref: AccountRef | None = None,
    status: PlatformAccountStatus = PlatformAccountStatus.ACTIVE,
    last_synced_at: datetime | None = None,
) -> PlatformAccount:
    return PlatformAccount(
        business_id=business_id,
        account_ref=ref or account_ref(),
        currency="EUR",
        timezone="Europe/Madrid",
        api_tier=ApiTier.META_FULL,
        credential_ref_id=CREDENTIAL_REF_ID,
        status=status,
        last_synced_at=last_synced_at,
    )


def entity_ref(level: EntityLevel, external_id: str, platform: PlatformCode) -> EntityRef:
    return EntityRef(platform=platform, level=level, external_id=external_id)


def build_entity(
    business_id: BusinessId,
    *,
    level: EntityLevel = EntityLevel.CAMPAIGN,
    external_id: str = "c-1",
    parent: EntityRef,
    name: str = "Búsqueda Marca",
    status: AdEntityStatus = AdEntityStatus.ACTIVE,
    budget: Budget | None = None,
    bid_target: Money | None = None,
    learning_state: LearningState = LearningState.NOT_APPLICABLE,
    is_controllable: bool = True,
    state_hash: str = "a" * 64,
) -> AdEntity:
    return AdEntity(
        business_id=business_id,
        entity_ref=entity_ref(level, external_id, parent.platform),
        parent_ref=parent,
        name=name,
        status=status,
        platform_state_hash=PlatformStateHash(state_hash),
        is_controllable=is_controllable,
        learning_state=learning_state,
        budget=budget,
        bid_target=bid_target,
    )


def account_parent_ref(ref: AccountRef) -> EntityRef:
    return EntityRef(
        platform=ref.platform, level=EntityLevel.ACCOUNT, external_id=ref.external_account_id
    )


DAILY_BUDGET = Budget(amount=Money(7800, "EUR"), kind=BudgetKind.DAILY)
