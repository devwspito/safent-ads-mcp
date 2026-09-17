"""0004_metrics_facts: hechos metricos. `metrics_daily` particionada por
mes, `metrics_hourly` con retencion de 14 dias, `metrics_restatements`
solo-anexable y `data_freshness`.

data-model.md §MetricFact: "clave natural (entity_ref, stat_date[,
stat_hour]); la reingesta es UPSERT, jamas INSERT duplicado (FR-5, NFR-6);
stat_date en la zona horaria de la cuenta, no del servidor; las tasas (CTR,
CPA, ROAS) no se almacenan: se derivan como razon de sumas por ventana".

Solo contadores crudos y valores monetarios. Las unicas razones que se
guardan son las cuotas de impresiones perdidas de Google, que la plataforma
reporta y no se pueden reconstruir desde nuestros contadores.

Revision ID: 0004_metrics_facts
Revises: 0003_ad_entities
Create Date: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004_metrics_facts"
down_revision: str | None = "0003_ad_entities"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE metrics_daily (
            business_id           UUID NOT NULL,
            entity_ref            TEXT NOT NULL,
            entity_level          TEXT NOT NULL
                                    CHECK (entity_level IN
                                        ('campaign', 'ad_set', 'ad', 'creative')),
            platform_account_id   UUID NOT NULL REFERENCES platform_accounts (id)
                                    ON DELETE RESTRICT,
            stat_date             DATE NOT NULL,
            account_timezone      TEXT NOT NULL,
            currency              TEXT NOT NULL CHECK (char_length(currency) = 3),

            spend                 NUMERIC(16, 4) NOT NULL DEFAULT 0 CHECK (spend >= 0),
            impressions           BIGINT NOT NULL DEFAULT 0 CHECK (impressions >= 0),
            clicks                BIGINT NOT NULL DEFAULT 0 CHECK (clicks >= 0),
            reach                 BIGINT CHECK (reach >= 0),
            conversions_lead      BIGINT NOT NULL DEFAULT 0 CHECK (conversions_lead >= 0),
            conversions_whatsapp  BIGINT NOT NULL DEFAULT 0 CHECK (conversions_whatsapp >= 0),
            conversions_call      BIGINT NOT NULL DEFAULT 0 CHECK (conversions_call >= 0),
            conversions_enrolment BIGINT NOT NULL DEFAULT 0 CHECK (conversions_enrolment >= 0),
            conversion_value      NUMERIC(16, 4) NOT NULL DEFAULT 0
                                    CHECK (conversion_value >= 0),
            video_views_3s        BIGINT CHECK (video_views_3s >= 0),
            video_views_75pct     BIGINT CHECK (video_views_75pct >= 0),
            search_lost_is_budget NUMERIC(6, 4)
                                    CHECK (search_lost_is_budget BETWEEN 0 AND 1),
            search_lost_is_rank   NUMERIC(6, 4)
                                    CHECK (search_lost_is_rank BETWEEN 0 AND 1),

            revision              INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
            ingested_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
            created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),

            CONSTRAINT metrics_daily_pkey PRIMARY KEY (entity_ref, stat_date),
            CONSTRAINT metrics_daily_entity_fk
                FOREIGN KEY (business_id, entity_ref)
                REFERENCES ad_entities (business_id, entity_ref) ON DELETE RESTRICT
        ) PARTITION BY RANGE (stat_date)
    """)
    op.execute("""
        COMMENT ON TABLE metrics_daily IS
        'Contadores crudos por dia y entidad. CTR/CPA/ROAS/frecuencia se derivan '
        'como razon de sumas sobre la ventana; nunca se almacenan (data-model.md).'
    """)
    op.execute("""
        COMMENT ON COLUMN metrics_daily.stat_date IS
        'Dia en la zona horaria de la cuenta (account_timezone), no la del servidor.'
    """)

    op.execute("""
        CREATE FUNCTION metrics_daily_ensure_partition(p_month DATE) RETURNS TEXT AS $$
        DECLARE
            month_start DATE := date_trunc('month', p_month)::date;
            month_end   DATE := (date_trunc('month', p_month) + INTERVAL '1 month')::date;
            part_name   TEXT := 'metrics_daily_' || to_char(month_start, 'YYYY_MM');
        BEGIN
            EXECUTE format(
                'CREATE TABLE IF NOT EXISTS %I PARTITION OF metrics_daily '
                'FOR VALUES FROM (%L) TO (%L)', part_name, month_start, month_end);
            RETURN part_name;
        END;
        $$ LANGUAGE plpgsql
    """)

    # Retencion de 37 meses (data-model.md). No se invoca desde la migracion:
    # borrar particiones es perdida de datos y la decide una tarea de
    # mantenimiento, con su registro. El guardarrail de 12 meses evita que un
    # parametro mal tecleado se lleve el historico por delante.
    op.execute("""
        CREATE FUNCTION metrics_daily_drop_old_partitions(p_keep_months INT DEFAULT 37)
        RETURNS INT AS $$
        DECLARE
            part    RECORD;
            cutoff  DATE;
            dropped INT := 0;
        BEGIN
            IF p_keep_months < 12 THEN
                RAISE EXCEPTION 'retencion minima 12 meses, se pidieron %', p_keep_months;
            END IF;
            cutoff := (date_trunc('month', current_date)
                       - make_interval(months => p_keep_months))::date;
            FOR part IN
                SELECT c.relname AS name
                  FROM pg_inherits i
                  JOIN pg_class c ON c.oid = i.inhrelid
                 WHERE i.inhparent = 'metrics_daily'::regclass
                   AND c.relname ~ '^metrics_daily_[0-9]{4}_[0-9]{2}$'
            LOOP
                IF to_date(right(part.name, 7), 'YYYY_MM') < cutoff THEN
                    EXECUTE format('DROP TABLE %I', part.name);
                    dropped := dropped + 1;
                END IF;
            END LOOP;
            RETURN dropped;
        END;
        $$ LANGUAGE plpgsql
    """)

    # Particiones del mes anterior a los tres siguientes: la ingesta nunca
    # depende de que el mantenimiento haya corrido. La particion DEFAULT es
    # la red: una fecha inesperada entra en vez de reventar el lote.
    op.execute("""
        DO $$
        DECLARE offset_month INT;
        BEGIN
            FOR offset_month IN -1..3 LOOP
                PERFORM metrics_daily_ensure_partition(
                    (current_date + make_interval(months => offset_month))::date);
            END LOOP;
        END
        $$
    """)
    op.execute("CREATE TABLE metrics_daily_default PARTITION OF metrics_daily DEFAULT")

    # GET /portfolio?window=7D: suma por negocio sobre una ventana reciente.
    op.execute("""
        CREATE INDEX ix_metrics_daily_business_date
        ON metrics_daily (business_id, stat_date DESC)
    """)

    op.execute("""
        CREATE TABLE metrics_hourly (
            business_id           UUID NOT NULL,
            entity_ref            TEXT NOT NULL,
            platform_account_id   UUID NOT NULL REFERENCES platform_accounts (id)
                                    ON DELETE RESTRICT,
            stat_date             DATE NOT NULL,
            stat_hour             SMALLINT NOT NULL CHECK (stat_hour BETWEEN 0 AND 23),
            account_timezone      TEXT NOT NULL,
            currency              TEXT NOT NULL CHECK (char_length(currency) = 3),

            spend                 NUMERIC(16, 4) NOT NULL DEFAULT 0 CHECK (spend >= 0),
            impressions           BIGINT NOT NULL DEFAULT 0 CHECK (impressions >= 0),
            clicks                BIGINT NOT NULL DEFAULT 0 CHECK (clicks >= 0),
            conversions_lead      BIGINT NOT NULL DEFAULT 0 CHECK (conversions_lead >= 0),
            conversions_whatsapp  BIGINT NOT NULL DEFAULT 0 CHECK (conversions_whatsapp >= 0),
            conversions_call      BIGINT NOT NULL DEFAULT 0 CHECK (conversions_call >= 0),
            conversions_enrolment BIGINT NOT NULL DEFAULT 0 CHECK (conversions_enrolment >= 0),
            conversion_value      NUMERIC(16, 4) NOT NULL DEFAULT 0
                                    CHECK (conversion_value >= 0),

            revision              INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
            ingested_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
            created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),

            CONSTRAINT metrics_hourly_pkey PRIMARY KEY (entity_ref, stat_date, stat_hour),
            CONSTRAINT metrics_hourly_entity_fk
                FOREIGN KEY (business_id, entity_ref)
                REFERENCES ad_entities (business_id, entity_ref) ON DELETE RESTRICT
        )
    """)
    # Reglas intradia (M12, X01, G10): ultimas horas de una cuenta.
    op.execute("""
        CREATE INDEX ix_metrics_hourly_business_date_hour
        ON metrics_hourly (business_id, stat_date DESC, stat_hour DESC)
    """)
    # 14 dias de retencion. Un indice parcial con `now()` no es posible
    # (predicado no inmutable): la ventana se mantiene purgando.
    op.execute("""
        CREATE FUNCTION metrics_hourly_purge(p_keep_days INT DEFAULT 14) RETURNS BIGINT AS $$
        DECLARE deleted BIGINT;
        BEGIN
            IF p_keep_days < 2 THEN
                RAISE EXCEPTION 'retencion minima 2 dias, se pidieron %', p_keep_days;
            END IF;
            DELETE FROM metrics_hourly
             WHERE stat_date < (current_date - make_interval(days => p_keep_days))::date;
            GET DIAGNOSTICS deleted = ROW_COUNT;
            RETURN deleted;
        END;
        $$ LANGUAGE plpgsql
    """)

    # "solo-anexable; guarda old_value/new_value por metrica corregida para
    # que NFR-7 se cumpla sin versionar toda la tabla de hechos".
    op.execute("""
        CREATE TABLE metrics_restatements (
            id            BIGSERIAL PRIMARY KEY,
            business_id   UUID NOT NULL REFERENCES businesses (id) ON DELETE RESTRICT,
            entity_ref    TEXT NOT NULL,
            stat_date     DATE NOT NULL,
            stat_hour     SMALLINT CHECK (stat_hour BETWEEN 0 AND 23),
            metric        TEXT NOT NULL,
            old_value     NUMERIC(20, 4),
            new_value     NUMERIC(20, 4) NOT NULL,
            reason        TEXT NOT NULL,
            source        TEXT NOT NULL CHECK (source IN ('platform', 'crm')),
            revision      INTEGER NOT NULL CHECK (revision >= 1),
            cycle_id      UUID,
            restated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT metrics_restatements_value_changed
                CHECK (old_value IS NULL OR old_value IS DISTINCT FROM new_value)
        )
    """)
    # Sin FK a metrics_daily: la retencion elimina particiones y una FK
    # convertiria ese borrado en un error. El hecho corregido puede haber
    # caducado; la correccion sobrevive.
    op.execute("""
        CREATE INDEX ix_metrics_restatements_entity_date
        ON metrics_restatements (entity_ref, stat_date DESC)
    """)
    op.execute("""
        CREATE INDEX ix_metrics_restatements_business_time
        ON metrics_restatements (business_id, restated_at DESC)
    """)
    op.execute("""
        CREATE FUNCTION metrics_restatements_immutable() RETURNS TRIGGER AS $$
        BEGIN
            RAISE EXCEPTION 'metrics_restatements es append-only: % no permitido', TG_OP;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER metrics_restatements_no_update
        BEFORE UPDATE ON metrics_restatements
        FOR EACH ROW EXECUTE FUNCTION metrics_restatements_immutable()
    """)
    op.execute("""
        CREATE TRIGGER metrics_restatements_no_delete
        BEFORE DELETE ON metrics_restatements
        FOR EACH ROW EXECUTE FUNCTION metrics_restatements_immutable()
    """)

    # "una fila por (platform_account, entity_level); is_stale se deriva de
    # lag_minutes > threshold".
    op.execute("""
        CREATE TABLE data_freshness (
            platform_account_id     UUID NOT NULL REFERENCES platform_accounts (id)
                                        ON DELETE CASCADE,
            entity_level            TEXT NOT NULL
                                        CHECK (entity_level IN
                                            ('campaign', 'ad_set', 'ad', 'creative')),
            granularity             TEXT NOT NULL DEFAULT 'daily'
                                        CHECK (granularity IN ('daily', 'hourly')),
            last_ingested_at        TIMESTAMPTZ,
            lag_minutes             INTEGER NOT NULL DEFAULT 0 CHECK (lag_minutes >= 0),
            stale_threshold_minutes INTEGER NOT NULL DEFAULT 60
                                        CHECK (stale_threshold_minutes > 0),
            is_stale                BOOLEAN GENERATED ALWAYS AS
                                        (lag_minutes > stale_threshold_minutes) STORED,
            last_error              TEXT,
            created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT data_freshness_pkey
                PRIMARY KEY (platform_account_id, entity_level, granularity)
        )
    """)
    op.execute("""
        CREATE TRIGGER data_freshness_set_updated_at
        BEFORE UPDATE ON data_freshness
        FOR EACH ROW EXECUTE FUNCTION set_updated_at()
    """)


def downgrade() -> None:
    op.execute("DROP TABLE data_freshness")
    op.execute("DROP TABLE metrics_restatements")
    op.execute("DROP FUNCTION IF EXISTS metrics_restatements_immutable()")
    op.execute("DROP FUNCTION IF EXISTS metrics_hourly_purge(INT)")
    op.execute("DROP TABLE metrics_hourly")
    op.execute("DROP TABLE metrics_daily")
    op.execute("DROP FUNCTION IF EXISTS metrics_daily_drop_old_partitions(INT)")
    op.execute("DROP FUNCTION IF EXISTS metrics_daily_ensure_partition(DATE)")
