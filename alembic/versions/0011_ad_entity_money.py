"""0011_ad_entity_money: alinea `ad_entities` con el dominio ya fusionado
(`accounts.domain`) en los tres puntos donde el esquema no puede
representarlo.

1. `learning_state` admitia el vocabulario de plataforma
   (`SUCCESS|FAIL|UNKNOWN`); el dominio (`LearningState`) solo produce
   `NOT_APPLICABLE|LEARNING|LEARNED`. `LEARNED` no cabia: bloqueante.
2. `bid_target` es un `Money` (importe + divisa) y no habia columna de
   divisa: guardar solo el importe pierde informacion y no hay
   `currency` en la tabla del que deducirla (`budget_currency` es NULL
   cuando la entidad no tiene presupuesto propio). Bloqueante.
3. `Money` son unidades minimas enteras. `NUMERIC(14,2)` obliga a dividir
   por 100 al escribir, lo que corrompe divisas sin decimales (JPY, KRW,
   CLP: 1.000 JPY se guardarian como 10,00 y volverian como 1.000
   *centimos*). Se guarda el entero tal cual, como en `spend_ledger`.

La conversion de (3) elimina dos columnas. La migracion se niega a correr
si `ad_entities` tiene una sola fila: en ese caso hay datos que decidir y
la decision no es de una migracion automatica.

Revision ID: 0011_ad_entity_money
Revises: 0010_notifications
Create Date: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0011_ad_entity_money"
down_revision: str | None = "0010_notifications"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM ad_entities) THEN
                RAISE EXCEPTION
                    '0011 reescribe columnas de dinero de ad_entities y la tabla no esta '
                    'vacia: migrar los importes a unidades minimas a mano antes de seguir';
            END IF;
        END
        $$
    """)

    op.execute("""
        ALTER TABLE ad_entities
            DROP CONSTRAINT IF EXISTS ad_entities_learning_state_check,
            ALTER COLUMN learning_state SET DEFAULT 'NOT_APPLICABLE',
            ADD CONSTRAINT ad_entities_learning_state_check
                CHECK (learning_state IN ('NOT_APPLICABLE', 'LEARNING', 'LEARNED'))
    """)

    op.execute("""
        ALTER TABLE ad_entities
            DROP CONSTRAINT IF EXISTS ad_entities_budget_triplet_check,
            DROP COLUMN budget_amount,
            DROP COLUMN bid_target_amount,
            ADD COLUMN budget_amount_minor     BIGINT CHECK (budget_amount_minor >= 0),
            ADD COLUMN bid_target_amount_minor BIGINT CHECK (bid_target_amount_minor >= 0),
            ADD COLUMN bid_target_currency     TEXT
                                                CHECK (char_length(bid_target_currency) = 3),
            ADD CONSTRAINT ad_entities_budget_triplet_check
                CHECK (num_nulls(budget_amount_minor, budget_currency, budget_kind) IN (0, 3)),
            ADD CONSTRAINT ad_entities_bid_target_pair_check
                CHECK (num_nulls(bid_target_amount_minor, bid_target_currency) IN (0, 2))
    """)
    op.execute("""
        COMMENT ON COLUMN ad_entities.budget_amount_minor IS
        'Unidades minimas de budget_currency (centimos en EUR), como Money.minor_units.'
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE ad_entities
            DROP CONSTRAINT IF EXISTS ad_entities_bid_target_pair_check,
            DROP CONSTRAINT IF EXISTS ad_entities_budget_triplet_check,
            DROP COLUMN bid_target_currency,
            DROP COLUMN bid_target_amount_minor,
            DROP COLUMN budget_amount_minor,
            ADD COLUMN budget_amount     NUMERIC(14, 2) CHECK (budget_amount >= 0),
            ADD COLUMN bid_target_amount NUMERIC(14, 4) CHECK (bid_target_amount >= 0),
            ADD CONSTRAINT ad_entities_budget_triplet_check
                CHECK (num_nulls(budget_amount, budget_currency, budget_kind) IN (0, 3))
    """)
    op.execute("""
        ALTER TABLE ad_entities
            DROP CONSTRAINT IF EXISTS ad_entities_learning_state_check,
            ALTER COLUMN learning_state SET DEFAULT 'UNKNOWN',
            ADD CONSTRAINT ad_entities_learning_state_check
                CHECK (learning_state IN
                    ('LEARNING', 'SUCCESS', 'FAIL', 'NOT_APPLICABLE', 'UNKNOWN'))
    """)
