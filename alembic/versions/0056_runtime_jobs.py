"""Durable, business-scoped preparation jobs and revocable runtime bridges."""

from alembic import op

revision = "0056_runtime_jobs"
down_revision = "0055_store_api_connection"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE runtime_connections (
        id UUID PRIMARY KEY,
        business_id UUID NOT NULL REFERENCES businesses(id),
        label TEXT NOT NULL,
        runtime TEXT NOT NULL CHECK(runtime IN ('codex','claude')),
        token_hash TEXT NOT NULL UNIQUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        expires_at TIMESTAMPTZ NOT NULL DEFAULT now() + interval '30 days',
        revoked_at TIMESTAMPTZ,
        last_seen_at TIMESTAMPTZ
    )""")
    op.execute("""CREATE TABLE runtime_jobs (
        id UUID PRIMARY KEY,
        business_id UUID NOT NULL REFERENCES businesses(id),
        slug TEXT NOT NULL,
        revision TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'queued'
          CHECK(state IN ('queued','running','prepared','blocked','failed','cancelled')),
        context JSONB NOT NULL,
        result JSONB,
        holder TEXT,
        lease_hash TEXT,
        lease_until TIMESTAMPTZ,
        attempts INTEGER NOT NULL DEFAULT 0,
        message TEXT NOT NULL DEFAULT 'Esperando un runtime conectado.',
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE(business_id,slug,revision)
    )""")
    op.execute("""CREATE INDEX runtime_jobs_claim ON runtime_jobs(business_id, created_at)
        WHERE state IN ('queued','running')""")


def downgrade() -> None:
    op.execute("DROP TABLE runtime_jobs")
    op.execute("DROP TABLE runtime_connections")
