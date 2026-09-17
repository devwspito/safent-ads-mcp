"""Read-only physical observations; raw OAuth provenance is never rewritten.

Upgrade adds projections and exact scoped-account proposal validation.
Downgrade restores previous validation only if no scoped account proposal
would lose representation. No facts, authorizations, receipts or historical
hashes are modified in either direction.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0036_physical_metric_views"
down_revision: str | None = "0035_connection_identity"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""CREATE VIEW ad_entities_physical AS
        SELECT DISTINCT ON (e.business_id,a.platform,a.external_account_id,e.level,e.external_id)
               e.*
        FROM ad_entities e JOIN platform_accounts a ON a.id=e.platform_account_id
            AND a.business_id=e.business_id AND a.platform=e.platform
        ORDER BY e.business_id,a.platform,a.external_account_id,e.level,e.external_id,e.entity_ref
    """)
    op.execute("""
        CREATE OR REPLACE FUNCTION proposals_entity_exists() RETURNS TRIGGER AS $$
        BEGIN
            IF EXISTS (SELECT 1 FROM ad_entities
                       WHERE business_id=NEW.business_id AND entity_ref=NEW.entity_ref)
            OR EXISTS (
                SELECT 1 FROM platform_accounts WHERE business_id=NEW.business_id
                AND ((connection_id IS NOT NULL AND account_ref=NEW.entity_ref) OR
                     (connection_id IS NULL AND
                      platform || ':account:' || external_account_id=NEW.entity_ref))
            ) THEN RETURN NEW; END IF;
            RAISE EXCEPTION 'proposal_entity_scope_mismatch: %',
                'no existe en ad_entities ni platform_accounts';
        END; $$ LANGUAGE plpgsql
    """)
    # Identifiers are constants owned by this migration, never request input.
    for table, hour in (("metrics_daily", ""), ("metrics_hourly", ", m.stat_hour")):
        op.execute(f"""
            CREATE VIEW {table}_physical AS
            SELECT DISTINCT ON (m.business_id, a.platform, a.external_account_id,
                                e.level, e.external_id, m.stat_date{hour})
                   m.*, identity.entity_ref AS physical_entity_ref
            FROM {table} m
            JOIN ad_entities e ON e.entity_ref=m.entity_ref AND e.business_id=m.business_id
                              AND e.platform_account_id=m.platform_account_id
            JOIN platform_accounts a ON a.id=e.platform_account_id
                                    AND a.business_id=e.business_id AND a.platform=e.platform
            CROSS JOIN LATERAL (
                SELECT min(sibling.entity_ref) AS entity_ref
                FROM ad_entities sibling
                JOIN platform_accounts account ON account.id=sibling.platform_account_id
                WHERE account.business_id=a.business_id AND account.platform=a.platform
                  AND account.external_account_id=a.external_account_id
                  AND sibling.business_id=e.business_id AND sibling.platform=e.platform
                  AND sibling.level=e.level AND sibling.external_id=e.external_id
            ) identity
            ORDER BY m.business_id, a.platform, a.external_account_id,
                     e.level, e.external_id, m.stat_date{hour},
                     m.ingested_at DESC, m.revision DESC, m.entity_ref
        """)  # noqa: S608 - fixed migration-owned identifiers


def downgrade() -> None:
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM proposals p JOIN platform_accounts a
                   ON a.account_ref=p.entity_ref AND a.business_id=p.business_id
                   WHERE a.connection_id IS NOT NULL) THEN
            RAISE EXCEPTION 'Cannot downgrade with scoped account proposals; preserve history';
        END IF;
    END $$""")
    op.execute("""
        CREATE OR REPLACE FUNCTION proposals_entity_exists() RETURNS TRIGGER AS $$
        BEGIN
            IF EXISTS (SELECT 1 FROM ad_entities
                       WHERE business_id=NEW.business_id AND entity_ref=NEW.entity_ref)
            OR EXISTS (SELECT 1 FROM platform_accounts WHERE business_id=NEW.business_id
                       AND platform || ':account:' || external_account_id=NEW.entity_ref)
            THEN RETURN NEW; END IF;
            RAISE EXCEPTION 'proposal_entity_scope_mismatch: %',
                'no existe en ad_entities ni platform_accounts';
        END; $$ LANGUAGE plpgsql
    """)
    op.execute("DROP VIEW metrics_hourly_physical")
    op.execute("DROP VIEW metrics_daily_physical")
    op.execute("DROP VIEW ad_entities_physical")
