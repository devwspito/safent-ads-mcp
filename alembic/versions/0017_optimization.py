"""0017_optimization: `marginal_estimates` (materializado, se sobrescribe),
`response_curves` (materializado, se sobrescribe), `allocation_plans`
(historico, solo-anexable) y la columna `expected_contribution_delta` en
`proposals` (profitability-engine.md §8: "PropuestaDeAccion gana
expected_contribution_delta: Money con su intervalo").

`marginal_estimates` guarda la ultima `MarginalEstimate` por
`(business_id, entity_ref)` -- el bootstrap de ventanas emparejadas o el
ajuste PyMC-Marketing son caros, los recalcula un `MaintenanceCycle` fuera
de alcance de esta migracion (UPSERT, igual que `lag_curve_snapshots` en
`0016_economics`).

`response_curves` es por `channel_id`, no por `entity_ref`: el puente
campana-dia<->canal (§3b) puede agrupar varias campanas bajo
`residual_<platform>`, que no es una fila de `ad_entities` -- sin FK a
`ad_entities` a proposito.

`allocation_plans` es historico (una fila por `AllocationPlan` propuesto,
solo-anexable): auditoria de que par donante/receptor se comparo y que
propuestas genero, no se sobrescribe ni se borra.

`experiments`/`signal_outcomes`/`calibration_adjustments` (§4, §6) tienen
dominio completo y probado (`optimization/domain/experiment.py`,
`calibration.py`) pero ningun repositorio ni caso de uso las persiste
todavia (P2 en profitability-engine.md §8) -- crear su esquema ahora seria
tabla sin lector. Se difieren a la migracion que traiga su capa de
aplicacion.

Aditiva sobre `0016_economics` (head actual): no toca `0001`-`0016`.

Revision ID: 0017_optimization
Revises: 0016_economics
Create Date: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0017_optimization"
down_revision: str | None = "0016_economics"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    _create_marginal_estimates()
    _create_response_curves()
    _create_allocation_plans()
    _add_expected_contribution_delta_to_proposals()


def _create_marginal_estimates() -> None:
    op.execute("""
        CREATE TABLE marginal_estimates (
            id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            business_id  UUID NOT NULL REFERENCES businesses (id) ON DELETE RESTRICT,
            entity_ref   TEXT NOT NULL,
            value        NUMERIC(10, 6) NOT NULL,
            ci_low       NUMERIC(10, 6) NOT NULL,
            ci_high      NUMERIC(10, 6) NOT NULL CHECK (ci_high >= ci_low),
            method       TEXT NOT NULL CHECK (method IN ('paired', 'mmm', 'shrunk')),
            sample_size  INTEGER NOT NULL CHECK (sample_size >= 0),
            computed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

            CONSTRAINT marginal_estimates_unique UNIQUE (business_id, entity_ref),
            CONSTRAINT marginal_estimates_entity_fk
                FOREIGN KEY (business_id, entity_ref)
                REFERENCES ad_entities (business_id, entity_ref) ON DELETE RESTRICT
        )
    """)
    op.execute("""
        COMMENT ON TABLE marginal_estimates IS
        'Materializada, no solo-anexable: el MaintenanceCycle la recalcula
        (UPSERT) porque el bootstrap de ventanas emparejadas o el ajuste
        PyMC-Marketing son caros de repetir en cada lectura.'
    """)


def _create_response_curves() -> None:
    op.execute("""
        CREATE TABLE response_curves (
            id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            business_id       UUID NOT NULL REFERENCES businesses (id) ON DELETE RESTRICT,
            channel_id        TEXT NOT NULL CHECK (char_length(channel_id) BETWEEN 1 AND 200),
            curve_type        TEXT NOT NULL CHECK (curve_type IN ('hill', 'power')),
            params            JSONB NOT NULL,
            observed_min_spend NUMERIC(14, 2) NOT NULL CHECK (observed_min_spend >= 0),
            observed_max_spend NUMERIC(14, 2) NOT NULL
                                 CHECK (observed_max_spend >= observed_min_spend),
            residual_std      NUMERIC(14, 4) NOT NULL CHECK (residual_std >= 0),
            curve_confidence  TEXT NOT NULL
                                 CHECK (curve_confidence IN ('experimental', 'observational')),
            computed_at       TIMESTAMPTZ NOT NULL DEFAULT now(),

            CONSTRAINT response_curves_unique UNIQUE (business_id, channel_id)
        )
    """)
    op.execute("""
        COMMENT ON TABLE response_curves IS
        'Por channel_id (puente campana<->canal, §3b), no por entity_ref: un
        canal residual agrupa varias campanas y no es una fila de
        ad_entities. Materializada, UPSERT por el MaintenanceCycle.'
    """)
    op.execute("""
        COMMENT ON COLUMN response_curves.params IS
        'Hill: {"e_max": ..., "k": ...}. Potencia: {"a": ..., "b": ...}.'
    """)


def _create_allocation_plans() -> None:
    op.execute("""
        CREATE TABLE allocation_plans (
            id                             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            business_id                    UUID NOT NULL REFERENCES businesses (id)
                                             ON DELETE RESTRICT,
            donor_entity_ref               TEXT NOT NULL,
            donor_current_daily_spend_minor    INTEGER NOT NULL
                                             CHECK (donor_current_daily_spend_minor >= 0),
            donor_proposed_daily_spend_minor   INTEGER NOT NULL
                                             CHECK (donor_proposed_daily_spend_minor >= 0),
            donor_step_pct                 NUMERIC(6, 4) NOT NULL,
            receiver_entity_ref            TEXT NOT NULL,
            receiver_current_daily_spend_minor  INTEGER NOT NULL
                                             CHECK (receiver_current_daily_spend_minor >= 0),
            receiver_proposed_daily_spend_minor INTEGER NOT NULL
                                             CHECK (receiver_proposed_daily_spend_minor >= 0),
            receiver_step_pct              NUMERIC(6, 4) NOT NULL,
            expected_contribution_delta_amount   NUMERIC(14, 2) NOT NULL,
            expected_contribution_delta_currency TEXT NOT NULL
                                             CHECK (
                                               char_length(expected_contribution_delta_currency) = 3
                                             ),
            decrease_proposal_id           UUID REFERENCES proposals (id) ON DELETE SET NULL,
            increase_proposal_id           UUID REFERENCES proposals (id) ON DELETE SET NULL,
            created_at                     TIMESTAMPTZ NOT NULL DEFAULT now(),

            CONSTRAINT allocation_plans_donor_fk
                FOREIGN KEY (business_id, donor_entity_ref)
                REFERENCES ad_entities (business_id, entity_ref) ON DELETE RESTRICT,
            CONSTRAINT allocation_plans_receiver_fk
                FOREIGN KEY (business_id, receiver_entity_ref)
                REFERENCES ad_entities (business_id, entity_ref) ON DELETE RESTRICT
        )
    """)
    op.execute("""
        COMMENT ON TABLE allocation_plans IS
        'Historico solo-anexable: una fila por AllocationPlan propuesto
        (auditoria del par donante/receptor comparado y de las dos
        propuestas ligadas que genero, §3).'
    """)
    op.execute("""
        CREATE INDEX ix_allocation_plans_business_created
        ON allocation_plans (business_id, created_at DESC)
    """)


def _add_expected_contribution_delta_to_proposals() -> None:
    op.execute("""
        ALTER TABLE proposals
        ADD COLUMN expected_contribution_delta_amount NUMERIC(14, 2),
        ADD COLUMN expected_contribution_delta_currency TEXT
            CHECK (expected_contribution_delta_currency IS NULL
                   OR char_length(expected_contribution_delta_currency) = 3)
    """)
    op.execute("""
        COMMENT ON COLUMN proposals.expected_contribution_delta_amount IS
        'profitability-engine.md §8: la cola ordena por este valor DESC, no
        por urgency/money_at_stake. NULL en propuestas que no vienen de
        optimization (reglas M01-M24): la migracion es aditiva, no todas las
        propuestas tienen contribucion estimada todavia.'
    """)
    op.execute("""
        CREATE INDEX ix_proposals_contribution_delta
        ON proposals (business_id, expected_contribution_delta_amount DESC NULLS LAST)
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_proposals_contribution_delta")
    op.execute("""
        ALTER TABLE proposals
        DROP COLUMN IF EXISTS expected_contribution_delta_amount,
        DROP COLUMN IF EXISTS expected_contribution_delta_currency
    """)
    op.execute("DROP TABLE IF EXISTS allocation_plans")
    op.execute("DROP TABLE IF EXISTS response_curves")
    op.execute("DROP TABLE IF EXISTS marginal_estimates")
