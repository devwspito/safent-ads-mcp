"""0027_us5_opportunities: `proposals.entity_ref` acepta el ambito de
cuenta (tasks.md T113/T114, US5), sin tocar `ad_entities`.

Una `Proposal` `CREATE_CAMPAIGN` necesita un `entity_ref` que ya "exista"
antes de que la campana exista de verdad en la plataforma
(`proposals_entity_fk`, `0008_proposals`) -- `mcp/presentation/catalog.py`
documentaba esto como el motivo de no exponer `propose_campaign` todavia.
La cuenta (`platform_accounts`) es el unico ambito valido para esa
`Proposal`.

**Decision explicita, corrige un intento anterior de este mismo lote**:
NO se anade `'account'` a `ad_entities.level`. `accounts.domain.ad_entity.
AdEntity.__post_init__` rechaza a proposito cualquier nivel fuera de
`{campaign, ad_set, ad, creative}` -- `ad_entities` modela SOLO la
jerarquia controlable; la cuenta ya tiene su propio agregado
(`PlatformAccount`). Insertar una fila `level='account'` en `ad_entities`
rompe a cualquier lector que mapee esas filas al agregado sin filtrar por
nivel (confirmado: `SignalCycle` recorre `ad_entities` de un negocio
completo y revienta con "AdEntity no admite nivel account").

En su lugar, `proposals_entity_fk` (una FK declarativa que solo puede
apuntar a UNA tabla) se sustituye por un trigger que acepta CUALQUIERA de
las dos fuentes validas: `ad_entities` (como antes) o `platform_accounts`
(cuenta activa, va desempaquetando `entity_ref = "<platform>:account:
<external_account_id>"`). Ninguna fila nueva se inserta en ninguna tabla
existente; `ad_entities` queda exactamente como estaba.

Revision ID: 0027_us5_opportunities
Revises: 0026_experiments
Create Date: 2026-09-10
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0027_us5_opportunities"
down_revision: str | None = "0026_experiments"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE proposals DROP CONSTRAINT proposals_entity_fk")
    op.execute("""
        CREATE FUNCTION proposals_entity_exists() RETURNS TRIGGER AS $$
        DECLARE
            platform_part TEXT;
            external_id_part TEXT;
        BEGIN
            IF EXISTS (
                SELECT 1 FROM ad_entities
                 WHERE business_id = NEW.business_id AND entity_ref = NEW.entity_ref
            ) THEN
                RETURN NEW;
            END IF;

            -- Ambito de cuenta (tasks.md T113/T114, opportunities):
            -- "<platform>:account:<external_account_id>", sin fila propia
            -- en ad_entities -- se valida contra platform_accounts.
            IF NEW.entity_ref ~ '^(google|meta):account:.+$' THEN
                platform_part := split_part(NEW.entity_ref, ':', 1);
                -- `substring(... FROM n)` es 1-indexado e inclusivo: el
                -- prefijo "<platform>:account:" ocupa length(platform_part)
                -- + length(':account:') = length(platform_part) + 9
                -- posiciones, asi que el external_id empieza en la +1.
                external_id_part := substring(NEW.entity_ref FROM length(platform_part) + 10);
                IF EXISTS (
                    SELECT 1 FROM platform_accounts
                     WHERE business_id = NEW.business_id
                       AND platform = platform_part
                       AND external_account_id = external_id_part
                ) THEN
                    RETURN NEW;
                END IF;
            END IF;

            RAISE EXCEPTION 'proposals.entity_ref % no existe en ad_entities ni platform_accounts',
                NEW.entity_ref;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER proposals_entity_exists_check
        BEFORE INSERT OR UPDATE OF entity_ref, business_id ON proposals
        FOR EACH ROW EXECUTE FUNCTION proposals_entity_exists()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER proposals_entity_exists_check ON proposals")
    op.execute("DROP FUNCTION proposals_entity_exists()")
    op.execute("""
        ALTER TABLE proposals
            ADD CONSTRAINT proposals_entity_fk
            FOREIGN KEY (business_id, entity_ref)
            REFERENCES ad_entities (business_id, entity_ref) ON DELETE RESTRICT
    """)
