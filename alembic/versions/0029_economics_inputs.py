"""0029_economics_inputs: `offering_economics` (T131/T132, profitability-
engine.md §1/§10 -- "lo que debe aportar el dueno") y
`conversion_webhook_tokens` (T220), ambas desde el panel en vez de
`config/economics/<business>.yaml`/env (owner decision: "los numeros de
margen/IVA/CRM tambien desde la UI").

`offering_economics` sustituye a `_NotYetWiredMarginInputsPort`
(orchestration/infrastructure/economics_step.py): IVA, coste variable de
entrega, coste MENSUAL del equipo comercial (`sales_cost_minor` --
profitability-engine.md §10.4: "coste del equipo comercial (mensual +
cierres/mes)"; `BuildUnitEconomicsProfile` ya divide esto entre los
cierres reales del CRM, `closes_per_month`, para `sales_cost_per_close` --
esta tabla solo guarda el mensual, el dueno nunca teclea el cociente) y
devolucion/impago (nullable: sin ella, `BuildUnitEconomicsProfile` cae al
10% documentado en §10.4). `theta`/`margin_horizon_days` NO son entrada del
dueno en este ticket (Assumptions de profitability-engine.md: "theta 0,35 y
suelo 0,20; margen a 90 dias" -- el adaptador SQL los aplica por defecto).

`payment_plan` se persiste para uso futuro (Assumption documentada, T131 no
llega a "porcentaje cobrado a 90/180 dias" -- `BuildUnitEconomicsProfile`
sigue fijando `collection_rate=Rate.one()` sin tocar ese caso de uso, fuera
de alcance de este ticket).

FK compuesta `(offering_id, business_id) -> offerings (id, business_id)`
(mismo patron que `ad_entities`/`signals` sobre `(business_id, entity_ref)`):
una fila de `offering_economics` nunca puede apuntar a un `offering_id` de
OTRO negocio, invariante de IDOR a nivel de esquema, no solo de aplicacion.

`conversion_webhook_tokens`: un token activo por negocio (regenerar
invalida el anterior, mismo criterio UX que el emparejamiento de
Telegram); solo se persiste el hash (sha256 hex) -- el crudo se muestra
una vez en el panel y nunca se guarda.

Revision ID: 0029_economics_inputs
Revises: 0028_credential_health
Create Date: 2026-09-10

Nota (tech-lead, coordinacion entre carriles): el hueco `0029` esta
reservado para `0029_platform_app_credentials` (carril `app-credentials-
ui`), que no existe todavia en este arbol de trabajo -- encadena tras
`0028_credential_health` a proposito. Si `0029_platform_app_credentials`
aterriza en `main` antes que este fichero, quien lo fusione en segundo
lugar rechaina `down_revision` a `"0029_platform_app_credentials"` (mismo
criterio que el rechain documentado en `0016_economics`).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0029_economics_inputs"
down_revision: str | None = "0028_credential_health"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    _upgrade_offering_economics()
    _upgrade_conversion_webhook_tokens()


def downgrade() -> None:
    _downgrade_conversion_webhook_tokens()
    _downgrade_offering_economics()


def _upgrade_offering_economics() -> None:
    # UNIQUE compuesta sobre `offerings` para que la FK de abajo pueda
    # referenciar `(id, business_id)`: `id` ya es PK (unica por si sola),
    # esta segunda UNIQUE es puramente el ancla de la FK compuesta.
    op.execute("""
        ALTER TABLE offerings
            ADD CONSTRAINT offerings_id_business_id_unique UNIQUE (id, business_id)
    """)
    op.execute("""
        CREATE TABLE offering_economics (
            offering_id         UUID PRIMARY KEY,
            business_id         UUID NOT NULL REFERENCES businesses (id) ON DELETE RESTRICT,
            vat_rate_pct        NUMERIC(5, 2) NOT NULL
                CHECK (vat_rate_pct >= 0 AND vat_rate_pct <= 100),
            delivery_cost_minor BIGINT NOT NULL CHECK (delivery_cost_minor >= 0),
            sales_cost_minor    BIGINT NOT NULL CHECK (sales_cost_minor >= 0),
            refund_rate_pct     NUMERIC(5, 2)
                CHECK (refund_rate_pct IS NULL
                       OR (refund_rate_pct >= 0 AND refund_rate_pct <= 100)),
            payment_plan        TEXT NOT NULL DEFAULT 'none'
                CHECK (payment_plan IN ('none', 'instalments')),
            currency            TEXT NOT NULL DEFAULT 'EUR' CHECK (char_length(currency) = 3),
            created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT offering_economics_offering_business_fkey
                FOREIGN KEY (offering_id, business_id)
                REFERENCES offerings (id, business_id) ON DELETE CASCADE
        )
    """)
    op.execute("""
        CREATE TRIGGER offering_economics_set_updated_at
        BEFORE UPDATE ON offering_economics FOR EACH ROW EXECUTE FUNCTION set_updated_at()
    """)


def _downgrade_offering_economics() -> None:
    op.execute("DROP TRIGGER offering_economics_set_updated_at ON offering_economics")
    op.execute("DROP TABLE offering_economics")
    op.execute("ALTER TABLE offerings DROP CONSTRAINT offerings_id_business_id_unique")


def _upgrade_conversion_webhook_tokens() -> None:
    op.execute("""
        CREATE TABLE conversion_webhook_tokens (
            id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            business_id  UUID NOT NULL UNIQUE REFERENCES businesses (id) ON DELETE CASCADE,
            token_hash   TEXT NOT NULL UNIQUE,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)


def _downgrade_conversion_webhook_tokens() -> None:
    op.execute("DROP TABLE conversion_webhook_tokens")
