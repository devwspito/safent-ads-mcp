"""Persist incomplete campaign briefs without creating executable proposals."""

from alembic import op

revision = "0041_campaign_drafts"
down_revision = "0040_managed_provider_account"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE campaign_drafts (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        business_id UUID NOT NULL REFERENCES businesses(id),
        draft_key VARCHAR(64) NOT NULL,
        revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0),
        brief JSONB NOT NULL CHECK(jsonb_typeof(brief)='object'),
        proposal_id UUID REFERENCES proposals(id),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE(business_id,draft_key)
    )""")


def downgrade() -> None:
    # Do not silently erase owner planning work during a downgrade.
    op.execute("""DO $$ BEGIN
        IF EXISTS(SELECT 1 FROM campaign_drafts) THEN
            RAISE EXCEPTION 'campaign_drafts_not_empty';
        END IF;
    END $$""")
    op.drop_table("campaign_drafts")
