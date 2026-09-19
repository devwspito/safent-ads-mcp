"""Client-independent workspaces; adopt existing work without new approvals."""

from alembic import op

revision = "0058_workspaces"
down_revision = "0057_runtime_pairings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE workspaces (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        business_id UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
        workspace_key TEXT NOT NULL,
        revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0),
        brief JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE(business_id, workspace_key), UNIQUE(business_id, id)
    )""")
    op.execute("""CREATE TABLE workspace_events (
        seq BIGSERIAL PRIMARY KEY,
        business_id UUID NOT NULL,
        workspace_id UUID NOT NULL,
        actor TEXT NOT NULL,
        kind TEXT NOT NULL,
        payload JSONB NOT NULL,
        occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        FOREIGN KEY(business_id, workspace_id) REFERENCES workspaces(business_id,id)
          ON DELETE CASCADE
    )""")
    op.execute("CREATE INDEX workspace_events_scope ON workspace_events(workspace_id,seq)")
    for table in ("campaign_drafts", "runtime_jobs"):
        op.execute(f"ALTER TABLE {table} ADD COLUMN workspace_id UUID")
        op.execute(f"""ALTER TABLE {table} ADD CONSTRAINT {table}_workspace_fk
            FOREIGN KEY(business_id,workspace_id) REFERENCES workspaces(business_id,id)""")
    op.execute("""INSERT INTO workspaces(business_id,workspace_key,brief)
        SELECT DISTINCT ON (business_id,slug) business_id, 'launch:' || slug,
          jsonb_build_object('title',COALESCE(context->>'title',slug),'source_slug',slug)
        FROM runtime_jobs ORDER BY business_id,slug,created_at DESC""")
    op.execute("""UPDATE runtime_jobs j SET workspace_id=w.id FROM workspaces w
        WHERE w.business_id=j.business_id AND w.workspace_key='launch:' || j.slug""")
    op.execute("""UPDATE campaign_drafts d SET workspace_id=j.workspace_id FROM runtime_jobs j
        WHERE d.business_id=j.business_id
          AND d.draft_key='runtime-' || replace(j.id::text,'-','')""")
    op.execute("""INSERT INTO workspaces(business_id,workspace_key,brief)
        SELECT business_id,'draft:' || draft_key,jsonb_build_object('title',brief->>'title')
        FROM campaign_drafts WHERE workspace_id IS NULL""")
    op.execute("""UPDATE campaign_drafts d SET workspace_id=w.id FROM workspaces w
        WHERE d.workspace_id IS NULL AND d.business_id=w.business_id
          AND w.workspace_key='draft:' || d.draft_key""")
    # Carry existing structured intent and notes forward, but do not parse prose
    # into new confirmations, spend caps, dates or authorization claims.
    op.execute("""WITH latest AS (
        SELECT DISTINCT ON (business_id,workspace_id) business_id,workspace_id,brief
        FROM campaign_drafts ORDER BY business_id,workspace_id,updated_at DESC,id
    ) UPDATE workspaces w SET brief=w.brief || jsonb_build_object(
        'objective',latest.brief->'objective','notes',latest.brief->'notes')
      FROM latest WHERE w.business_id=latest.business_id AND w.id=latest.workspace_id""")


def downgrade() -> None:
    for table in ("campaign_drafts", "runtime_jobs"):
        op.execute(f"ALTER TABLE {table} DROP COLUMN workspace_id")
    op.execute("DROP TABLE workspace_events")
    op.execute("DROP TABLE workspaces")
