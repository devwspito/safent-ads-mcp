"""0012_domain_alignment: alinea `signals`, `anomalies`, `rules` y
`guardrails` con el dominio ya fusionado (`signals.domain`, `rules.domain`).

Igual que 0011, esto no es gusto: son valores que el dominio produce y el
esquema rechaza, o datos que produce y no tienen donde caer.

1. `signals`: `money_at_stake` es `MoneyAtStake.minor_units` (entero); en
   `NUMERIC(14,2)` habria que dividir por 100 y eso corrompe divisas sin
   decimales (JPY, KRW). Ademas `Signal` lleva `cause` (enum) y `rule_code`,
   que no tenian columna: la de texto guarda la frase renderizada.
2. `anomalies`: `AnomalySeverity` es `NONE|WARN|PAGE`; el CHECK pedia
   `INFO|WARNING|CRITICAL`, vocabulario que nadie emite.
3. `rules`: `ActionKind` tiene doce acciones y el CHECK solo admitia nueve
   con otros nombres: `TIGHTEN_TARGET`, `LOOSEN_TARGET` y `REPLACE_ASSET`
   (G03, G04, G08) no cabian. Se restablece tambien la regla de FR-11/FR-12
   con el vocabulario nuevo: AUTO nunca puede subir gasto, y subir gasto es
   exactamente `increases_spend()` del dominio (BUY, CREATIVE_SCALE,
   LOOSEN_TARGET). Se anaden `entity_level`, `description` y `source_url`,
   que el catalogo trae y no tenian columna.
4. `guardrails`: `GuardrailPolicy` es entera en unidades minimas, como el
   resto del dinero del esquema desde 0011.
5. `metrics_hourly` no tenia alcance, vistas de video ni cuota perdida, que
   `MetricFact` si lleva. El puerto no distingue diario de horario: guardar
   un hecho horario perdia esos contadores sin decirlo, y una perdida
   silenciosa es peor que un error.

Las conversiones de dinero reescriben columnas: la migracion se niega a
correr si la tabla afectada tiene filas.

Revision ID: 0012_domain_alignment
Revises: 0011_ad_entity_money
Create Date: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0012_domain_alignment"
down_revision: str | None = "0011_ad_entity_money"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_ACTIONS = (
    "'BUY', 'SELL', 'EXIT', 'UNPAUSE', 'HOLD_ALL', 'NOTIFY_ONLY', "
    "'ADD_NEGATIVE_KEYWORD', 'CREATIVE_KILL', 'CREATIVE_SCALE', "
    "'TIGHTEN_TARGET', 'LOOSEN_TARGET', 'REPLACE_ASSET'"
)
# `rules.domain.autonomy.increases_spend`, en SQL.
_SPEND_INCREASING = "'BUY', 'CREATIVE_SCALE', 'LOOSEN_TARGET'"


# Guarda literal por tabla (sin componer SQL): reescribir columnas de dinero
# con filas dentro seria una conversion a ciegas.
_REFUSE_IF_NOT_EMPTY = """
    DO $$
    BEGIN
        IF EXISTS (SELECT 1 FROM signals) THEN
            RAISE EXCEPTION '0012 reescribe columnas de signals y la tabla no esta vacia';
        END IF;
        IF EXISTS (SELECT 1 FROM guardrails) THEN
            RAISE EXCEPTION '0012 reescribe columnas de guardrails y la tabla no esta vacia';
        END IF;
    END
    $$
"""


def upgrade() -> None:
    op.execute(_REFUSE_IF_NOT_EMPTY)

    op.execute("""
        ALTER TABLE signals
            DROP COLUMN money_at_stake_amount,
            ADD COLUMN money_at_stake_minor BIGINT NOT NULL
                CHECK (money_at_stake_minor >= 0),
            ADD COLUMN cause_code TEXT NOT NULL,
            ADD COLUMN rule_code TEXT NOT NULL
    """)
    op.execute("""
        COMMENT ON COLUMN signals.cause IS
        'Frase renderizada para el propietario. El codigo de causa tipado va en cause_code.'
    """)
    # Soltar la columna se llevo por delante el indice parcial del ticker
    # (0006). Se rehace sobre la columna nueva: sin el, la consulta que
    # ordena las senales accionables por dinero en juego vuelve a barrer.
    op.execute("""
        CREATE INDEX ix_signals_actionable
        ON signals (business_id, money_at_stake_minor DESC)
        WHERE kind <> 'HOLD'
    """)

    op.execute("""
        ALTER TABLE metrics_hourly
            ADD COLUMN reach                 BIGINT CHECK (reach >= 0),
            ADD COLUMN video_views_3s        BIGINT CHECK (video_views_3s >= 0),
            ADD COLUMN video_views_75pct     BIGINT CHECK (video_views_75pct >= 0),
            ADD COLUMN search_lost_is_budget NUMERIC(6, 4)
                                             CHECK (search_lost_is_budget BETWEEN 0 AND 1),
            ADD COLUMN search_lost_is_rank   NUMERIC(6, 4)
                                             CHECK (search_lost_is_rank BETWEEN 0 AND 1)
    """)

    op.execute("""
        ALTER TABLE anomalies
            DROP CONSTRAINT IF EXISTS anomalies_severity_check,
            ADD CONSTRAINT anomalies_severity_check
                CHECK (severity IN ('NONE', 'WARN', 'PAGE'))
    """)

    # `account` entra solo en el ambito de las reglas (G10 vigila la cuenta
    # entera): los hechos de metricas siguen con grano por entidad.
    op.execute(f"""
        ALTER TABLE rules
            DROP CONSTRAINT IF EXISTS rules_action_check,
            DROP CONSTRAINT IF EXISTS rules_auto_is_defensive_check,
            ADD COLUMN entity_level TEXT
                CHECK (entity_level IN
                    ('account', 'campaign', 'ad_set', 'ad', 'creative')),
            ADD COLUMN description TEXT,
            ADD COLUMN source_url TEXT,
            ADD CONSTRAINT rules_action_check CHECK (action IN ({_ACTIONS})),
            ADD CONSTRAINT rules_auto_is_defensive_check CHECK (
                autonomy_level <> 'AUTO'
                OR action IS NULL
                OR action NOT IN ({_SPEND_INCREASING})
            )
    """)

    op.execute("""
        ALTER TABLE guardrails
            DROP CONSTRAINT IF EXISTS guardrails_floor_ceiling_check,
            DROP CONSTRAINT IF EXISTS guardrails_caps_check,
            DROP COLUMN daily_cap_amount,
            DROP COLUMN monthly_cap_amount,
            DROP COLUMN budget_floor_amount,
            DROP COLUMN budget_ceiling_amount,
            DROP COLUMN min_viable_spend_amount,
            ADD COLUMN daily_cap_minor        BIGINT CHECK (daily_cap_minor > 0),
            ADD COLUMN monthly_cap_minor      BIGINT CHECK (monthly_cap_minor > 0),
            ADD COLUMN budget_floor_minor     BIGINT CHECK (budget_floor_minor >= 0),
            ADD COLUMN budget_ceiling_minor   BIGINT CHECK (budget_ceiling_minor > 0),
            ADD COLUMN min_viable_spend_minor BIGINT CHECK (min_viable_spend_minor >= 0),
            ADD CONSTRAINT guardrails_floor_ceiling_check CHECK (
                budget_floor_minor IS NULL OR budget_ceiling_minor IS NULL
                OR budget_floor_minor <= budget_ceiling_minor
            ),
            ADD CONSTRAINT guardrails_caps_check CHECK (
                daily_cap_minor IS NULL OR monthly_cap_minor IS NULL
                OR daily_cap_minor <= monthly_cap_minor
            )
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE guardrails
            DROP CONSTRAINT IF EXISTS guardrails_caps_check,
            DROP CONSTRAINT IF EXISTS guardrails_floor_ceiling_check,
            DROP COLUMN min_viable_spend_minor,
            DROP COLUMN budget_ceiling_minor,
            DROP COLUMN budget_floor_minor,
            DROP COLUMN monthly_cap_minor,
            DROP COLUMN daily_cap_minor,
            ADD COLUMN daily_cap_amount        NUMERIC(14, 2) CHECK (daily_cap_amount > 0),
            ADD COLUMN monthly_cap_amount      NUMERIC(14, 2) CHECK (monthly_cap_amount > 0),
            ADD COLUMN budget_floor_amount     NUMERIC(14, 2) CHECK (budget_floor_amount >= 0),
            ADD COLUMN budget_ceiling_amount   NUMERIC(14, 2) CHECK (budget_ceiling_amount > 0),
            ADD COLUMN min_viable_spend_amount NUMERIC(14, 2)
                                                CHECK (min_viable_spend_amount >= 0),
            ADD CONSTRAINT guardrails_floor_ceiling_check CHECK (
                budget_floor_amount IS NULL OR budget_ceiling_amount IS NULL
                OR budget_floor_amount <= budget_ceiling_amount
            ),
            ADD CONSTRAINT guardrails_caps_check CHECK (
                daily_cap_amount IS NULL OR monthly_cap_amount IS NULL
                OR daily_cap_amount <= monthly_cap_amount
            )
    """)
    op.execute("""
        ALTER TABLE rules
            DROP CONSTRAINT IF EXISTS rules_auto_is_defensive_check,
            DROP CONSTRAINT IF EXISTS rules_action_check,
            DROP COLUMN source_url,
            DROP COLUMN description,
            DROP COLUMN entity_level,
            ADD CONSTRAINT rules_action_check CHECK (action IN
                ('BUY', 'SELL', 'EXIT', 'HOLD', 'UNPAUSE', 'ADD_NEGATIVE',
                 'CREATIVE_KILL', 'CREATIVE_SCALE', 'NOTIFY')),
            ADD CONSTRAINT rules_auto_is_defensive_check CHECK (
                autonomy_level <> 'AUTO'
                OR action IS NULL
                OR action IN ('SELL', 'EXIT', 'HOLD', 'UNPAUSE',
                              'ADD_NEGATIVE', 'CREATIVE_KILL', 'NOTIFY')
            )
    """)
    op.execute("""
        ALTER TABLE anomalies
            DROP CONSTRAINT IF EXISTS anomalies_severity_check,
            ADD CONSTRAINT anomalies_severity_check
                CHECK (severity IN ('INFO', 'WARNING', 'CRITICAL'))
    """)
    op.execute("""
        ALTER TABLE metrics_hourly
            DROP COLUMN search_lost_is_rank,
            DROP COLUMN search_lost_is_budget,
            DROP COLUMN video_views_75pct,
            DROP COLUMN video_views_3s,
            DROP COLUMN reach
    """)
    op.execute("""
        ALTER TABLE signals
            DROP COLUMN rule_code,
            DROP COLUMN cause_code,
            DROP COLUMN money_at_stake_minor,
            ADD COLUMN money_at_stake_amount NUMERIC(14, 2) NOT NULL DEFAULT 0
                CHECK (money_at_stake_amount >= 0)
    """)
    op.execute("""
        CREATE INDEX ix_signals_actionable
        ON signals (business_id, money_at_stake_amount DESC)
        WHERE kind <> 'HOLD'
    """)
