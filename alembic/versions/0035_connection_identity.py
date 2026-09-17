"""ADS-02: scope new identities without rewriting signed historical references."""

from alembic import op

revision = "0035_connection_identity"
down_revision = "0034_execution_reservations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE platform_connections (
        id UUID PRIMARY KEY,
        business_id UUID NOT NULL REFERENCES businesses(id),
        owner_id UUID NOT NULL REFERENCES owners(id),
        platform TEXT NOT NULL CHECK(platform IN ('google','meta')),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE(id,business_id,platform)
    )""")
    op.execute(
        "ALTER TABLE oauth_connect_sessions ADD COLUMN connection_id UUID "
        "REFERENCES platform_connections(id)"
    )
    op.execute("ALTER TABLE platform_accounts ADD COLUMN connection_id UUID")
    op.execute("""ALTER TABLE platform_accounts ADD COLUMN account_ref TEXT GENERATED ALWAYS AS
        (platform || ':' || CASE WHEN connection_id IS NULL THEN '' ELSE
        'account:' || business_id::text || ':' || connection_id::text || ':' END ||
        external_account_id) STORED UNIQUE""")
    op.execute("""ALTER TABLE platform_accounts
        ADD FOREIGN KEY(connection_id,business_id,platform)
        REFERENCES platform_connections(id,business_id,platform),
        DROP CONSTRAINT platform_accounts_platform_external_account_id_key,
        ADD UNIQUE NULLS NOT DISTINCT(platform,connection_id,external_account_id),
        ADD UNIQUE(id,business_id,platform,connection_id)""")
    # DROP EXPRESSION retains all old values and dependent FKs/hashes unchanged.
    op.execute("ALTER TABLE ad_entities ALTER COLUMN entity_ref DROP EXPRESSION")
    op.execute("ALTER TABLE ad_entities ADD COLUMN connection_id UUID")
    op.execute("""ALTER TABLE ad_entities
        DROP CONSTRAINT ad_entities_platform_level_external_unique,
        ADD UNIQUE NULLS NOT DISTINCT(platform,connection_id,level,external_id),
        ADD FOREIGN KEY(platform_account_id,business_id,platform,connection_id)
        REFERENCES platform_accounts(id,business_id,platform,connection_id)""")
    op.execute("""CREATE FUNCTION ads_scoped_entity_identity() RETURNS TRIGGER AS $$
        DECLARE account_connection UUID;
        BEGIN
            SELECT connection_id INTO account_connection FROM platform_accounts
              WHERE id=NEW.platform_account_id AND business_id=NEW.business_id
                AND platform=NEW.platform;
            IF NOT FOUND OR account_connection IS DISTINCT FROM NEW.connection_id THEN
                RAISE EXCEPTION 'connection_scope_mismatch';
            END IF;
            IF TG_OP='UPDATE' AND (NEW.business_id,NEW.connection_id,NEW.platform,
               NEW.level,NEW.external_id,NEW.platform_account_id) IS DISTINCT FROM
               (OLD.business_id,OLD.connection_id,OLD.platform,OLD.level,OLD.external_id,
                OLD.platform_account_id) THEN
                RAISE EXCEPTION 'entity_identity_is_immutable';
            END IF;
            NEW.entity_ref := NEW.platform || ':' || NEW.level || ':' ||
              CASE WHEN NEW.connection_id IS NULL THEN '' ELSE
                 NEW.business_id::text || ':' || NEW.connection_id::text || ':' END ||
              NEW.external_id;
            RETURN NEW;
        END; $$ LANGUAGE plpgsql""")
    op.execute("""CREATE TRIGGER ads_scoped_entity_identity BEFORE INSERT OR UPDATE
        ON ad_entities FOR EACH ROW EXECUTE FUNCTION ads_scoped_entity_identity()""")


def downgrade() -> None:
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM execution_reservations WHERE state='ACTIVE')
        OR EXISTS (SELECT 1 FROM executions WHERE outcome='UNKNOWN') THEN
            RAISE EXCEPTION 'Cannot downgrade with unresolved executions';
        END IF;
    END $$""")
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM platform_connections) THEN
            RAISE EXCEPTION 'Cannot downgrade with scoped connections; preserve signed history';
        END IF;
    END $$""")
    # PostgreSQL cannot turn a plain column back into generated. Capture every
    # dependent root FK/unique constraint before replacing it. PostgreSQL
    # restores inherited partition FKs when their parent is restored; replaying
    # those child constraints too would duplicate them. Only legacy data exists
    # here, so its generated value is identical, including signed references.
    bind = op.get_bind()
    constraints = bind.exec_driver_sql("""
        SELECT n.nspname, t.relname, c.conname, pg_get_constraintdef(c.oid), c.contype
        FROM pg_constraint c JOIN pg_class t ON t.oid=c.conrelid
        JOIN pg_namespace n ON n.oid=t.relnamespace
        WHERE c.conparentid = 0 AND ((c.confrelid='ad_entities'::regclass AND
               (SELECT attnum FROM pg_attribute WHERE attrelid='ad_entities'::regclass
                AND attname='entity_ref') = ANY(c.confkey))
           OR (c.conrelid='ad_entities'::regclass AND
               (SELECT attnum FROM pg_attribute WHERE attrelid='ad_entities'::regclass
                AND attname='entity_ref') = ANY(c.conkey)))
        ORDER BY (c.contype='f'), c.conname
    """).all()
    op.execute("DROP TRIGGER ads_scoped_entity_identity ON ad_entities")
    op.execute("DROP FUNCTION ads_scoped_entity_identity()")
    op.execute("ALTER TABLE ad_entities DROP COLUMN entity_ref CASCADE")
    op.execute("""ALTER TABLE ad_entities ADD COLUMN entity_ref TEXT GENERATED ALWAYS AS
        (platform || ':' || level || ':' || external_id) STORED""")
    quote = bind.dialect.identifier_preparer.quote
    for schema, table, name, definition, _kind in constraints:
        bind.exec_driver_sql(
            f"ALTER TABLE {quote(schema)}.{quote(table)} "  # noqa: S608 - DB metadata identifiers
            f"ADD CONSTRAINT {quote(name)} {definition}"
        )
    op.execute("ALTER TABLE ad_entities DROP COLUMN connection_id")
    op.execute("""ALTER TABLE ad_entities ADD CONSTRAINT ad_entities_platform_level_external_unique
        UNIQUE(platform,level,external_id)""")
    op.execute("ALTER TABLE platform_accounts DROP COLUMN account_ref")
    op.execute("ALTER TABLE platform_accounts DROP COLUMN connection_id")
    op.execute("""ALTER TABLE platform_accounts
        ADD CONSTRAINT platform_accounts_platform_external_account_id_key
        UNIQUE(platform,external_account_id)""")
    op.execute("ALTER TABLE oauth_connect_sessions DROP COLUMN connection_id")
    op.execute("DROP TABLE platform_connections")
