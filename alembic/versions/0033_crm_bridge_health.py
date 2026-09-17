"""0033_crm_bridge_health: `crm_bridge_health` (spec 027 A-3, data-model.md
§CrmBridgeHealth) + `crm_bridge_tokens` -- dos concerns bajo una migracion,
mismo patron que `0029_economics_inputs` (offering_economics +
conversion_webhook_tokens en un unico fichero).

1. `crm_bridge_health`: una fila viva por `(business_id, connector_id)`.
   `has_recent_events_24h` la CALCULA `RecordBridgeHealth` desde el ultimo
   `RevenueEvent`, nunca se declara desde fuera -- `SqlMeasurementFreezeGate`
   la lee para congelar BUY (contracts/crm-link.md §2 `PUT /crm/bridge-
   health`).
2. `crm_bridge_tokens`: autenticacion `X-Bridge-Token` del borde de ingesta
   (contracts/crm-link.md §2: "mismo patron que X-Webhook-Token de
   /conversions/webhook"). Tabla DISTINTA de `conversion_webhook_tokens`
   (0029) a proposito: son dos superficies de confianza distintas -- el
   CRM externo del propietario en un caso, el puente propio
   `connectors`->`ads-api` en el otro. Compartir el secreto ampliaria el
   radio de un token de conversiones filtrado hasta poder escribir
   ingresos y descongelar BUY (Assumption documentada, ver informe de la
   tarea: la emision del token via `POST /crm/bridge-token` no estaba en
   la lista explicita de T016, se anade porque sin ella la superficie es
   inerte).

Revision ID: 0033_crm_bridge_health
Revises: 0032_revenue_events
Create Date: 2026-09-10
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0033_crm_bridge_health"
down_revision: str | None = "0032_revenue_events"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    _upgrade_crm_bridge_health()
    _upgrade_crm_bridge_tokens()


def downgrade() -> None:
    _downgrade_crm_bridge_tokens()
    _downgrade_crm_bridge_health()


def _upgrade_crm_bridge_health() -> None:
    op.execute("""
        CREATE TABLE crm_bridge_health (
            business_id            UUID NOT NULL REFERENCES businesses (id) ON DELETE RESTRICT,
            connector_id           TEXT NOT NULL,
            connector_state        TEXT NOT NULL
                CHECK (connector_state IN ('listo', 'degradado', 'suspendido')),
            last_event_at          TIMESTAMPTZ,
            has_recent_events_24h  BOOLEAN NOT NULL,
            cause                  TEXT,
            updated_at             TIMESTAMPTZ NOT NULL,

            PRIMARY KEY (business_id, connector_id)
        )
    """)
    op.execute("""
        COMMENT ON TABLE crm_bridge_health IS
        'has_recent_events_24h se CALCULA (RecordBridgeHealth), nunca se
        declara: el estado de conector que reporta el runtime solo puede
        empeorarla, nunca mejorarla (contracts/crm-link.md §2).'
    """)
    op.execute("""
        CREATE INDEX ix_crm_bridge_health_unhealthy
        ON crm_bridge_health (business_id) WHERE has_recent_events_24h = false
    """)


def _downgrade_crm_bridge_health() -> None:
    op.execute("DROP TABLE crm_bridge_health")


def _upgrade_crm_bridge_tokens() -> None:
    op.execute("""
        CREATE TABLE crm_bridge_tokens (
            id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            business_id  UUID NOT NULL UNIQUE REFERENCES businesses (id) ON DELETE CASCADE,
            token_hash   TEXT NOT NULL UNIQUE,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)


def _downgrade_crm_bridge_tokens() -> None:
    op.execute("DROP TABLE crm_bridge_tokens")
