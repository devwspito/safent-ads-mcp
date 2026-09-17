"""`campaign_package_steps_parent_check` (0042) solo dejaba a
`create_ad_set`/`create_ad` llevar `parent_local_ref` -- pero
`derive_step_plan` (`packages/domain/approval_envelope.py`) SIEMPRE le da
uno tambien a `activate_campaign` (el propio `CAMPAIGN_LOCAL_REF`): es como
`RunPackagePublication._resolve_parent` resuelve que `entity_ref` activar
(la campaña ya confirmada por `CREATE_CAMPAIGN`), no un dato estructural
del arbol de anuncios que solo aplique a `create_ad_set`/`create_ad`.

Sin este ajuste, el ULTIMO paso de CUALQUIER publicacion (activar la
campaña) nunca podia ni siquiera nacer `pending` -- ninguna publicacion
real podia completarse jamas, descubierto por
`tests/integration/packages/test_publication_saga.py` contra Postgres de
verdad (los tests unitarios de T023/T024 usan repositorios en memoria, que
no aplican este CHECK)."""

from alembic import op

revision = "0047_activate_step_parent"
down_revision = "0046_execution_package_steps"
branch_labels = None
depends_on = None

_OLD_PARENTED_KINDS = "'create_ad_set','create_ad'"
_NEW_PARENTED_KINDS = "'create_ad_set','create_ad','activate_campaign'"


def upgrade() -> None:
    op.execute(
        "ALTER TABLE campaign_package_steps DROP CONSTRAINT campaign_package_steps_parent_check"
    )
    op.execute(f"""
        ALTER TABLE campaign_package_steps
        ADD CONSTRAINT campaign_package_steps_parent_check
            CHECK ((kind IN ({_NEW_PARENTED_KINDS})) = (parent_local_ref IS NOT NULL))
    """)


def downgrade() -> None:
    op.execute("""DO $$ BEGIN
        IF EXISTS (
            SELECT 1 FROM campaign_package_steps
             WHERE kind = 'activate_campaign' AND parent_local_ref IS NOT NULL
        ) THEN
            RAISE EXCEPTION 'campaign_package_steps_activate_parent_present';
        END IF;
    END $$""")
    op.execute(
        "ALTER TABLE campaign_package_steps DROP CONSTRAINT campaign_package_steps_parent_check"
    )
    op.execute(f"""
        ALTER TABLE campaign_package_steps
        ADD CONSTRAINT campaign_package_steps_parent_check
            CHECK ((kind IN ({_OLD_PARENTED_KINDS})) = (parent_local_ref IS NOT NULL))
    """)
