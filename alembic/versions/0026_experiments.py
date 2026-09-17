"""0026_experiments: `signal_outcomes`, `calibration_adjustments` y
`experiments` (profitability-engine.md §4 y §6, tasks.md T199/T200/T202).

1. `signal_outcomes`: el contraste a 14 dias (o `median_lag_days + 14`) de
   cada `Signal` accionable -- `outcome_source IN (applied, expired,
   rejected)` y `was_correct` nulo si es inconcluyente (nunca se fuerza
   confirmado/contradicho sin dato). Una fila por senal (`UNIQUE
   (signal_id)`): idempotente, la vuelta del ciclo que ya la proceso no la
   vuelve a tocar. Indexada por `(business_id, account_id, rule_code)`
   -- la precision de §6 se agrega ahi -- y por `rule_code` solo, para el
   agregado global que consume `RecalibrateRules` (ver nota 2).

2. `calibration_adjustments`: bitacora de cada paso de calibracion. Las
   reglas del catalogo viven en `scope = 'global'` (una fila por `code`,
   0007_rules_guardrails): no hay hoy umbral calibrado por negocio o
   cuenta, asi que el ajuste que aplica `RecalibrateRules` es agregado
   sobre TODAS las salidas de ese `rule_code` -- Assumption documentada,
   escalada a database-engineer/tech-lead (un umbral calibrado por cuenta
   exigiria extender `rules` mas alla de este lote). `UNIQUE (rule_code,
   threshold_name, week_start)` impone en la base el limite de §6 ("maximo
   un ajuste por regla y semana"), no solo en el codigo de aplicacion.

3. `experiments`: el agregado de §4 -- diseno (arms, unidad de
   aleatorizacion, muestra por brazo), duracion, maquina de estados
   `draft -> running -> stopped_{success|futility|guardrail} -> concluded`
   y el resultado (JSONB: metodo, delta, IC, degradado o no). `proposal_id`
   enlaza a la propuesta que pide aprobacion del propietario para arrancar
   (T201 `propose_experiment`): nunca autonomo.

Revision ID: 0026_experiments
Revises: 0025_vocabulary
Create Date: 2026-09-10
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0026_experiments"
down_revision: str | None = "0025_vocabulary"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    _create_signal_outcomes()
    _create_calibration_adjustments()
    _create_experiments()


def _create_signal_outcomes() -> None:
    op.execute("""
        CREATE TABLE signal_outcomes (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            business_id     UUID NOT NULL REFERENCES businesses (id) ON DELETE RESTRICT,
            account_id      UUID NOT NULL REFERENCES platform_accounts (id) ON DELETE RESTRICT,
            rule_code       TEXT NOT NULL,
            signal_id       UUID NOT NULL UNIQUE REFERENCES signals (id) ON DELETE RESTRICT,
            outcome_source  TEXT NOT NULL
                              CHECK (outcome_source IN ('applied', 'expired', 'rejected')),
            was_correct     BOOLEAN,
            horizon_days    SMALLINT NOT NULL CHECK (horizon_days > 0),
            observed_at     TIMESTAMPTZ NOT NULL,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        COMMENT ON TABLE signal_outcomes IS
        'Contraste senal <-> resultado real (profitability-engine.md §6, FR-9,
         SC-4). Caducadas y rechazadas son el grupo de control natural.'
    """)
    op.execute("""
        CREATE INDEX ix_signal_outcomes_precision
        ON signal_outcomes (business_id, account_id, rule_code)
    """)
    op.execute("CREATE INDEX ix_signal_outcomes_rule_code ON signal_outcomes (rule_code)")


def _create_calibration_adjustments() -> None:
    op.execute("""
        CREATE TABLE calibration_adjustments (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            rule_code       TEXT NOT NULL,
            threshold_name  TEXT NOT NULL,
            direction       TEXT NOT NULL CHECK (direction IN
                              ('higher_is_more_conservative', 'lower_is_more_conservative')),
            previous_value  NUMERIC(10, 4) NOT NULL,
            new_value       NUMERIC(10, 4) NOT NULL,
            autonomy_level  TEXT NOT NULL CHECK (autonomy_level IN
                              ('notify', 'auto', 'approval')),
            sample_size     INTEGER NOT NULL CHECK (sample_size >= 0),
            precision_value NUMERIC(6, 4),
            week_start      DATE NOT NULL,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

            -- profitability-engine.md §6: "maximo un ajuste por regla y
            -- semana" -- invariante de base, no solo de aplicacion.
            CONSTRAINT calibration_adjustments_weekly_unique
                UNIQUE (rule_code, threshold_name, week_start)
        )
    """)
    op.execute("""
        COMMENT ON TABLE calibration_adjustments IS
        'Bitacora de calibracion (§6): un ajuste sobre una regla AUTO es
         siempre monotono conservador -- lo garantiza el dominio antes de
         llegar aqui (CalibrationDirectionViolationError).'
    """)
    op.execute(
        "CREATE INDEX ix_calibration_adjustments_rule ON calibration_adjustments (rule_code)"
    )


def _create_experiments() -> None:
    op.execute("""
        CREATE TABLE experiments (
            id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            business_id            UUID NOT NULL REFERENCES businesses (id) ON DELETE RESTRICT,
            entity_ref             TEXT NOT NULL,
            hypothesis             TEXT NOT NULL
                                     CHECK (char_length(hypothesis) BETWEEN 1 AND 500),
            metric                 TEXT NOT NULL,
            randomization_unit     TEXT NOT NULL
                                     CHECK (randomization_unit IN ('user', 'geo', 'campaign')),
            arms                   JSONB NOT NULL,
            baseline_rate          NUMERIC(6, 4) NOT NULL CHECK (baseline_rate BETWEEN 0 AND 1),
            relative_mde           NUMERIC(6, 4) NOT NULL CHECK (relative_mde > 0),
            sample_per_arm         INTEGER NOT NULL CHECK (sample_per_arm > 0),
            duration_days          SMALLINT NOT NULL CHECK (duration_days > 0),
            minimum_duration_days  SMALLINT NOT NULL CHECK (minimum_duration_days > 0),
            state                  TEXT NOT NULL DEFAULT 'draft' CHECK (state IN
                                     ('draft', 'running', 'stopped_success',
                                      'stopped_futility', 'stopped_guardrail', 'concluded')),
            stop_rule              TEXT,
            result                 JSONB,
            proposal_id            UUID REFERENCES proposals (id) ON DELETE SET NULL,
            started_at             TIMESTAMPTZ,
            ended_at               TIMESTAMPTZ,
            created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),

            CONSTRAINT experiments_entity_fk
                FOREIGN KEY (business_id, entity_ref)
                REFERENCES ad_entities (business_id, entity_ref) ON DELETE RESTRICT
        )
    """)
    op.execute("""
        COMMENT ON TABLE experiments IS
        'Experiment (profitability-engine.md §4): draft -> running ->
         stopped_{success|futility|guardrail} -> concluded. Nunca arranca
         sin proposal_id aprobada por el propietario.'
    """)
    op.execute("CREATE INDEX ix_experiments_business_state ON experiments (business_id, state)")
    op.execute("""
        CREATE TRIGGER experiments_set_updated_at
        BEFORE UPDATE ON experiments FOR EACH ROW EXECUTE FUNCTION set_updated_at()
    """)


def downgrade() -> None:
    op.execute("DROP TABLE experiments")
    op.execute("DROP TABLE calibration_adjustments")
    op.execute("DROP TABLE signal_outcomes")
