"""Sobre de aprobacion del paquete y sujeto de autorizacion (expand only).

`003-paquete-de-campana` data-model.md "Revision 2" §R2.2 (BL-2) y §R2.5
(BL-7), traspasadas a esta migracion porque T105/el resto del alcance
ampliado de T003 (`database-engineer`) no habian aterrizado todavia cuando
`ApproveCampaignPackage` (T022) los necesito para poder persistir de
verdad -- gap descubierto en esta rama, documentado en el informe de la
tarea; `database-engineer` deberia revisar esta migracion en el proximo
repaso del contrato.

1. `campaign_packages.publish_as` (JSONB NULL): la pagina de Meta ya
   conectada desde la que se publica (R2.1) -- ausente en la migracion
   0042 original, que solo cubria data-model.md antes de la Revision 2.
2. `campaign_package_publications` gana el sobre firmado ENTERO
   (`approval_envelope`), su firma (`approval_signature`), su huella
   (`envelope_hash`), su caducidad (`approval_expires_at`) y el arbol
   archivado inmutable al aprobar (`approved_plan`, cierra B-R1). Todas
   `NOT NULL` sin `DEFAULT`: Postgres lo permite sobre una tabla vacia
   (todavia no hay ninguna publicacion en esta rama) sin comprobar filas
   existentes.
3. `approvals.proposal_id` pasa a admitir `NULL` y gana `subject_kind`/
   `subject_id` con una CHECK de exclusividad -- una autorizacion de
   paquete (`subject_kind='package'`) nunca mete su `package_id` en
   `proposal_id` (R2.5). Compatible con filas existentes: todas tienen
   `proposal_id` no nulo y `subject_id` nulo, que sigue satisfaciendo la
   exclusividad.
"""

from alembic import op

revision = "0044_package_approval_envelope"
down_revision = "0043_proposed_by"
branch_labels = None
depends_on = None

_SHA256_HEX = "^[a-f0-9]{64}$"


def upgrade() -> None:
    op.execute("""
        ALTER TABLE campaign_packages
        ADD COLUMN publish_as JSONB
            CHECK (publish_as IS NULL OR jsonb_typeof(publish_as) = 'object')
    """)

    op.execute("""
        ALTER TABLE campaign_package_publications
        ADD COLUMN approval_envelope JSONB NOT NULL
            CHECK (jsonb_typeof(approval_envelope) = 'object'),
        ADD COLUMN approval_signature BYTEA NOT NULL,
        ADD COLUMN envelope_hash TEXT NOT NULL,
        ADD COLUMN approval_expires_at TIMESTAMPTZ NOT NULL,
        ADD COLUMN approved_plan JSONB NOT NULL
            CHECK (jsonb_typeof(approved_plan) = 'object')
    """)
    op.execute(f"""
        ALTER TABLE campaign_package_publications
        ADD CONSTRAINT campaign_package_publications_envelope_hash_check
            CHECK (envelope_hash ~ '{_SHA256_HEX}')
    """)
    op.execute("""
        ALTER TABLE campaign_package_publications
        ADD CONSTRAINT campaign_package_publications_expiry_check
            CHECK (approval_expires_at > started_at)
    """)

    op.execute("ALTER TABLE approvals ALTER COLUMN proposal_id DROP NOT NULL")
    op.execute("""
        ALTER TABLE approvals
        ADD COLUMN subject_kind TEXT CHECK (subject_kind IN ('proposal', 'package')),
        ADD COLUMN subject_id TEXT CHECK (char_length(subject_id) BETWEEN 1 AND 128)
    """)
    op.execute("""
        ALTER TABLE approvals
        ADD CONSTRAINT approvals_subject_exclusive_check
            CHECK ((proposal_id IS NULL) <> (subject_id IS NULL))
    """)


def downgrade() -> None:
    # Ninguna fila de esta historia se borra en silencio (mismo criterio que
    # 0042/0043): si ya hay un sujeto de paquete o una publicacion con sobre
    # firmado, la vuelta atras se para y se ve.
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM approvals WHERE subject_id IS NOT NULL) THEN
            RAISE EXCEPTION 'approvals_package_subject_history_present';
        END IF;
        IF EXISTS (SELECT 1 FROM campaign_package_publications) THEN
            RAISE EXCEPTION 'campaign_package_publications_not_empty';
        END IF;
        IF EXISTS (SELECT 1 FROM campaign_packages WHERE publish_as IS NOT NULL) THEN
            RAISE EXCEPTION 'campaign_packages_publish_as_present';
        END IF;
    END $$""")

    op.execute("ALTER TABLE approvals DROP CONSTRAINT approvals_subject_exclusive_check")
    op.execute("ALTER TABLE approvals DROP COLUMN subject_id")
    op.execute("ALTER TABLE approvals DROP COLUMN subject_kind")
    op.execute("ALTER TABLE approvals ALTER COLUMN proposal_id SET NOT NULL")

    op.execute("""
        ALTER TABLE campaign_package_publications
        DROP CONSTRAINT campaign_package_publications_expiry_check
    """)
    op.execute("""
        ALTER TABLE campaign_package_publications
        DROP CONSTRAINT campaign_package_publications_envelope_hash_check
    """)
    op.execute("""
        ALTER TABLE campaign_package_publications
        DROP COLUMN approved_plan,
        DROP COLUMN approval_expires_at,
        DROP COLUMN envelope_hash,
        DROP COLUMN approval_signature,
        DROP COLUMN approval_envelope
    """)

    op.execute("ALTER TABLE campaign_packages DROP COLUMN publish_as")
