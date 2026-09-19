"""Short-lived encrypted handoff from owner consent to a local installer."""

from alembic import op

revision = "0057_runtime_pairings"
down_revision = "0056_runtime_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE runtime_pairings (
        challenge TEXT PRIMARY KEY,
        request_hash TEXT NOT NULL,
        connection_id UUID NOT NULL REFERENCES runtime_connections(id),
        sealed_token TEXT NOT NULL,
        expires_at TIMESTAMPTZ NOT NULL DEFAULT now() + interval '5 minutes'
    )""")


def downgrade() -> None:
    op.execute("DROP TABLE runtime_pairings")
