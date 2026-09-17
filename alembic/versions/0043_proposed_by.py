"""`proposals.proposed_by`: procedencia de una propuesta (004 tasks.md A8).

`person:<user_id>` cuando una llamada MCP con puesto de anuncios crea la
propuesta, `NULL` cuando la crea el motor de reglas o el dueno desde el
panel. Dato de procedencia, no de autorizacion: no participa en
`diff_hash` ni en la firma de aprobacion (`proposals/domain/authorization.py`).

Expand only: columna nueva NULLABLE, sin backfill, sin tocar el trigger de
`diff_hash` (0008) ni el de maquina de estados. Un despliegue anterior
sigue arrancando contra el esquema nuevo.
"""

from alembic import op

revision = "0043_proposed_by"
down_revision = "0042_campaign_packages"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE proposals ADD COLUMN proposed_by TEXT")
    op.execute(
        "CREATE INDEX ix_proposals_proposed_by ON proposals (business_id, proposed_by) "
        "WHERE proposed_by IS NOT NULL"
    )


def downgrade() -> None:
    # No borrar procedencia en silencio (mismo guardian que 0041/0042):
    # con una fila que ya sabe quien la propuso, la bajada se para y lo dice.
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM proposals WHERE proposed_by IS NOT NULL) THEN
            RAISE EXCEPTION 'proposals_proposed_by_not_empty';
        END IF;
    END $$""")
    op.execute("DROP INDEX ix_proposals_proposed_by")
    op.execute("ALTER TABLE proposals DROP COLUMN proposed_by")
