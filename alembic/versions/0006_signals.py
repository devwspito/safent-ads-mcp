"""0006_signals: `signals` (BUY/HOLD/SELL/EXIT + senales de creatividad) y
`anomalies` (solo notifican).

data-model.md §Signal: "toda senal lleva puerta superada, ventana, causa en
una frase y money_at_stake; si una puerta falla se emite kind = HOLD con
gate_reason y no es accionable; strength en [0,100]".

El indice parcial `WHERE kind <> 'HOLD'` sirve la consulta que manda el
ticker: senales accionables de un negocio ordenadas por dinero en juego
(contracts/telegram.md). Las HOLD son mayoria y no entran nunca en esa
consulta, asi que no pagan sitio en el indice.

Revision ID: 0006_signals
Revises: 0005_catalog_crm
Create Date: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0006_signals"
down_revision: str | None = "0005_catalog_crm"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE signals (
            id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            business_id              UUID NOT NULL REFERENCES businesses (id)
                                        ON DELETE RESTRICT,
            entity_ref               TEXT NOT NULL,
            kind                     TEXT NOT NULL CHECK (kind IN
                                        ('BUY', 'HOLD', 'SELL', 'EXIT',
                                         'FATIGUE', 'WINNER', 'LOSER')),
            strength                 SMALLINT NOT NULL
                                        CHECK (strength BETWEEN 0 AND 100),
            cause                    TEXT NOT NULL
                                        CHECK (char_length(cause) BETWEEN 1 AND 280),
            gate_reason              TEXT,
            gate_verdicts            JSONB NOT NULL DEFAULT '[]'::jsonb,
            evidence                 JSONB NOT NULL DEFAULT '{}'::jsonb,
            data_window              TEXT NOT NULL,
            window_start             DATE NOT NULL,
            window_end               DATE NOT NULL,
            money_at_stake_amount    NUMERIC(14, 2) NOT NULL
                                        CHECK (money_at_stake_amount >= 0),
            money_at_stake_currency  TEXT NOT NULL
                                        CHECK (char_length(money_at_stake_currency) = 3),
            outcome_at_14d           JSONB,
            outcome_evaluated_at     TIMESTAMPTZ,
            contradicted_at          TIMESTAMPTZ,
            cycle_id                 UUID NOT NULL,
            emitted_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
            created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at               TIMESTAMPTZ NOT NULL DEFAULT now(),

            CONSTRAINT signals_entity_fk
                FOREIGN KEY (business_id, entity_ref)
                REFERENCES ad_entities (business_id, entity_ref) ON DELETE RESTRICT,
            CONSTRAINT signals_window_check CHECK (window_end >= window_start),
            -- "si una puerta falla se emite kind = HOLD con gate_reason".
            CONSTRAINT signals_hold_needs_reason
                CHECK (kind <> 'HOLD' OR gate_reason IS NOT NULL),
            CONSTRAINT signals_outcome_pair_check
                CHECK (num_nulls(outcome_at_14d, outcome_evaluated_at) IN (0, 2)),
            -- Idempotencia del ciclo: reevaluar no duplica la senal.
            CONSTRAINT signals_cycle_entity_kind_unique
                UNIQUE (cycle_id, entity_ref, kind)
        )
    """)
    op.execute("""
        COMMENT ON COLUMN signals.outcome_at_14d IS
        'Contraste senal <-> resultado a 14 dias (FR-9, SC-4). Nulo hasta evaluarse.'
    """)
    op.execute("""
        CREATE TRIGGER signals_set_updated_at
        BEFORE UPDATE ON signals FOR EACH ROW EXECUTE FUNCTION set_updated_at()
    """)

    # GET /signals?business_id&since: feed del panel, orden cronologico.
    op.execute("""
        CREATE INDEX ix_signals_business_emitted ON signals (business_id, emitted_at DESC)
    """)
    # Detalle de entidad: ultimas senales de una campana concreta.
    op.execute("""
        CREATE INDEX ix_signals_entity_emitted ON signals (entity_ref, emitted_at DESC)
    """)
    # Ticker: accionables por dinero en juego. Excluye las HOLD, que son el
    # grueso de las filas y jamas se publican.
    op.execute("""
        CREATE INDEX ix_signals_actionable
        ON signals (business_id, money_at_stake_amount DESC)
        WHERE kind <> 'HOLD'
    """)
    # Cola de evaluacion a 14 dias: senales accionables aun sin desenlace.
    op.execute("""
        CREATE INDEX ix_signals_pending_outcome
        ON signals (emitted_at)
        WHERE outcome_at_14d IS NULL AND kind <> 'HOLD'
    """)

    op.execute("""
        CREATE TABLE anomalies (
            id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            business_id            UUID NOT NULL REFERENCES businesses (id)
                                      ON DELETE RESTRICT,
            entity_ref             TEXT NOT NULL,
            method                 TEXT NOT NULL CHECK (method IN
                                      ('weekday_z', 'ewma', 'wow', 'pace')),
            metric                 TEXT NOT NULL,
            score                  NUMERIC(10, 4) NOT NULL,
            consecutive_intervals  SMALLINT NOT NULL DEFAULT 1
                                      CHECK (consecutive_intervals >= 1),
            severity               TEXT NOT NULL CHECK (severity IN
                                      ('INFO', 'WARNING', 'CRITICAL')),
            interval_start         TIMESTAMPTZ NOT NULL,
            evidence               JSONB NOT NULL DEFAULT '{}'::jsonb,
            cycle_id               UUID NOT NULL,
            detected_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
            created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),

            CONSTRAINT anomalies_entity_fk
                FOREIGN KEY (business_id, entity_ref)
                REFERENCES ad_entities (business_id, entity_ref) ON DELETE RESTRICT,
            -- G10: una alerta por tipo e intervalo. Reejecutar el detector
            -- no vuelve a avisar.
            CONSTRAINT anomalies_natural_unique
                UNIQUE (entity_ref, method, metric, interval_start)
        )
    """)
    op.execute("""
        COMMENT ON TABLE anomalies IS
        'Solo notifica: nunca genera accion directa (data-model.md §Anomaly).'
    """)
    # GET /anomalies?business_id&since.
    op.execute("""
        CREATE INDEX ix_anomalies_business_detected ON anomalies (business_id, detected_at DESC)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE anomalies")
    op.execute("DROP TABLE signals")
