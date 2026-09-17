"""T123/AL-4 (gap cerrado: 2 xfails estrictos en
`test_chokepoint_step_executor.py`): `expected_state_hash` para
CREATE_AD_SET/CREATE_AD/ACTIVATE_CAMPAIGN nace del RECIBO CONFIRMADO del
paso padre -- nunca de `ad_entities` (el padre lo crea la MISMA saga, no
vive alli todavia) ni de lo que el llamante afirme. Columna aditiva:
`confirmed_state_hash` archiva el `WriteResult.confirmed_state_hash` que
`ChokepointStepExecutor` ya recibe al confirmar cada escritura
(`ExecutionAttempt.platform_state_hash_after`) -- el HIJO la lee del padre
via `RunPackagePublication._resolve_parent` y la firma en
`PackageStepBinding.parent_receipt_state_hash` (data-model.md R2.8,
generalizado de la activacion, unica que lo mencionaba, a todo paso con
padre).

CHECK con `NOT VALID` + `VALIDATE CONSTRAINT` (mismo criterio que
`0048_step_parent_check_online`): separar el `ADD CONSTRAINT` de su
validacion deja el escaneo bajo `SHARE UPDATE EXCLUSIVE` en vez de
`ACCESS EXCLUSIVE` -- no bloquea lecturas/escrituras concurrentes, aunque
aqui la tabla parta de `confirmed_state_hash` siempre NULL (columna nueva).

Vuelta atras honesta: si algun paso ya archivo un `confirmed_state_hash`
real, perderlo en el downgrade borraria historia -- se rechaza, mismo
criterio que `0046_execution_package_steps`."""

from alembic import op

revision = "0049_package_step_state_hash"
down_revision = "0048_step_parent_check_online"
branch_labels = None
depends_on = None

_CONSTRAINT_NAME = "campaign_package_steps_confirmed_state_hash_check"
_CHECK_BODY = (
    "CHECK (confirmed_state_hash IS NULL OR (state = 'done' AND kind <> 'upload_creative'))"
)


def upgrade() -> None:
    op.execute("ALTER TABLE campaign_package_steps ADD COLUMN confirmed_state_hash TEXT")
    op.execute(
        f"ALTER TABLE campaign_package_steps ADD CONSTRAINT {_CONSTRAINT_NAME} "
        f"{_CHECK_BODY} NOT VALID"
    )
    op.execute(f"ALTER TABLE campaign_package_steps VALIDATE CONSTRAINT {_CONSTRAINT_NAME}")


def downgrade() -> None:
    op.execute("""DO $$ BEGIN
        IF EXISTS (
            SELECT 1 FROM campaign_package_steps WHERE confirmed_state_hash IS NOT NULL
        ) THEN
            RAISE EXCEPTION 'campaign_package_steps_confirmed_state_hash_present';
        END IF;
    END $$""")
    op.execute(f"ALTER TABLE campaign_package_steps DROP CONSTRAINT {_CONSTRAINT_NAME}")
    op.execute("ALTER TABLE campaign_package_steps DROP COLUMN confirmed_state_hash")
