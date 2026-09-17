"""0007_rules_guardrails: `rules`, `guardrails`, `emergency_brakes`,
`rule_firings` y la semilla idempotente que hace nacer el catalogo en
NOTIFY con `autonomy_enabled = false`.

data-model.md §Rule: "una regla AUTO solo produce acciones defensivas
(bajar, pausar, negativas, rotar fuera, reanudar por conversion tardia) --
el dominio rechaza una AUTO que aumente gasto (FR-11/FR-12)". Eso se
comprueba tambien aqui: un CHECK, no solo el dominio.

tasks.md §Notas 4: "El catalogo nace NOTIFY con autonomy_enabled=false
(D-A1); pasar a AUTO es decision del propietario, no un despliegue". La
semilla siembra los 37 codigos deshabilitados y en NOTIFY; el cargador de
`rules/catalog/rules.yaml` (T038) los completa. `ON CONFLICT DO NOTHING`:
reejecutarla nunca pisa una calibracion del propietario.

Revision ID: 0007_rules_guardrails
Revises: 0006_signals
Create Date: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0007_rules_guardrails"
down_revision: str | None = "0006_signals"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    # La autonomia es una propiedad de la cuenta, no del guardarrail: apagarla
    # deja fuera de juego a todas sus reglas AUTO de una vez.
    op.execute("""
        ALTER TABLE platform_accounts
            ADD COLUMN autonomy_enabled BOOLEAN NOT NULL DEFAULT false
    """)
    op.execute("""
        COMMENT ON COLUMN platform_accounts.autonomy_enabled IS
        'Puerta de autonomia (tasks.md T120). Nace en false; solo el propietario la abre.'
    """)

    op.execute("""
        CREATE TABLE rules (
            id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            code                 TEXT NOT NULL CHECK (code ~ '^[MGX][0-9]{2}$'),
            scope                TEXT NOT NULL CHECK (scope IN
                                    ('global', 'business', 'platform_account', 'campaign')),
            business_id          UUID REFERENCES businesses (id) ON DELETE CASCADE,
            platform_account_id  UUID REFERENCES platform_accounts (id) ON DELETE CASCADE,
            campaign_entity_ref  TEXT REFERENCES ad_entities (entity_ref) ON DELETE CASCADE,
            scope_key            TEXT GENERATED ALWAYS AS (
                                    COALESCE(business_id::text, '*') || '|' ||
                                    COALESCE(platform_account_id::text, '*') || '|' ||
                                    COALESCE(campaign_entity_ref, '*')) STORED,
            platform             TEXT CHECK (platform IN ('google', 'meta')),
            condition            JSONB NOT NULL DEFAULT '{}'::jsonb,
            thresholds           JSONB NOT NULL DEFAULT '{}'::jsonb,
            data_window          TEXT,
            action               TEXT CHECK (action IN
                                    ('BUY', 'SELL', 'EXIT', 'HOLD', 'UNPAUSE',
                                     'ADD_NEGATIVE', 'CREATIVE_KILL', 'CREATIVE_SCALE',
                                     'NOTIFY')),
            magnitude_pct        NUMERIC(5, 2)
                                    CHECK (magnitude_pct > 0 AND magnitude_pct <= 100),
            autonomy_level       TEXT NOT NULL DEFAULT 'NOTIFY' CHECK (autonomy_level IN
                                    ('OFF', 'NOTIFY', 'AUTO', 'APPROVAL')),
            cooldown_minutes     INTEGER CHECK (cooldown_minutes >= 0),
            max_firings_per_day  SMALLINT CHECK (max_firings_per_day > 0),
            is_enabled           BOOLEAN NOT NULL DEFAULT false,
            calibrated_at        TIMESTAMPTZ,
            created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),

            CONSTRAINT rules_code_scope_unique UNIQUE (code, scope_key),
            CONSTRAINT rules_scope_coherence_check CHECK (
                (scope = 'global'
                    AND business_id IS NULL AND platform_account_id IS NULL
                    AND campaign_entity_ref IS NULL)
                OR (scope = 'business'
                    AND business_id IS NOT NULL AND platform_account_id IS NULL
                    AND campaign_entity_ref IS NULL)
                OR (scope = 'platform_account'
                    AND platform_account_id IS NOT NULL AND campaign_entity_ref IS NULL)
                OR (scope = 'campaign' AND campaign_entity_ref IS NOT NULL)
            ),
            -- Una regla habilitada esta completa o no se habilita.
            CONSTRAINT rules_enabled_is_complete_check CHECK (
                is_enabled = false
                OR (action IS NOT NULL AND data_window IS NOT NULL
                    AND cooldown_minutes IS NOT NULL AND max_firings_per_day IS NOT NULL)
            ),
            -- FR-11/FR-12: lo autonomo solo defiende. Subir gasto, tocar
            -- objetivos o publicar exige aprobacion humana.
            CONSTRAINT rules_auto_is_defensive_check CHECK (
                autonomy_level <> 'AUTO'
                OR action IS NULL
                OR action IN ('SELL', 'EXIT', 'HOLD', 'UNPAUSE',
                              'ADD_NEGATIVE', 'CREATIVE_KILL', 'NOTIFY')
            )
        )
    """)
    op.execute("""
        CREATE TRIGGER rules_set_updated_at
        BEFORE UPDATE ON rules FOR EACH ROW EXECUTE FUNCTION set_updated_at()
    """)
    # RuleMatcher: reglas vivas de una plataforma para el ciclo.
    op.execute("""
        CREATE INDEX ix_rules_enabled_platform ON rules (platform, scope)
        WHERE is_enabled
    """)
    op.execute("CREATE INDEX ix_rules_business ON rules (business_id)")
    op.execute("CREATE INDEX ix_rules_platform_account ON rules (platform_account_id)")
    op.execute("CREATE INDEX ix_rules_campaign ON rules (campaign_entity_ref)")

    op.execute("""
        CREATE TABLE guardrails (
            id                             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            scope                          TEXT NOT NULL CHECK (scope IN
                                              ('business', 'platform_account', 'campaign')),
            business_id                    UUID REFERENCES businesses (id) ON DELETE CASCADE,
            platform_account_id            UUID REFERENCES platform_accounts (id)
                                              ON DELETE CASCADE,
            campaign_entity_ref            TEXT REFERENCES ad_entities (entity_ref)
                                              ON DELETE CASCADE,
            scope_key                      TEXT GENERATED ALWAYS AS (
                                              COALESCE(business_id::text, '*') || '|' ||
                                              COALESCE(platform_account_id::text, '*') || '|' ||
                                              COALESCE(campaign_entity_ref, '*')) STORED,
            currency                       TEXT NOT NULL CHECK (char_length(currency) = 3),
            daily_cap_amount               NUMERIC(14, 2) CHECK (daily_cap_amount > 0),
            monthly_cap_amount             NUMERIC(14, 2) CHECK (monthly_cap_amount > 0),
            budget_floor_amount            NUMERIC(14, 2) CHECK (budget_floor_amount >= 0),
            budget_ceiling_amount          NUMERIC(14, 2) CHECK (budget_ceiling_amount > 0),
            max_step_pct                   NUMERIC(5, 2) NOT NULL
                                              CHECK (max_step_pct > 0 AND max_step_pct <= 100),
            max_changes_per_entity_per_day SMALLINT NOT NULL
                                              CHECK (max_changes_per_entity_per_day > 0),
            min_viable_spend_amount        NUMERIC(14, 2)
                                              CHECK (min_viable_spend_amount >= 0),
            created_at                     TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at                     TIMESTAMPTZ NOT NULL DEFAULT now(),

            CONSTRAINT guardrails_scope_unique UNIQUE (scope, scope_key),
            CONSTRAINT guardrails_scope_coherence_check CHECK (
                (scope = 'business'
                    AND business_id IS NOT NULL AND platform_account_id IS NULL
                    AND campaign_entity_ref IS NULL)
                OR (scope = 'platform_account'
                    AND platform_account_id IS NOT NULL AND campaign_entity_ref IS NULL)
                OR (scope = 'campaign' AND campaign_entity_ref IS NOT NULL)
            ),
            CONSTRAINT guardrails_floor_ceiling_check CHECK (
                budget_floor_amount IS NULL OR budget_ceiling_amount IS NULL
                OR budget_floor_amount <= budget_ceiling_amount
            ),
            CONSTRAINT guardrails_caps_check CHECK (
                daily_cap_amount IS NULL OR monthly_cap_amount IS NULL
                OR daily_cap_amount <= monthly_cap_amount
            )
        )
    """)
    op.execute("""
        CREATE TRIGGER guardrails_set_updated_at
        BEFORE UPDATE ON guardrails FOR EACH ROW EXECUTE FUNCTION set_updated_at()
    """)
    op.execute("CREATE INDEX ix_guardrails_business ON guardrails (business_id)")
    op.execute("""
        CREATE INDEX ix_guardrails_platform_account ON guardrails (platform_account_id)
    """)
    op.execute("CREATE INDEX ix_guardrails_campaign ON guardrails (campaign_entity_ref)")

    op.execute("""
        CREATE TABLE emergency_brakes (
            id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            scope_kind           TEXT NOT NULL CHECK (scope_kind IN
                                    ('global', 'business', 'platform_account')),
            business_id          UUID REFERENCES businesses (id) ON DELETE CASCADE,
            platform_account_id  UUID REFERENCES platform_accounts (id) ON DELETE CASCADE,
            scope_key            TEXT GENERATED ALWAYS AS (
                                    COALESCE(business_id::text, '*') || '|' ||
                                    COALESCE(platform_account_id::text, '*')) STORED,
            mode                 TEXT NOT NULL CHECK (mode IN ('AUTONOMOUS', 'ALL')),
            reason               TEXT NOT NULL,
            engaged_by           TEXT NOT NULL,
            engaged_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
            released_by          TEXT,
            released_at          TIMESTAMPTZ,
            created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),

            CONSTRAINT emergency_brakes_scope_coherence_check CHECK (
                (scope_kind = 'global'
                    AND business_id IS NULL AND platform_account_id IS NULL)
                OR (scope_kind = 'business'
                    AND business_id IS NOT NULL AND platform_account_id IS NULL)
                OR (scope_kind = 'platform_account' AND platform_account_id IS NOT NULL)
            ),
            CONSTRAINT emergency_brakes_release_check CHECK (
                (released_at IS NULL AND released_by IS NULL)
                OR (released_at IS NOT NULL AND released_by IS NOT NULL
                    AND released_at >= engaged_at)
            )
        )
    """)
    # "un freno activo por ambito como maximo": lo garantiza el indice, no el
    # codigo. Dos pulsaciones simultaneas no crean dos frenos.
    op.execute("""
        CREATE UNIQUE INDEX ix_emergency_brakes_active
        ON emergency_brakes (scope_kind, scope_key)
        WHERE released_at IS NULL
    """)

    op.execute("""
        CREATE TABLE rule_firings (
            id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            rule_id       UUID NOT NULL REFERENCES rules (id) ON DELETE RESTRICT,
            business_id   UUID NOT NULL REFERENCES businesses (id) ON DELETE RESTRICT,
            entity_ref    TEXT NOT NULL,
            signal_id     UUID REFERENCES signals (id) ON DELETE SET NULL,
            -- Sin FK a `proposals`: esa tabla llega en 0008 (otro carril).
            proposal_id   UUID,
            outcome       TEXT NOT NULL CHECK (outcome IN
                            ('PROPOSED', 'AUTHORIZED', 'SUPPRESSED_COOLDOWN',
                             'SUPPRESSED_DAILY_CAP', 'SUPPRESSED_BRAKE',
                             'SUPPRESSED_STALE', 'SUPPRESSED_GUARDRAIL')),
            evidence      JSONB NOT NULL DEFAULT '{}'::jsonb,
            cycle_id      UUID NOT NULL,
            fired_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

            CONSTRAINT rule_firings_entity_fk
                FOREIGN KEY (business_id, entity_ref)
                REFERENCES ad_entities (business_id, entity_ref) ON DELETE RESTRICT,
            -- Reejecutar un ciclo no vuelve a disparar la misma regla.
            CONSTRAINT rule_firings_cycle_unique UNIQUE (rule_id, entity_ref, cycle_id)
        )
    """)
    # CooldownGate y max_firings_per_day: ultimo disparo y recuento del dia
    # para (regla, entidad).
    op.execute("""
        CREATE INDEX ix_rule_firings_cooldown
        ON rule_firings (rule_id, entity_ref, fired_at DESC)
    """)
    op.execute("""
        CREATE INDEX ix_rule_firings_business_time ON rule_firings (business_id, fired_at DESC)
    """)
    op.execute("CREATE INDEX ix_rule_firings_signal ON rule_firings (signal_id)")

    # Semilla: 37 codigos del catalogo (M01-M24, G01-G11, X01-X02), ambito
    # global, NOTIFY y deshabilitados hasta que el cargador los calibre.
    op.execute("""
        CREATE FUNCTION seed_rule_catalog() RETURNS INTEGER AS $$
        DECLARE inserted INTEGER;
        BEGIN
            INSERT INTO rules (code, scope, platform, autonomy_level, is_enabled)
            SELECT catalog.code,
                   'global',
                   CASE left(catalog.code, 1)
                       WHEN 'M' THEN 'meta'
                       WHEN 'G' THEN 'google'
                       ELSE NULL
                   END,
                   'NOTIFY',
                   false
              FROM (
                    SELECT 'M' || to_char(n, 'FM00') AS code FROM generate_series(1, 24) AS n
                    UNION ALL
                    SELECT 'G' || to_char(n, 'FM00') FROM generate_series(1, 11) AS n
                    UNION ALL
                    SELECT 'X' || to_char(n, 'FM00') FROM generate_series(1, 2) AS n
                   ) AS catalog
            ON CONFLICT ON CONSTRAINT rules_code_scope_unique DO NOTHING;
            GET DIAGNOSTICS inserted = ROW_COUNT;
            RETURN inserted;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("SELECT seed_rule_catalog()")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS seed_rule_catalog()")
    op.execute("DROP TABLE rule_firings")
    op.execute("DROP TABLE emergency_brakes")
    op.execute("DROP TABLE guardrails")
    op.execute("DROP TABLE rules")
    op.execute("ALTER TABLE platform_accounts DROP COLUMN autonomy_enabled")
