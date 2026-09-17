"""0025_vocabulary: lenguaje ubicuo generico (vocabulary.md T175-T180).
Solo renombres de tablas/columnas/constraints/indices/triggers; sin vistas
de compatibilidad, `downgrade` simetrico.

- `courses` -> `offerings`: `cuerpo`/`especialidad` -> `category`/
  `subcategory`.
- `convocatorias` -> `calendar_events`: `course_id`->`offering_id`,
  `denominacion`->`name`, `comunidad`->`region`, `fecha_inicio_plazo`->
  `window_start`, `fecha_fin_plazo`->`window_end`, `fecha_examen`->
  `event_date`, `fuente`->`source`, `fuente_url`->`source_url`. Anade
  `kind` (`season|deadline|launch|promotion`, default `season`: toda fila
  existente era, de hecho, una temporada de examen) y el CHECK de longitud
  de `name`. `region` pasa a admitir NULL -- `CalendarEvent` generaliza a
  hitos sin alcance geografico (una `promotion` de marca nacional, p. ej.);
  el CHECK de longitud (ahora `region_check`) sigue vivo y sigue
  cumpliendose en NULL (Postgres evalua un CHECK a verdadero cuando el
  predicado es UNKNOWN), asi que solo se relaja la nulabilidad, nunca el
  formato de un valor presente.
- `lead_attributions.convocatoria_id`/`proposals.convocatoria_id` ->
  `calendar_event_id`; `creative_briefs.convocatoria_id` -> mismo rename,
  sin FK ni indice (nunca los tuvo). `conversion_kind='enrolment'` ->
  `'business_conversion'` en `lead_attributions`, con su CHECK.
- `metrics_daily`/`metrics_hourly.conversions_enrolment` ->
  `conversions_business_conversion`. `metrics_daily` esta particionada por
  rango de mes: `ALTER TABLE ... RENAME COLUMN`/`RENAME CONSTRAINT` sobre
  el padre cascada sola a cada particion (verificado contra Postgres 16),
  asi que no hace falta un bucle manual sobre `pg_inherits` para cada
  partición existente -- las que se creen despues
  (`metrics_daily_ensure_partition`) ya nacen con los nombres nuevos.
- `unit_economics_profiles.cvr_lead_to_enrolment` ->
  `cvr_lead_to_business_conversion`, con su CHECK.

Revision ID: 0025_vocabulary
Revises: 0023_owner_settings
Create Date: 2026-09-10
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0025_vocabulary"
down_revision: str | None = "0024_brake_confirmations"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    _upgrade_offerings()
    _upgrade_calendar_events()
    _upgrade_lead_attributions()
    _upgrade_proposals()
    _upgrade_creative_briefs()
    _upgrade_metrics_daily()
    _upgrade_metrics_hourly()
    _upgrade_unit_economics_profiles()


def downgrade() -> None:
    _downgrade_unit_economics_profiles()
    _downgrade_metrics_hourly()
    _downgrade_metrics_daily()
    _downgrade_creative_briefs()
    _downgrade_proposals()
    _downgrade_lead_attributions()
    _downgrade_calendar_events()
    _downgrade_offerings()


# ---------------------------------------------------------------------------
# `courses` -> `offerings`
# ---------------------------------------------------------------------------


def _upgrade_offerings() -> None:
    op.execute("ALTER TABLE courses RENAME TO offerings")
    op.execute("ALTER TABLE offerings RENAME COLUMN cuerpo TO category")
    op.execute("ALTER TABLE offerings RENAME COLUMN especialidad TO subcategory")
    op.execute("ALTER TABLE offerings RENAME CONSTRAINT courses_pkey TO offerings_pkey")
    op.execute(
        "ALTER TABLE offerings RENAME CONSTRAINT courses_business_id_fkey "
        "TO offerings_business_id_fkey"
    )
    op.execute(
        "ALTER TABLE offerings RENAME CONSTRAINT courses_business_code_unique "
        "TO offerings_business_code_unique"
    )
    op.execute(
        "ALTER TABLE offerings RENAME CONSTRAINT courses_price_pair_check "
        "TO offerings_price_pair_check"
    )
    op.execute("ALTER TABLE offerings RENAME CONSTRAINT courses_code_check TO offerings_code_check")
    op.execute(
        "ALTER TABLE offerings RENAME CONSTRAINT courses_price_amount_check "
        "TO offerings_price_amount_check"
    )
    op.execute(
        "ALTER TABLE offerings RENAME CONSTRAINT courses_price_currency_check "
        "TO offerings_price_currency_check"
    )
    op.execute(
        "ALTER TRIGGER courses_set_updated_at ON offerings RENAME TO offerings_set_updated_at"
    )


def _downgrade_offerings() -> None:
    op.execute(
        "ALTER TRIGGER offerings_set_updated_at ON offerings RENAME TO courses_set_updated_at"
    )
    op.execute(
        "ALTER TABLE offerings RENAME CONSTRAINT offerings_price_currency_check "
        "TO courses_price_currency_check"
    )
    op.execute(
        "ALTER TABLE offerings RENAME CONSTRAINT offerings_price_amount_check "
        "TO courses_price_amount_check"
    )
    op.execute("ALTER TABLE offerings RENAME CONSTRAINT offerings_code_check TO courses_code_check")
    op.execute(
        "ALTER TABLE offerings RENAME CONSTRAINT offerings_price_pair_check "
        "TO courses_price_pair_check"
    )
    op.execute(
        "ALTER TABLE offerings RENAME CONSTRAINT offerings_business_code_unique "
        "TO courses_business_code_unique"
    )
    op.execute(
        "ALTER TABLE offerings RENAME CONSTRAINT offerings_business_id_fkey "
        "TO courses_business_id_fkey"
    )
    op.execute("ALTER TABLE offerings RENAME CONSTRAINT offerings_pkey TO courses_pkey")
    op.execute("ALTER TABLE offerings RENAME COLUMN subcategory TO especialidad")
    op.execute("ALTER TABLE offerings RENAME COLUMN category TO cuerpo")
    op.execute("ALTER TABLE offerings RENAME TO courses")


# ---------------------------------------------------------------------------
# `convocatorias` -> `calendar_events`
# ---------------------------------------------------------------------------


def _upgrade_calendar_events() -> None:
    op.execute("ALTER TABLE convocatorias RENAME TO calendar_events")
    op.execute("ALTER TABLE calendar_events RENAME COLUMN course_id TO offering_id")
    op.execute("ALTER TABLE calendar_events RENAME COLUMN denominacion TO name")
    op.execute("ALTER TABLE calendar_events RENAME COLUMN comunidad TO region")
    op.execute("ALTER TABLE calendar_events RENAME COLUMN fecha_inicio_plazo TO window_start")
    op.execute("ALTER TABLE calendar_events RENAME COLUMN fecha_fin_plazo TO window_end")
    op.execute("ALTER TABLE calendar_events RENAME COLUMN fecha_examen TO event_date")
    op.execute("ALTER TABLE calendar_events RENAME COLUMN fuente TO source")
    op.execute("ALTER TABLE calendar_events RENAME COLUMN fuente_url TO source_url")
    op.execute("ALTER TABLE calendar_events ALTER COLUMN region DROP NOT NULL")
    op.execute("""
        ALTER TABLE calendar_events ADD COLUMN kind TEXT NOT NULL DEFAULT 'season'
            CHECK (kind IN ('season', 'deadline', 'launch', 'promotion'))
    """)
    op.execute("""
        ALTER TABLE calendar_events ADD CONSTRAINT calendar_events_name_check
            CHECK (char_length(name) BETWEEN 1 AND 120)
    """)
    op.execute(
        "ALTER TABLE calendar_events RENAME CONSTRAINT convocatorias_pkey "
        "TO calendar_events_pkey"
    )
    op.execute(
        "ALTER TABLE calendar_events RENAME CONSTRAINT convocatorias_business_id_fkey "
        "TO calendar_events_business_id_fkey"
    )
    op.execute(
        "ALTER TABLE calendar_events RENAME CONSTRAINT convocatorias_course_id_fkey "
        "TO calendar_events_offering_id_fkey"
    )
    op.execute(
        "ALTER TABLE calendar_events RENAME CONSTRAINT convocatorias_comunidad_check "
        "TO calendar_events_region_check"
    )
    op.execute(
        "ALTER TABLE calendar_events RENAME CONSTRAINT convocatorias_plazo_check "
        "TO calendar_events_window_check"
    )
    op.execute(
        "ALTER TABLE calendar_events RENAME CONSTRAINT convocatorias_examen_check "
        "TO calendar_events_event_date_check"
    )
    op.execute(
        "ALTER TABLE calendar_events RENAME CONSTRAINT convocatorias_natural_unique "
        "TO calendar_events_natural_unique"
    )
    op.execute(
        "ALTER INDEX ix_convocatorias_business_plazo RENAME TO ix_calendar_events_business_window"
    )
    op.execute("ALTER INDEX ix_convocatorias_course RENAME TO ix_calendar_events_offering")
    op.execute(
        "ALTER TRIGGER convocatorias_set_updated_at ON calendar_events "
        "RENAME TO calendar_events_set_updated_at"
    )


def _downgrade_calendar_events() -> None:
    op.execute(
        "ALTER TRIGGER calendar_events_set_updated_at ON calendar_events "
        "RENAME TO convocatorias_set_updated_at"
    )
    op.execute(
        "ALTER INDEX ix_calendar_events_offering RENAME TO ix_convocatorias_course"
    )
    op.execute(
        "ALTER INDEX ix_calendar_events_business_window RENAME TO ix_convocatorias_business_plazo"
    )
    op.execute(
        "ALTER TABLE calendar_events RENAME CONSTRAINT calendar_events_natural_unique "
        "TO convocatorias_natural_unique"
    )
    op.execute(
        "ALTER TABLE calendar_events RENAME CONSTRAINT calendar_events_event_date_check "
        "TO convocatorias_examen_check"
    )
    op.execute(
        "ALTER TABLE calendar_events RENAME CONSTRAINT calendar_events_window_check "
        "TO convocatorias_plazo_check"
    )
    op.execute(
        "ALTER TABLE calendar_events RENAME CONSTRAINT calendar_events_region_check "
        "TO convocatorias_comunidad_check"
    )
    op.execute(
        "ALTER TABLE calendar_events RENAME CONSTRAINT calendar_events_offering_id_fkey "
        "TO convocatorias_course_id_fkey"
    )
    op.execute(
        "ALTER TABLE calendar_events RENAME CONSTRAINT calendar_events_business_id_fkey "
        "TO convocatorias_business_id_fkey"
    )
    op.execute(
        "ALTER TABLE calendar_events RENAME CONSTRAINT calendar_events_pkey "
        "TO convocatorias_pkey"
    )
    op.execute("ALTER TABLE calendar_events DROP CONSTRAINT calendar_events_name_check")
    op.execute("ALTER TABLE calendar_events DROP COLUMN kind")
    # Simetria: los eventos que se creasen sin `region` (permitido tras el
    # upgrade) no tendrian valor que devolver a `NOT NULL` -- consistente
    # con "sin vistas de compatibilidad", el downgrade asume una base
    # recien migrada de vuelta, no datos nuevos creados bajo el esquema
    # generico.
    op.execute("ALTER TABLE calendar_events ALTER COLUMN region SET NOT NULL")
    op.execute("ALTER TABLE calendar_events RENAME COLUMN source_url TO fuente_url")
    op.execute("ALTER TABLE calendar_events RENAME COLUMN source TO fuente")
    op.execute("ALTER TABLE calendar_events RENAME COLUMN event_date TO fecha_examen")
    op.execute("ALTER TABLE calendar_events RENAME COLUMN window_end TO fecha_fin_plazo")
    op.execute("ALTER TABLE calendar_events RENAME COLUMN window_start TO fecha_inicio_plazo")
    op.execute("ALTER TABLE calendar_events RENAME COLUMN region TO comunidad")
    op.execute("ALTER TABLE calendar_events RENAME COLUMN name TO denominacion")
    op.execute("ALTER TABLE calendar_events RENAME COLUMN offering_id TO course_id")
    op.execute("ALTER TABLE calendar_events RENAME TO convocatorias")


# ---------------------------------------------------------------------------
# `lead_attributions.convocatoria_id` -> `calendar_event_id`;
# `conversion_kind`: `enrolment` -> `business_conversion`
# ---------------------------------------------------------------------------

_LEAD_ATTRIBUTIONS_KIND_CHECK_UP = """
    ALTER TABLE lead_attributions ADD CONSTRAINT lead_attributions_conversion_kind_check
        CHECK (conversion_kind IN ('lead', 'whatsapp', 'call', 'business_conversion'))
"""
_LEAD_ATTRIBUTIONS_KIND_CHECK_DOWN = """
    ALTER TABLE lead_attributions ADD CONSTRAINT lead_attributions_conversion_kind_check
        CHECK (conversion_kind IN ('lead', 'whatsapp', 'call', 'enrolment'))
"""


def _upgrade_lead_attributions() -> None:
    op.execute("ALTER TABLE lead_attributions RENAME COLUMN convocatoria_id TO calendar_event_id")
    op.execute(
        "ALTER TABLE lead_attributions RENAME CONSTRAINT lead_attributions_convocatoria_id_fkey "
        "TO lead_attributions_calendar_event_id_fkey"
    )
    op.execute(
        "ALTER INDEX ix_lead_attributions_convocatoria "
        "RENAME TO ix_lead_attributions_calendar_event"
    )
    op.execute(
        "ALTER TABLE lead_attributions DROP CONSTRAINT lead_attributions_conversion_kind_check"
    )
    op.execute(
        "UPDATE lead_attributions SET conversion_kind = 'business_conversion' "
        "WHERE conversion_kind = 'enrolment'"
    )
    op.execute(_LEAD_ATTRIBUTIONS_KIND_CHECK_UP)


def _downgrade_lead_attributions() -> None:
    op.execute(
        "ALTER TABLE lead_attributions DROP CONSTRAINT lead_attributions_conversion_kind_check"
    )
    op.execute(
        "UPDATE lead_attributions SET conversion_kind = 'enrolment' "
        "WHERE conversion_kind = 'business_conversion'"
    )
    op.execute(_LEAD_ATTRIBUTIONS_KIND_CHECK_DOWN)
    op.execute(
        "ALTER INDEX ix_lead_attributions_calendar_event "
        "RENAME TO ix_lead_attributions_convocatoria"
    )
    op.execute(
        "ALTER TABLE lead_attributions RENAME CONSTRAINT lead_attributions_calendar_event_id_fkey "
        "TO lead_attributions_convocatoria_id_fkey"
    )
    op.execute("ALTER TABLE lead_attributions RENAME COLUMN calendar_event_id TO convocatoria_id")


# ---------------------------------------------------------------------------
# `proposals.convocatoria_id` -> `calendar_event_id`
# ---------------------------------------------------------------------------


def _upgrade_proposals() -> None:
    op.execute("ALTER TABLE proposals RENAME COLUMN convocatoria_id TO calendar_event_id")
    op.execute(
        "ALTER TABLE proposals RENAME CONSTRAINT proposals_convocatoria_id_fkey "
        "TO proposals_calendar_event_id_fkey"
    )
    op.execute("ALTER INDEX ix_proposals_convocatoria RENAME TO ix_proposals_calendar_event")


def _downgrade_proposals() -> None:
    op.execute("ALTER INDEX ix_proposals_calendar_event RENAME TO ix_proposals_convocatoria")
    op.execute(
        "ALTER TABLE proposals RENAME CONSTRAINT proposals_calendar_event_id_fkey "
        "TO proposals_convocatoria_id_fkey"
    )
    op.execute("ALTER TABLE proposals RENAME COLUMN calendar_event_id TO convocatoria_id")


# ---------------------------------------------------------------------------
# `creative_briefs.convocatoria_id` -> `calendar_event_id` (columna simple,
# nunca tuvo FK ni indice propio)
# ---------------------------------------------------------------------------


def _upgrade_creative_briefs() -> None:
    op.execute("ALTER TABLE creative_briefs RENAME COLUMN convocatoria_id TO calendar_event_id")


def _downgrade_creative_briefs() -> None:
    op.execute("ALTER TABLE creative_briefs RENAME COLUMN calendar_event_id TO convocatoria_id")


# ---------------------------------------------------------------------------
# `metrics_daily`/`metrics_hourly.conversions_enrolment` ->
# `conversions_business_conversion`
# ---------------------------------------------------------------------------


def _upgrade_metrics_daily() -> None:
    op.execute(
        "ALTER TABLE metrics_daily RENAME COLUMN conversions_enrolment "
        "TO conversions_business_conversion"
    )
    op.execute(
        "ALTER TABLE metrics_daily RENAME CONSTRAINT metrics_daily_conversions_enrolment_check "
        "TO metrics_daily_conversions_business_conversion_check"
    )


def _downgrade_metrics_daily() -> None:
    op.execute(
        "ALTER TABLE metrics_daily RENAME CONSTRAINT "
        "metrics_daily_conversions_business_conversion_check "
        "TO metrics_daily_conversions_enrolment_check"
    )
    op.execute(
        "ALTER TABLE metrics_daily RENAME COLUMN conversions_business_conversion "
        "TO conversions_enrolment"
    )


def _upgrade_metrics_hourly() -> None:
    op.execute(
        "ALTER TABLE metrics_hourly RENAME COLUMN conversions_enrolment "
        "TO conversions_business_conversion"
    )
    op.execute(
        "ALTER TABLE metrics_hourly RENAME CONSTRAINT metrics_hourly_conversions_enrolment_check "
        "TO metrics_hourly_conversions_business_conversion_check"
    )


def _downgrade_metrics_hourly() -> None:
    op.execute(
        "ALTER TABLE metrics_hourly RENAME CONSTRAINT "
        "metrics_hourly_conversions_business_conversion_check "
        "TO metrics_hourly_conversions_enrolment_check"
    )
    op.execute(
        "ALTER TABLE metrics_hourly RENAME COLUMN conversions_business_conversion "
        "TO conversions_enrolment"
    )


# ---------------------------------------------------------------------------
# `unit_economics_profiles.cvr_lead_to_enrolment` ->
# `cvr_lead_to_business_conversion`
# ---------------------------------------------------------------------------


def _upgrade_unit_economics_profiles() -> None:
    op.execute(
        "ALTER TABLE unit_economics_profiles RENAME COLUMN cvr_lead_to_enrolment "
        "TO cvr_lead_to_business_conversion"
    )
    op.execute(
        "ALTER TABLE unit_economics_profiles RENAME CONSTRAINT "
        "unit_economics_profiles_cvr_lead_to_enrolment_check "
        "TO unit_economics_profiles_cvr_lead_to_business_conversion_check"
    )


def _downgrade_unit_economics_profiles() -> None:
    op.execute(
        "ALTER TABLE unit_economics_profiles RENAME CONSTRAINT "
        "unit_economics_profiles_cvr_lead_to_business_conversion_check "
        "TO unit_economics_profiles_cvr_lead_to_enrolment_check"
    )
    op.execute(
        "ALTER TABLE unit_economics_profiles RENAME COLUMN cvr_lead_to_business_conversion "
        "TO cvr_lead_to_enrolment"
    )
