"""Factories de filas para los tests de migracion. El contenedor Postgres y
los ayudantes de Alembic viven en `tests/conftest.py`: uno por sesion para
toda la suite de integracion.

Las filas entran por SQL crudo a proposito: un test que congela el esquema en
una revision intermedia no puede escribir con los repositorios de la
aplicacion, que hablan siempre el esquema de cabecera (hoy `proposed_by`,
0043; manana la columna que toque)."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from testcontainers.community.postgres import PostgresContainer

from safent_ads.proposals.domain.money import Money
from safent_ads.proposals.domain.proposal import ProposedDiff
from safent_ads.proposals.infrastructure.value_codec import encode_value
from safent_ads.shared.ids import BusinessId, EntityRef
from safent_ads.shared.managed_ads import ManagedAdsBinding, binding_to_json
from tests.conftest import (
    alembic_downgrade as downgrade,
)
from tests.conftest import (
    alembic_upgrade as upgrade,
)
from tests.conftest import (
    to_alembic_dsn,
    to_asyncpg_dsn,
    with_database,
)

pytestmark = pytest.mark.integration

__all__ = ["downgrade", "to_alembic_dsn", "to_asyncpg_dsn", "upgrade", "with_database"]


@pytest.fixture
async def pg(postgres_container: PostgresContainer) -> AsyncIterator[asyncpg.Connection]:
    connection = await asyncpg.connect(to_asyncpg_dsn(postgres_container.get_connection_url()))
    try:
        yield connection
    finally:
        await connection.close()


def state_hash(seed: str) -> str:
    """`platform_state_hash` valido: la columna exige sha256 en hexadecimal."""
    return hashlib.sha256(seed.encode()).hexdigest()


async def make_business(connection: asyncpg.Connection) -> uuid.UUID:
    slug = f"neg-{uuid.uuid4().hex[:12]}"
    return await connection.fetchval(
        """
        INSERT INTO businesses (slug, name, timezone, reference_currency)
        VALUES ($1, 'Negocio de prueba', 'Europe/Madrid', 'EUR')
        RETURNING id
        """,
        slug,
    )


async def make_platform_account(
    connection: asyncpg.Connection,
    business_id: uuid.UUID,
    platform: str = "meta",
) -> uuid.UUID:
    return await connection.fetchval(
        """
        INSERT INTO platform_accounts (business_id, platform, external_account_id,
                                       currency, timezone, api_tier, status)
        VALUES ($1, $2, $3, 'EUR', 'Europe/Madrid', 'standard', 'ACTIVE')
        RETURNING id
        """,
        business_id,
        platform,
        f"act_{uuid.uuid4().hex[:12]}",
    )


async def make_entity(
    connection: asyncpg.Connection,
    business_id: uuid.UUID,
    platform_account_id: uuid.UUID,
    level: str = "campaign",
    parent_id: uuid.UUID | None = None,
    *,
    platform: str = "meta",
    status: str = "ACTIVE",
) -> asyncpg.Record:
    external_id = uuid.uuid4().hex[:16]
    return await connection.fetchrow(
        """
        INSERT INTO ad_entities (business_id, platform_account_id, platform, level,
                                 external_id, parent_id, name, status, platform_state_hash)
        VALUES ($1, $2, $3, $4, $5, $6, 'Entidad de prueba', $7, $8)
        RETURNING id, entity_ref, parent_level
        """,
        business_id,
        platform_account_id,
        platform,
        level,
        external_id,
        parent_id,
        status,
        state_hash(external_id),
    )


async def make_proposal(
    connection: asyncpg.Connection,
    business_id: uuid.UUID,
    entity_ref: str,
    *,
    parameter: str = "daily_budget",
    classification: str = "routine",
    proposed_value: str = '{"amount": 78, "currency": "EUR"}',
) -> asyncpg.Record:
    return await connection.fetchrow(
        """
        INSERT INTO proposals (business_id, entity_ref, parameter, current_value,
                               proposed_value, diff_hash, classification, cause_key, cause,
                               estimated_impact_amount, estimated_impact_currency, urgency,
                               expires_at)
        VALUES ($1, $2, $3, '{"amount": 60, "currency": "EUR"}'::jsonb, $4::jsonb, $5,
                $6, 'limitada-por-presupuesto', 'Limitada por presupuesto con CPL bajo objetivo',
                310, 'EUR', 'recommended', now() + interval '24 hours')
        RETURNING id, diff_hash, state
        """,
        business_id,
        entity_ref,
        parameter,
        proposed_value,
        state_hash(f"{entity_ref}|{parameter}|{proposed_value}"),
        classification,
    )


async def make_approval(
    connection: asyncpg.Connection,
    proposal_id: uuid.UUID,
    diff_hash: str,
    *,
    kind: str = "human_approval",
    decision: str = "approved",
) -> uuid.UUID:
    return await connection.fetchval(
        """
        INSERT INTO approvals (proposal_id, kind, decision, diff_hash, guardrail_verdict_hash,
                               issued_by, channel, signature, expires_at)
        VALUES ($1, $2, $3, $4, $5, 'owner', 'panel', 'firma-ed25519-de-prueba',
                now() + interval '1 hour')
        RETURNING id
        """,
        proposal_id,
        kind,
        decision,
        diff_hash,
        state_hash(f"guardrail|{diff_hash}"),
    )


async def make_offering(connection: asyncpg.Connection, business_id: uuid.UUID) -> uuid.UUID:
    return await connection.fetchval(
        """
        INSERT INTO offerings (business_id, code, title)
        VALUES ($1, $2, 'Oferta de prueba')
        RETURNING id
        """,
        business_id,
        f"of-{uuid.uuid4().hex[:12]}",
    )


_MANAGED_PROPOSAL_SQL = text("""
    INSERT INTO proposals (business_id, entity_ref, parameter, current_value, proposed_value,
                           diff_hash, classification, cause_key, cause,
                           estimated_impact_amount, estimated_impact_currency, urgency,
                           expires_at, managed_binding)
    VALUES (:business_id, :entity_ref, :parameter, CAST(:current_value AS JSONB),
            CAST(:proposed_value AS JSONB), :diff_hash, 'routine', 'senal-fisica',
            'Senal fisica', 20, 'EUR', 'recommended', now() + interval '24 hours',
            CAST(:managed_binding AS JSONB))
    RETURNING id
""")


async def insert_managed_proposal(
    session: AsyncSession,
    business_id: BusinessId,
    entity_ref: EntityRef,
    binding: ManagedAdsBinding | None,
    *,
    parameter: str = "daily_budget",
    after: str = "90",
) -> uuid.UUID:
    """Propuesta con contexto firmado, escrita sin pasar por el repositorio.

    Lo que miran los tests de 0039/0040 es el trigger `ads_guard_managed_context`
    y el guardian de su downgrade, no `SqlProposalRepository` -- que escribe
    columnas de cabecera y por eso no compila contra un esquema congelado. Las
    claims viajan por `binding_to_json` y el `diff_hash` por `ProposedDiff`:
    los mismos bytes que firmaria el dominio, incluida la validacion de
    pertenencia (`validate_entity`) antes de tocar la base.
    """
    diff = ProposedDiff.build(
        entity_ref, parameter, Money.of("70"), Money.of(after), managed_binding=binding
    )
    result = await session.execute(
        _MANAGED_PROPOSAL_SQL,
        {
            "business_id": str(business_id),
            "entity_ref": str(entity_ref),
            "parameter": parameter,
            "current_value": encode_value(diff.before),
            "proposed_value": encode_value(diff.after),
            "diff_hash": diff.diff_hash,
            "managed_binding": binding_to_json(binding),
        },
    )
    return result.scalar_one()
