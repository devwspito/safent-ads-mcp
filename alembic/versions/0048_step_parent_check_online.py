"""M4 (revision de codigo): `0047_activate_step_parent` reescribe
`campaign_package_steps_parent_check` con un `DROP CONSTRAINT` + `ADD
CONSTRAINT ... CHECK (...)` liso -- valida TODAS las filas existentes
dentro del propio `ALTER TABLE`, bajo un `ACCESS EXCLUSIVE` que no se
libera hasta terminar. `0042_campaign_packages` ya fijo la convencion
correcta para esto (`_replace_kind_checks`, `approvals_kind_check`/
`approvals_rule_kind_check`): `NOT VALID` primero (no escanea nada, el
`ACCESS EXCLUSIVE` es solo de catalogo) y `VALIDATE CONSTRAINT` aparte
(escanea las filas, pero con un lock mas ligero -- `SHARE UPDATE
EXCLUSIVE`, que no bloquea lecturas ni escrituras concurrentes).

Migracion NUEVA en vez de editar `0047` in situ: `0047` ya esta mergeado
en `lane/003-paquete-de-campana` (pudo haber corrido ya en un Postgres
compartido de desarrollo/CI) -- reescribir un `upgrade()` ya aplicado
desincroniza `alembic_version` de quien ya lo corrio. El CHECK resultante
es identico byte a byte al que deja `0047`; solo cambia COMO se aplica."""

from alembic import op

revision = "0048_step_parent_check_online"
down_revision = "0047_activate_step_parent"
branch_labels = None
depends_on = None

_CONSTRAINT_NAME = "campaign_package_steps_parent_check"
_PARENTED_KINDS = "'create_ad_set','create_ad','activate_campaign'"
_CHECK_BODY = f"CHECK ((kind IN ({_PARENTED_KINDS})) = (parent_local_ref IS NOT NULL))"


def upgrade() -> None:
    op.execute(f"ALTER TABLE campaign_package_steps DROP CONSTRAINT {_CONSTRAINT_NAME}")
    op.execute(
        f"ALTER TABLE campaign_package_steps ADD CONSTRAINT {_CONSTRAINT_NAME} "
        f"{_CHECK_BODY} NOT VALID"
    )
    op.execute(f"ALTER TABLE campaign_package_steps VALIDATE CONSTRAINT {_CONSTRAINT_NAME}")


def downgrade() -> None:
    # Restaura el estado exacto que dejaba `0047_activate_step_parent`
    # (mismo CHECK, anadido en un solo paso bloqueante) para que la
    # cadena de `downgrade` de esa migracion siga funcionando sin cambios.
    op.execute(f"ALTER TABLE campaign_package_steps DROP CONSTRAINT {_CONSTRAINT_NAME}")
    op.execute(
        f"ALTER TABLE campaign_package_steps ADD CONSTRAINT {_CONSTRAINT_NAME} {_CHECK_BODY}"
    )
