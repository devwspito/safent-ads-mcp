"""Campaign package tree, its single publication saga and every write step.

Expand only: nothing existing changes shape. `approvals` gains the
`package_step` kind in its two CHECK vocabularies (data-model.md §Plan de
migracion 6) -- the column is TEXT + CHECK, not an ENUM, so no type ever
needs rewriting. The classification and managed-binding triggers that still
demand `human_approval` are NOT relaxed here: that is a security posture
call and belongs to the threat model (T001).
"""

from alembic import op

revision = "0042_campaign_packages"
down_revision = "0041_campaign_drafts"
branch_labels = None
depends_on = None

_ULID = "^[0-9A-HJKMNP-TV-Z]{26}$"
_SHA256_HEX = "^[a-f0-9]{64}$"
_PACKAGE_STATES = (
    "'draft','proposed','approved','publishing','verifying','published',"
    "'partially_published','failed','rejected','expired','invalidated'"
)
_PUBLICATION_STATES = "'pending','running','completed','halted'"
_STEP_KINDS = "'create_campaign','create_ad_set','create_ad','activate_campaign'"
_STEP_STATES = "'pending','running','done','failed','unknown','blocked'"
_PARENTED_KINDS = "'create_ad_set','create_ad'"

# Un solo salto por transicion, en el espiritu de `proposals_guard_diff_hash`
# (0008): el estado intermedio se persiste, nunca se salta.
_PACKAGE_TRANSITIONS = """CASE OLD.state
        WHEN 'draft'      THEN ARRAY['proposed','rejected','expired']
        WHEN 'proposed'   THEN ARRAY['approved','rejected','expired']
        WHEN 'approved'   THEN ARRAY['publishing','invalidated','rejected']
        WHEN 'publishing' THEN ARRAY['published','partially_published','verifying','failed']
        WHEN 'verifying'  THEN ARRAY['published','partially_published']
        WHEN 'partially_published' THEN ARRAY['publishing']
        WHEN 'failed'     THEN ARRAY['proposed']
        ELSE ARRAY[]::TEXT[]
    END"""
_PUBLICATION_TRANSITIONS = """CASE OLD.state
        WHEN 'pending' THEN ARRAY['running','halted']
        WHEN 'running' THEN ARRAY['completed','halted']
        WHEN 'halted'  THEN ARRAY['running']
        ELSE ARRAY[]::TEXT[]
    END"""
# `done` no aparece: un paso hecho no se repite (PackagePublication inv. 3).
_STEP_TRANSITIONS = """CASE OLD.state
        WHEN 'pending' THEN ARRAY['running','blocked','failed']
        WHEN 'running' THEN ARRAY['done','failed','unknown']
        WHEN 'unknown' THEN ARRAY['done','failed','blocked']
        WHEN 'blocked' THEN ARRAY['running','failed']
        WHEN 'failed'  THEN ARRAY['pending','running']
        ELSE ARRAY[]::TEXT[]
    END"""

_KIND_CHECK = "CHECK (kind IN ('human_approval','rule_authorization'{extra}))"
_RULE_KIND_CHECK = """CHECK (
        (kind = 'rule_authorization' AND rule_id IS NOT NULL)
        OR (kind IN ('human_approval'{extra}) AND rule_id IS NULL)
    )"""


def _approvals_kind_vocabulary(extra: str) -> None:
    """Reescribe las dos CHECK de `kind` de `approvals`.

    `NOT VALID` + `VALIDATE`: la ventana de ACCESS EXCLUSIVE es solo de
    catalogo y el escaneo de validacion no bloquea escrituras. `approvals`
    es solo-anexable por trigger, pero los triggers de fila no miran DDL."""
    for name, template in (
        ("approvals_kind_check", _KIND_CHECK),
        ("approvals_rule_kind_check", _RULE_KIND_CHECK),
    ):
        body = template.format(extra=extra)
        op.execute(f"ALTER TABLE approvals DROP CONSTRAINT {name}")
        op.execute(f"ALTER TABLE approvals ADD CONSTRAINT {name} {body} NOT VALID")
        op.execute(f"ALTER TABLE approvals VALIDATE CONSTRAINT {name}")


def upgrade() -> None:
    _approvals_kind_vocabulary(",'package_step'")

    # Claves compuestas para que un paquete no pueda apuntar a la cuenta ni a
    # la oferta de OTRO negocio: la pertenencia se comprueba en la base, no
    # en la aplicacion (mismo patron que `(business_id, entity_ref)` en 0008).
    op.execute(
        "ALTER TABLE platform_accounts ADD CONSTRAINT platform_accounts_business_scope_unique "
        "UNIQUE (business_id, account_ref)"
    )
    op.execute(
        "ALTER TABLE offerings ADD CONSTRAINT offerings_business_scope_unique "
        "UNIQUE (business_id, id)"
    )

    op.execute(f"""
        CREATE TABLE campaign_packages (
            id               TEXT PRIMARY KEY CHECK (id ~ '{_ULID}'),
            business_id      UUID NOT NULL REFERENCES businesses (id) ON DELETE RESTRICT,
            platform         TEXT NOT NULL CHECK (platform IN ('google', 'meta')),
            account_ref      TEXT NOT NULL,
            offering_id      UUID NOT NULL,
            package_group_id TEXT CHECK (package_group_id ~ '{_ULID}'),
            state            TEXT NOT NULL DEFAULT 'draft'
                               CHECK (state IN ({_PACKAGE_STATES})),
            package_hash     TEXT NOT NULL CHECK (package_hash ~ '{_SHA256_HEX}'),
            plan             JSONB NOT NULL CHECK (jsonb_typeof(plan) = 'object'),
            budget           JSONB NOT NULL CHECK (jsonb_typeof(budget) = 'object'),
            rationale        JSONB NOT NULL CHECK (jsonb_typeof(rationale) = 'object'),
            research         JSONB CHECK (jsonb_typeof(research) = 'object'),
            owner_context    TEXT CHECK (char_length(owner_context) BETWEEN 1 AND 500),
            created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at       TIMESTAMPTZ NOT NULL,
            updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),

            CONSTRAINT campaign_packages_expiry_check CHECK (expires_at > created_at),
            CONSTRAINT campaign_packages_account_fk
                FOREIGN KEY (business_id, account_ref)
                REFERENCES platform_accounts (business_id, account_ref) ON DELETE RESTRICT,
            CONSTRAINT campaign_packages_offering_fk
                FOREIGN KEY (business_id, offering_id)
                REFERENCES offerings (business_id, id) ON DELETE RESTRICT
        )
    """)
    # Bandeja y listado: GET /packages?business_id&state=...&limit&cursor
    # (paginacion por clave sobre created_at).
    op.execute("""
        CREATE INDEX ix_campaign_packages_business_state_created
        ON campaign_packages (business_id, state, created_at DESC)
    """)
    # FR-20: un solo paquete abierto por (negocio, cuenta, oferta). Es tambien
    # el indice que sirve la comprobacion de deduplicacion al proponer.
    op.execute("""
        CREATE UNIQUE INDEX ix_campaign_packages_open_dedup
        ON campaign_packages (business_id, account_ref, offering_id)
        WHERE state IN ('draft', 'proposed')
    """)
    # La FK a `offerings` es RESTRICT: sin indice, borrar una oferta obliga a
    # recorrer la tabla entera.
    op.execute("CREATE INDEX ix_campaign_packages_offering ON campaign_packages (offering_id)")

    op.execute(f"""
        CREATE TABLE campaign_package_publications (
            id                TEXT PRIMARY KEY CHECK (id ~ '{_ULID}'),
            package_id        TEXT NOT NULL UNIQUE
                                REFERENCES campaign_packages (id) ON DELETE RESTRICT,
            authorization_id  UUID NOT NULL REFERENCES approvals (id) ON DELETE RESTRICT,
            state             TEXT NOT NULL DEFAULT 'pending'
                                CHECK (state IN ({_PUBLICATION_STATES})),
            cursor            INTEGER NOT NULL DEFAULT 0 CHECK (cursor >= 0),
            halt_reason       TEXT CHECK (char_length(halt_reason) BETWEEN 1 AND 200),
            failed_step_index INTEGER CHECK (failed_step_index >= 0),
            started_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
            finished_at       TIMESTAMPTZ,
            created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

            CONSTRAINT campaign_package_publications_halt_check
                CHECK ((state = 'halted') = (halt_reason IS NOT NULL)),
            CONSTRAINT campaign_package_publications_failed_step_check
                CHECK (failed_step_index IS NULL OR state = 'halted'),
            CONSTRAINT campaign_package_publications_finished_check
                CHECK (finished_at IS NULL
                       OR (state IN ('completed', 'halted') AND finished_at >= started_at))
        )
    """)
    # Bucle de `ads-worker`: la siguiente publicacion que pide un paso.
    op.execute("""
        CREATE INDEX ix_campaign_package_publications_live
        ON campaign_package_publications (started_at)
        WHERE state IN ('pending', 'running')
    """)

    op.execute(f"""
        CREATE TABLE campaign_package_steps (
            publication_id     TEXT NOT NULL
                                 REFERENCES campaign_package_publications (id) ON DELETE CASCADE,
            step_index         INTEGER NOT NULL CHECK (step_index >= 0),
            kind               TEXT NOT NULL CHECK (kind IN ({_STEP_KINDS})),
            local_ref          TEXT NOT NULL CHECK (char_length(local_ref) BETWEEN 1 AND 64),
            parent_local_ref   TEXT CHECK (char_length(parent_local_ref) BETWEEN 1 AND 64),
            state              TEXT NOT NULL DEFAULT 'pending'
                                 CHECK (state IN ({_STEP_STATES})),
            created_entity_ref TEXT,
            proposal_id        UUID REFERENCES proposals (id) ON DELETE RESTRICT,
            execution_id       UUID REFERENCES executions (id) ON DELETE RESTRICT,
            outcome_code       TEXT CHECK (char_length(outcome_code) BETWEEN 1 AND 64),
            created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),

            CONSTRAINT campaign_package_steps_pkey PRIMARY KEY (publication_id, step_index),
            CONSTRAINT campaign_package_steps_parent_check
                CHECK ((kind IN ({_PARENTED_KINDS})) = (parent_local_ref IS NOT NULL)),
            -- `created_entity_ref` solo con recibo confirmado, y activar no crea nada.
            CONSTRAINT campaign_package_steps_receipt_check
                CHECK (created_entity_ref IS NULL
                       OR (state = 'done' AND kind <> 'activate_campaign'))
        )
    """)
    op.execute("""
        CREATE INDEX ix_campaign_package_steps_publication_state
        ON campaign_package_steps (publication_id, state)
    """)
    # Invariante 1: solo un paso en vuelo por publicacion, garantizado por la
    # base y no por el orden en que corran los trabajadores.
    op.execute("""
        CREATE UNIQUE INDEX ix_campaign_package_steps_single_running
        ON campaign_package_steps (publication_id)
        WHERE state = 'running'
    """)
    # La FK a `executions` es RESTRICT y la reconciliacion busca por ejecucion.
    op.execute("""
        CREATE INDEX ix_campaign_package_steps_execution
        ON campaign_package_steps (execution_id) WHERE execution_id IS NOT NULL
    """)
    op.execute("""
        CREATE INDEX ix_campaign_package_steps_proposal
        ON campaign_package_steps (proposal_id) WHERE proposal_id IS NOT NULL
    """)

    op.execute(f"""
        CREATE FUNCTION campaign_packages_guard() RETURNS TRIGGER AS $$
        DECLARE
            allowed TEXT[];
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.state NOT IN ('draft', 'proposed') THEN
                    RAISE EXCEPTION 'campaign_packages: un paquete nace draft o proposed, no %',
                        NEW.state;
                END IF;
                RETURN NEW;
            END IF;

            IF (NEW.business_id, NEW.platform, NEW.account_ref, NEW.offering_id)
               IS DISTINCT FROM
               (OLD.business_id, OLD.platform, OLD.account_ref, OLD.offering_id) THEN
                RAISE EXCEPTION 'campaign_packages: la pertenencia del paquete es inmutable';
            END IF;

            -- Invariante 8: si cambia el arbol declarado, la huella rota. Sin
            -- esto una firma vieja seguiria valiendo para otro contenido.
            IF NEW.plan IS DISTINCT FROM OLD.plan AND NEW.package_hash = OLD.package_hash THEN
                RAISE EXCEPTION
                    'campaign_packages: cambiar el plan obliga a rotar el package_hash';
            END IF;

            IF NEW.state <> OLD.state THEN
                allowed := {_PACKAGE_TRANSITIONS};
                IF NOT (NEW.state = ANY (allowed)) THEN
                    RAISE EXCEPTION 'campaign_packages: transicion % -> % no permitida',
                        OLD.state, NEW.state;
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER campaign_packages_guard
        BEFORE INSERT OR UPDATE ON campaign_packages
        FOR EACH ROW EXECUTE FUNCTION campaign_packages_guard()
    """)
    op.execute("""
        CREATE TRIGGER campaign_packages_set_updated_at
        BEFORE UPDATE ON campaign_packages FOR EACH ROW EXECUTE FUNCTION set_updated_at()
    """)

    op.execute(f"""
        CREATE FUNCTION campaign_package_publications_guard() RETURNS TRIGGER AS $$
        DECLARE
            allowed TEXT[];
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.state <> 'pending' THEN
                    RAISE EXCEPTION 'campaign_package_publications: una publicacion nace pending';
                END IF;
                RETURN NEW;
            END IF;

            -- Invariante 5: todos los pasos derivan de la MISMA firma humana.
            IF (NEW.package_id, NEW.authorization_id)
               IS DISTINCT FROM (OLD.package_id, OLD.authorization_id) THEN
                RAISE EXCEPTION
                    'campaign_package_publications: paquete y autorizacion son inmutables';
            END IF;
            IF NEW.cursor < OLD.cursor THEN
                RAISE EXCEPTION 'campaign_package_publications: el cursor no retrocede';
            END IF;

            IF NEW.state <> OLD.state THEN
                allowed := {_PUBLICATION_TRANSITIONS};
                IF NOT (NEW.state = ANY (allowed)) THEN
                    RAISE EXCEPTION
                        'campaign_package_publications: transicion % -> % no permitida',
                        OLD.state, NEW.state;
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER campaign_package_publications_guard
        BEFORE INSERT OR UPDATE ON campaign_package_publications
        FOR EACH ROW EXECUTE FUNCTION campaign_package_publications_guard()
    """)
    op.execute("""
        CREATE TRIGGER campaign_package_publications_set_updated_at
        BEFORE UPDATE ON campaign_package_publications
        FOR EACH ROW EXECUTE FUNCTION set_updated_at()
    """)

    op.execute(f"""
        CREATE FUNCTION campaign_package_steps_guard() RETURNS TRIGGER AS $$
        DECLARE
            allowed TEXT[];
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.state <> 'pending' THEN
                    RAISE EXCEPTION 'campaign_package_steps: un paso nace pending, no %', NEW.state;
                END IF;
            ELSE
                IF (NEW.publication_id, NEW.step_index, NEW.kind, NEW.local_ref,
                    NEW.parent_local_ref)
                   IS DISTINCT FROM
                   (OLD.publication_id, OLD.step_index, OLD.kind, OLD.local_ref,
                    OLD.parent_local_ref) THEN
                    RAISE EXCEPTION 'campaign_package_steps: el plan de pasos es inmutable';
                END IF;
                IF OLD.created_entity_ref IS NOT NULL
                   AND NEW.created_entity_ref IS DISTINCT FROM OLD.created_entity_ref THEN
                    RAISE EXCEPTION 'campaign_package_steps: el recibo confirmado es inmutable';
                END IF;
                IF NEW.state <> OLD.state THEN
                    allowed := {_STEP_TRANSITIONS};
                    IF NOT (NEW.state = ANY (allowed)) THEN
                        RAISE EXCEPTION 'campaign_package_steps: transicion % -> % no permitida',
                            OLD.state, NEW.state;
                    END IF;
                END IF;
            END IF;

            -- Invariantes 1 y 4: en orden, y un paso `unknown` detiene el avance
            -- (el anterior no esta `done`, luego el siguiente no puede correr).
            IF NEW.state = 'running' AND EXISTS (
                SELECT 1 FROM campaign_package_steps
                 WHERE publication_id = NEW.publication_id
                   AND step_index < NEW.step_index AND state <> 'done') THEN
                RAISE EXCEPTION 'campaign_package_steps: hay pasos anteriores sin terminar';
            END IF;

            -- Invariante 2: activar la campana es siempre el ultimo paso.
            IF NEW.kind = 'activate_campaign' AND EXISTS (
                SELECT 1 FROM campaign_package_steps
                 WHERE publication_id = NEW.publication_id AND step_index > NEW.step_index) THEN
                RAISE EXCEPTION 'campaign_package_steps: activar es siempre el ultimo paso';
            END IF;
            IF EXISTS (
                SELECT 1 FROM campaign_package_steps
                 WHERE publication_id = NEW.publication_id AND kind = 'activate_campaign'
                   AND step_index < NEW.step_index) THEN
                RAISE EXCEPTION 'campaign_package_steps: activar es siempre el ultimo paso';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)  # noqa: S608 - identificadores fijos de migracion, jamas entrada de cliente
    op.execute("""
        CREATE TRIGGER campaign_package_steps_guard
        BEFORE INSERT OR UPDATE ON campaign_package_steps
        FOR EACH ROW EXECUTE FUNCTION campaign_package_steps_guard()
    """)
    op.execute("""
        CREATE TRIGGER campaign_package_steps_set_updated_at
        BEFORE UPDATE ON campaign_package_steps
        FOR EACH ROW EXECUTE FUNCTION set_updated_at()
    """)


def downgrade() -> None:
    # Ni un paquete ni una firma de paso se borran en silencio: si hay
    # historia, la vuelta atras se para y se ve (0041 hace lo mismo).
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM campaign_packages) THEN
            RAISE EXCEPTION 'campaign_packages_not_empty';
        END IF;
        IF EXISTS (SELECT 1 FROM approvals WHERE kind = 'package_step') THEN
            RAISE EXCEPTION 'approvals_package_step_history_present';
        END IF;
    END $$""")

    op.execute("DROP TABLE campaign_package_steps")
    op.execute("DROP TABLE campaign_package_publications")
    op.execute("DROP TABLE campaign_packages")
    op.execute("DROP FUNCTION campaign_package_steps_guard()")
    op.execute("DROP FUNCTION campaign_package_publications_guard()")
    op.execute("DROP FUNCTION campaign_packages_guard()")
    op.execute("ALTER TABLE offerings DROP CONSTRAINT offerings_business_scope_unique")
    op.execute(
        "ALTER TABLE platform_accounts DROP CONSTRAINT platform_accounts_business_scope_unique"
    )
    _approvals_kind_vocabulary("")
