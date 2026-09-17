"""0013_oauth_connect: conexion inicial de cuentas de plataforma desde la UI
("Conectar"), US3.

Aditiva (data-model.md: "0001-0010 no se tocan"; esta tampoco toca
0011/0012 ya fusionadas):

1. `oauth_connect_sessions` — igual invariante que `ReconnectSession`
   (data-model.md): **nunca `code`, `code_verifier`, `client_secret` ni
   token**. Solo `state_hash` (hasheado, como el token de sesion), un solo
   uso via CHECK de coherencia estado/`completed_at`, TTL corto. El
   `code_verifier` PKCE lo genera y guarda el broker en su propio almacen
   cifrado (`broker/infrastructure/credential_store.py`), fuera de esta
   base de datos.
2. `credential_refs` gana columnas de salud (`status`, `obtained_at`,
   `last_validated_at`, `revoked_at`): son exactamente los metadatos que
   `GET /platform-accounts` expone (token.health) sin que el secreto
   entre nunca en esta tabla (invariante ya escrita en 0001_bootstrap:
   "CredentialRef... nunca contiene el secreto"). No se crea una tabla
   `platform_credentials` en Postgres a proposito: guardar ahi el token
   cifrado violaria threat-model.md C-24 ("Secretos solo en entorno o
   secret manager... nunca en BD"); el broker no tiene ni tendra
   `database_url` (composition/settings.py: `BrokerSettings` aislado a
   proposito). El "platform_credentials" que pide el encargo vive como
   almacen cifrado en disco del propio broker, no en esta migracion.

Encadenada tras `0014_brand` (no `0012_domain_alignment`): esa migracion
llego primero a `main` mientras esta rama trabajaba en paralelo, y ambas
son aditivas y mutuamente independientes (tablas distintas, sin FKs
cruzadas) -- el propio `0014_brand` documenta "solo necesita que alguien
reencadene down_revision, nunca que se reescriba". `alembic heads` debe
imprimir una unica cabeza tras esto.

Revision ID: 0013_oauth_connect
Revises: 0014_brand
Create Date: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0013_oauth_connect"
down_revision: str | None = "0014_brand"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE credential_refs
            ADD COLUMN status TEXT NOT NULL DEFAULT 'CONNECTED'
                CHECK (status IN ('CONNECTED', 'EXPIRED', 'REVOKED', 'INVALID')),
            ADD COLUMN obtained_at TIMESTAMPTZ,
            ADD COLUMN last_validated_at TIMESTAMPTZ,
            ADD COLUMN revoked_at TIMESTAMPTZ
    """)

    op.execute("""
        CREATE TABLE oauth_connect_sessions (
            id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            business_id   UUID NOT NULL REFERENCES businesses (id),
            owner_id      UUID NOT NULL REFERENCES owners (id),
            provider      TEXT NOT NULL CHECK (provider IN ('google', 'meta')),
            state_hash    TEXT NOT NULL UNIQUE,
            status        TEXT NOT NULL DEFAULT 'waiting'
                            CHECK (status IN ('waiting', 'ok', 'error')),
            error_code    TEXT,
            expires_at    TIMESTAMPTZ NOT NULL,
            completed_at  TIMESTAMPTZ,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            -- un solo uso: resuelta (ok/error) siempre lleva completed_at,
            -- en espera nunca lo lleva.
            CONSTRAINT oauth_connect_sessions_completion_matches_status CHECK (
                (status = 'waiting' AND completed_at IS NULL)
                OR (status <> 'waiting' AND completed_at IS NOT NULL)
            )
        )
    """)
    op.execute(
        "CREATE INDEX ix_oauth_connect_sessions_business ON oauth_connect_sessions (business_id)"
    )
    # Purga de sesiones caducadas (mismo patron que telegram_callbacks, 0010).
    op.execute(
        "CREATE INDEX ix_oauth_connect_sessions_expires ON oauth_connect_sessions (expires_at)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE oauth_connect_sessions")
    op.execute("""
        ALTER TABLE credential_refs
            DROP COLUMN status,
            DROP COLUMN obtained_at,
            DROP COLUMN last_validated_at,
            DROP COLUMN revoked_at
    """)
