"""Durable, one-shot owner confirmations without Community MFA."""

from alembic import op

revision = "0038_owner_action_confirmations"
down_revision = "0037_execution_account_targets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE owner_action_confirmations (
        nonce UUID PRIMARY KEY,
        session_id UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        owner_id UUID NOT NULL REFERENCES owners(id) ON DELETE CASCADE,
        binding_hash TEXT NOT NULL CHECK (binding_hash ~ '^[a-f0-9]{64}$'),
        expires_at TIMESTAMPTZ NOT NULL,
        consumed_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""")


def downgrade() -> None:
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM owner_action_confirmations WHERE expires_at > now()) THEN
            RAISE EXCEPTION 'Cannot downgrade while consumed confirmations could be replayed';
        END IF;
    END $$""")
    op.execute("DROP TABLE owner_action_confirmations")
