"""Business-scoped, encrypted read-only store API connections."""

from alembic import op

revision = "0055_store_api_connection"
down_revision = "0054_mcp_oauth_loopback_resource"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE store_api_connections (
        business_id UUID PRIMARY KEY REFERENCES businesses(id),
        base_url TEXT NOT NULL,
        token_encrypted BYTEA NOT NULL,
        updated_by UUID REFERENCES owners(id) ON DELETE SET NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""")


def downgrade() -> None:
    op.execute("DROP TABLE store_api_connections")
