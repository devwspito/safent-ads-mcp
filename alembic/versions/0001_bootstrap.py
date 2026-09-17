"""0001_bootstrap: extensiones, businesses, credential_refs,
platform_accounts, owners, sessions, login_attempts.

data-model.md §Migration plan, paso 1. Revision id <= 32 caracteres
(assumptions.md #4 del plan: un id largo entra en bucle al arrancar).

Revision ID: 0001_bootstrap
Revises:
Create Date: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0001_bootstrap"
down_revision: str | None = None
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.execute('CREATE EXTENSION IF NOT EXISTS "pgcrypto"')

    op.execute("""
        CREATE TABLE businesses (
            id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            slug                TEXT NOT NULL UNIQUE,
            name                TEXT NOT NULL,
            timezone            TEXT NOT NULL,
            reference_currency  TEXT NOT NULL CHECK (char_length(reference_currency) = 3),
            is_active           BOOLEAN NOT NULL DEFAULT true,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)

    # CredentialRef: "nunca contiene el secreto; solo un alias que el broker
    # resuelve contra su propio almacen" (data-model.md).
    op.execute("""
        CREATE TABLE credential_refs (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            platform    TEXT NOT NULL CHECK (platform IN ('google', 'meta')),
            alias       TEXT NOT NULL UNIQUE,
            scopes      TEXT[] NOT NULL DEFAULT '{}',
            rotated_at  TIMESTAMPTZ,
            expires_at  TIMESTAMPTZ,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)

    op.execute("""
        CREATE TABLE platform_accounts (
            id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            business_id           UUID NOT NULL REFERENCES businesses (id),
            platform              TEXT NOT NULL CHECK (platform IN ('google', 'meta')),
            external_account_id   TEXT NOT NULL,
            currency              TEXT NOT NULL,
            timezone              TEXT NOT NULL,
            api_tier              TEXT NOT NULL,
            credential_ref_id     UUID REFERENCES credential_refs (id),
            status                TEXT NOT NULL
                                    CHECK (status IN
                                        ('ACTIVE', 'THROTTLED', 'SUSPENDED', 'READ_ONLY')),
            last_synced_at        TIMESTAMPTZ,
            created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (platform, external_account_id)
        )
    """)
    op.execute(
        "CREATE INDEX ix_platform_accounts_business ON platform_accounts (business_id)"
    )

    # Owner/TotpSecret: "Argon2id; TOTP obligatorio, cifrado en reposo"
    # (data-model.md). El secreto TOTP vive cifrado en la misma fila: no hay
    # tabla propia en el plan de migracion.
    op.execute("""
        CREATE TABLE owners (
            id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            email                   TEXT NOT NULL UNIQUE,
            password_hash           TEXT NOT NULL,
            totp_secret_encrypted   BYTEA,
            totp_confirmed_at       TIMESTAMPTZ,
            created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)

    # "token de sesion almacenado hasheado" (data-model.md / rest-api.md).
    op.execute("""
        CREATE TABLE sessions (
            id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id     UUID NOT NULL REFERENCES owners (id),
            token_hash   TEXT NOT NULL UNIQUE,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at   TIMESTAMPTZ NOT NULL,
            revoked_at   TIMESTAMPTZ
        )
    """)
    op.execute("CREATE INDEX ix_sessions_owner ON sessions (owner_id)")

    # "bloqueo tras 5 intentos/15 min" (data-model.md / threat-model.md C-25).
    op.execute("""
        CREATE TABLE login_attempts (
            id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            email         TEXT NOT NULL,
            succeeded     BOOLEAN NOT NULL,
            ip_address    INET,
            attempted_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute(
        "CREATE INDEX ix_login_attempts_email_time ON login_attempts (email, attempted_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE login_attempts")
    op.execute("DROP TABLE sessions")
    op.execute("DROP TABLE owners")
    op.execute("DROP TABLE platform_accounts")
    op.execute("DROP TABLE credential_refs")
    op.execute("DROP TABLE businesses")
