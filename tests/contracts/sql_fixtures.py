"""Prerrequisitos de integridad referencial para los bancos de contrato que
tocan Postgres: metricas, senales y disparos cuelgan de una entidad, que
cuelga de una cuenta, que cuelga de un negocio.

En memoria no hay nada que preparar; en SQL, saltarse esto es chocar con las
FK compuestas que impiden que un dato apunte al negocio de otro (C-27)."""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.shared.ids import EntityLevel, EntityRef

_STATE_HASH = "a" * 64


async def seed_entity(session: AsyncSession, entity_ref: EntityRef) -> uuid.UUID:
    """Crea negocio, credencial, cuenta y la entidad de nivel campana que
    nombra `entity_ref`. Devuelve el `business_id`."""
    business_id = uuid.uuid4()
    credential_id = uuid.uuid4()
    account_id = uuid.uuid4()
    suffix = business_id.hex[:12]
    await session.execute(
        text(
            """
            INSERT INTO businesses (id, slug, name, timezone, reference_currency)
            VALUES (:id, :slug, 'Negocio de contrato', 'Europe/Madrid', 'EUR')
            """
        ),
        {"id": business_id, "slug": f"neg-{suffix}"},
    )
    await session.execute(
        text(
            """
            INSERT INTO credential_refs (id, platform, alias)
            VALUES (:id, :platform, :alias)
            """
        ),
        {"id": credential_id, "platform": entity_ref.platform.value, "alias": f"alias-{suffix}"},
    )
    await session.execute(
        text(
            """
            INSERT INTO platform_accounts (id, business_id, platform, external_account_id,
                                           currency, timezone, api_tier, credential_ref_id,
                                           status)
            VALUES (:id, :business_id, :platform, :external_account_id, 'EUR',
                    'Europe/Madrid', 'meta_full', :credential_ref_id, 'ACTIVE')
            """
        ),
        {
            "id": account_id,
            "business_id": business_id,
            "platform": entity_ref.platform.value,
            "external_account_id": account_external_id(entity_ref),
            "credential_ref_id": credential_id,
        },
    )
    await session.execute(
        text(
            """
            INSERT INTO ad_entities (business_id, platform_account_id, platform, level,
                                     external_id, name, status, platform_state_hash)
            VALUES (:business_id, :account_id, :platform, :level, :external_id,
                    'Campana de contrato', 'ACTIVE', :state_hash)
            """
        ),
        {
            "business_id": business_id,
            "account_id": account_id,
            "platform": entity_ref.platform.value,
            "level": entity_ref.level.value,
            "external_id": entity_ref.external_id,
            "state_hash": _STATE_HASH,
        },
    )
    await session.flush()
    return business_id


def account_external_id(entity_ref: EntityRef) -> str:
    """Cada entidad de prueba estrena cuenta: la cuenta se deriva de su
    identificador externo para que dos casos nunca compartan una."""
    return f"act_{entity_ref.external_id}"


def campaign_ref(external_id: str, platform_value: str = "meta") -> EntityRef:
    return EntityRef.parse(f"{platform_value}:{EntityLevel.CAMPAIGN.value}:{external_id}")
