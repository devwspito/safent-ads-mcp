"""002b hardening, threat-model.md C-79 (T084): `federated_login_transactions.
ip_address` -- sin ella no hay forma de contar "cuantas transacciones
pendientes abrio esta IP" y el tope de 5 por IP (mismo criterio que
`mcp_oauth.application.policy.MAX_PENDING_PER_CLIENT`) no se puede aplicar.

Columna nullable a proposito: filas escritas ANTES de este despliegue (si
alguna sigue viva, la TTL es de 10 minutos) se quedan sin IP conocida y
sencillamente no cuentan contra el tope de ninguna direccion -- ninguna
comparacion `ip_address = $1` casa con `NULL`.

Sin indice nuevo: la nota de `0052_federated_identity.py` sigue vigente
-- esta tabla vive con un punado de filas (TTL corto, poda periodica), asi
que un barrido secuencial para contar 5 filas es mas barato que mantener un
indice mas.

Revision ID: 0053_federated_transaction_ip
Revises: 0052_federated_identity
Create Date: 2026-09-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0053_federated_transaction_ip"
down_revision: str | None = "0052_federated_identity"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE federated_login_transactions ADD COLUMN ip_address INET")
    op.execute(
        """
        COMMENT ON COLUMN federated_login_transactions.ip_address IS
        'IP que abrio el salto (threat-model.md C-79): sostiene el tope de 5
         transacciones pendientes por IP. NULL en filas de antes de esta
         columna -- no cuentan contra ningun tope.'
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE federated_login_transactions DROP COLUMN ip_address")
