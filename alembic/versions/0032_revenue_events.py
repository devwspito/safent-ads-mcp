"""0032_revenue_events: `revenue_events` (spec 027 T014, data-model.md
§RevenueEvent): primera compra, cobro recurrente, devolucion o baja. Una
devolucion es un hecho de importe NEGATIVO, jamas el borrado del cobro
original (FR-019, "sin reescribir la historia").

FK compuesta `(customer_id, business_id) -> customers (id, business_id)`
(mismo patron que `0029_economics_inputs::offering_economics`): una fila
de `revenue_events` nunca puede apuntar a un `customer_id` de OTRO
negocio, invariante de IDOR a nivel de esquema.

Idempotencia por esquema, no por codigo: UNIQUE `(business_id,
connector_id, source_event_id)` -- reenviar un lote entero es seguro por
construccion (contracts/crm-link.md §2).

Desviacion documentada de data-model.md ("trigger de no-UPDATE/DELETE,
igual que decision_log"): el propio data-model.md exige tambien que
`ForgetCustomer` borre estas filas por `identity_digest` dentro de una
transaccion (A-2, "Retencion y olvido"). Ambas cosas no pueden ser
ciertas a la vez con un trigger que bloquee DELETE sin ademas inventar un
mecanismo de bypass (superficie nueva, fuera de alcance sin revision de
`security-engineer`). La invariante que de verdad protege dinero es "nadie
corrige un hecho en su sitio" -- el trigger bloquea UPDATE siempre; DELETE
queda disponible solo a traves de `RevenueEventRepository.
delete_for_customer` (el unico metodo de borrado del puerto, sin
`update`/`delete` generico en la superficie de aplicacion).

Revision ID: 0032_revenue_events
Revises: 0031_customers
Create Date: 2026-09-10
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0032_revenue_events"
down_revision: str | None = "0031_customers"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_KIND_CHECK = "kind IN ('first_payment', 'recurring_payment', 'refund', 'churn')"
_REDUCING_KIND_CHECK = (
    "(kind IN ('refund', 'churn') AND amount_minor <= 0) "
    "OR (kind IN ('first_payment', 'recurring_payment') AND amount_minor > 0)"
)


def upgrade() -> None:
    op.execute("""
        ALTER TABLE customers
            ADD CONSTRAINT customers_id_business_id_unique UNIQUE (id, business_id)
    """)
    op.execute(f"""
        CREATE TABLE revenue_events (
            id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            business_id       UUID NOT NULL REFERENCES businesses (id) ON DELETE RESTRICT,
            connector_id      TEXT NOT NULL,
            customer_id       UUID NOT NULL,
            source_event_id   TEXT NOT NULL,
            kind              TEXT NOT NULL CHECK ({_KIND_CHECK}),
            amount_minor      BIGINT NOT NULL CHECK ({_REDUCING_KIND_CHECK}),
            currency          TEXT NOT NULL CHECK (char_length(currency) = 3),
            occurred_at       TIMESTAMPTZ NOT NULL,
            observed_at       TIMESTAMPTZ NOT NULL,
            mapping_version   INTEGER NOT NULL CHECK (mapping_version >= 1),
            created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

            CONSTRAINT revenue_events_idempotency_unique
                UNIQUE (business_id, connector_id, source_event_id),
            CONSTRAINT revenue_events_customer_business_fkey
                FOREIGN KEY (customer_id, business_id)
                REFERENCES customers (id, business_id) ON DELETE RESTRICT
        )
    """)
    op.execute("""
        COMMENT ON TABLE revenue_events IS
        'Solo-anexable frente a CORRECCION (FR-019): una devolucion resta
        con su propio hecho negativo, nunca reescribe el cobro original.
        DELETE existe unicamente para el olvido de cliente (A-2) via
        RevenueEventRepository.delete_for_customer -- ver docstring de la
        migracion para la desviacion documentada sobre no-DELETE.'
    """)
    op.execute(
        "CREATE INDEX ix_revenue_events_customer_occurred "
        "ON revenue_events (customer_id, occurred_at)"
    )
    op.execute(
        "CREATE INDEX ix_revenue_events_business_occurred "
        "ON revenue_events (business_id, occurred_at DESC)"
    )

    op.execute("""
        CREATE FUNCTION revenue_events_no_update() RETURNS TRIGGER AS $$
        BEGIN
            RAISE EXCEPTION 'revenue_events es solo-anexable: UPDATE no permitido (FR-019)';
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER revenue_events_no_update
        BEFORE UPDATE ON revenue_events
        FOR EACH ROW EXECUTE FUNCTION revenue_events_no_update()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS revenue_events_no_update ON revenue_events")
    op.execute("DROP FUNCTION IF EXISTS revenue_events_no_update()")
    op.execute("DROP TABLE revenue_events")
    op.execute("ALTER TABLE customers DROP CONSTRAINT customers_id_business_id_unique")
