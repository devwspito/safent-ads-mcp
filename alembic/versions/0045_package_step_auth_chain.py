"""Chains a `package_step` authorization to the human one it derives from.

`003-paquete-de-campana` data-model.md "Veredicto T002" (recomendacion no
implementada, ahora adoptada por decision del integrador): en vez de
relajar `approvals_kind_matches_classification` (0008) para dejar pasar
`package_step` sin condiciones -- lo que debilitaria "lo importante lo
firma una persona" -- se anade una columna aditiva que ata cada paso a la
`human_approval` de sujeto `package` de la que deriva, y el guarda solo
admite `package_step` cuando esa cadena existe y sigue viva.

Expand only: ninguna fila existente cambia de forma. Tambien anade
`upload_creative` al vocabulario de `campaign_package_steps.kind`
(data-model.md Revision 2 §R2.10.3), que 0042 no cubria porque el paso de
subida de creatividad (BL-6) llego despues de esa migracion.
"""

from alembic import op

revision = "0045_package_step_auth_chain"
down_revision = "0044_package_approval_envelope"
branch_labels = None
depends_on = None

_STEP_KINDS = "'upload_creative','create_campaign','create_ad_set','create_ad','activate_campaign'"


def upgrade() -> None:
    op.execute("""
        ALTER TABLE approvals
        ADD COLUMN derived_from_authorization_id UUID REFERENCES approvals (id) ON DELETE RESTRICT
    """)
    op.execute("""
        ALTER TABLE approvals
        ADD CONSTRAINT approvals_package_step_chain_check
            CHECK ((kind = 'package_step') = (derived_from_authorization_id IS NOT NULL))
    """)

    # `CREATE OR REPLACE`: mismo nombre de funcion y disparador que 0008, sin
    # tocar el trigger. La condicion "importante exige human_approval" NO se
    # relaja -- gana una segunda forma de cumplirla: una cadena viva hasta
    # una `human_approval` de sujeto `package` con decision `approved`.
    op.execute("""
        CREATE OR REPLACE FUNCTION approvals_kind_matches_classification() RETURNS TRIGGER AS $$
        DECLARE
            proposal_classification TEXT;
            chained_to_live_human_approval BOOLEAN;
        BEGIN
            SELECT classification INTO proposal_classification
              FROM proposals WHERE id = NEW.proposal_id;
            IF proposal_classification IN ('important', 'critical')
               AND NEW.decision = 'approved'
               AND NEW.kind <> 'human_approval' THEN
                IF NEW.kind = 'package_step' AND NEW.derived_from_authorization_id IS NOT NULL THEN
                    SELECT EXISTS (
                        SELECT 1 FROM approvals AS human
                         WHERE human.id = NEW.derived_from_authorization_id
                           AND human.kind = 'human_approval'
                           AND human.decision = 'approved'
                           AND human.subject_kind = 'package'
                    ) INTO chained_to_live_human_approval;
                ELSE
                    chained_to_live_human_approval := FALSE;
                END IF;
                IF NOT chained_to_live_human_approval THEN
                    RAISE EXCEPTION
                        'approvals: una propuesta % exige human_approval (o un package_step '
                        'encadenado a una human_approval de paquete viva)', proposal_classification;
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)

    op.execute(
        "ALTER TABLE campaign_package_steps DROP CONSTRAINT campaign_package_steps_kind_check"
    )
    op.execute(f"""
        ALTER TABLE campaign_package_steps
        ADD CONSTRAINT campaign_package_steps_kind_check CHECK (kind IN ({_STEP_KINDS}))
    """)


def downgrade() -> None:
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM approvals WHERE derived_from_authorization_id IS NOT NULL) THEN
            RAISE EXCEPTION 'approvals_package_step_chain_history_present';
        END IF;
        IF EXISTS (SELECT 1 FROM campaign_package_steps WHERE kind = 'upload_creative') THEN
            RAISE EXCEPTION 'campaign_package_steps_upload_creative_history_present';
        END IF;
    END $$""")

    op.execute(
        "ALTER TABLE campaign_package_steps DROP CONSTRAINT campaign_package_steps_kind_check"
    )
    op.execute("""
        ALTER TABLE campaign_package_steps
        ADD CONSTRAINT campaign_package_steps_kind_check
            CHECK (kind IN ('create_campaign','create_ad_set','create_ad','activate_campaign'))
    """)

    op.execute("""
        CREATE OR REPLACE FUNCTION approvals_kind_matches_classification() RETURNS TRIGGER AS $$
        DECLARE
            proposal_classification TEXT;
        BEGIN
            SELECT classification INTO proposal_classification
              FROM proposals WHERE id = NEW.proposal_id;
            IF proposal_classification IN ('important', 'critical')
               AND NEW.kind <> 'human_approval'
               AND NEW.decision = 'approved' THEN
                RAISE EXCEPTION
                    'approvals: una propuesta % exige human_approval', proposal_classification;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)

    op.execute("ALTER TABLE approvals DROP CONSTRAINT approvals_package_step_chain_check")
    op.execute("ALTER TABLE approvals DROP COLUMN derived_from_authorization_id")
