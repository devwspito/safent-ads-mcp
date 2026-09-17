"""0050_cloudflare_connection: `cloudflare_connection` (lane
006-cloudflare-ui, owner decision -- "crear una conexion con Cloudflare
pidiendo el token e indicando el enlace donde crearlo").

Fila unica cifrada, mismo criterio de singleton por INSTALACION que las
credenciales de VENDOR de Google/Meta (`broker/infrastructure/
credential_store.py::_app_credentials_path`, un fichero por plataforma, no
por propietario ni por negocio): el token de Cloudflare administra DNS de
la instalacion entera, no de un negocio ni de un propietario concreto --
`id BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id)` fuerza a nivel de
constraint que nunca exista mas de una fila (`INSERT ... ON CONFLICT (id)
DO UPDATE` en `SqlCloudflareConnectionStore` es siempre un upsert de esa
unica fila).

`api_token_encrypted` es AES-256-GCM con la MISMA clave `ADS_TOTP_ENC_KEY`
que ya cifra el secreto TOTP y el codigo de emparejamiento de Telegram
(`AesGcmTotpCipher` reusada, mismo criterio documentado en
`0022_telegram_pairing`: "no un secreto nuevo que desplegar"). `zones` es
JSONB (lista de nombres de zona auto-descubierta al conectar, `GET
/zones` con el token ya verificado) -- mismo patron `payload::text` +
`CAST(:zones AS JSONB)` que `decision_log`/`0002_audit_chain`, nunca
confiar en la decodificacion automatica jsonb->list del driver
(`audit/infrastructure/sql_repository.py`).

`connected_by_owner_id` es solo auditoria (que propietario tecleo el
token la ultima vez) -- `ON DELETE SET NULL`: borrar al propietario nunca
debe arrastrar ni bloquear el borrado de la conexion.

Aditiva sobre `0049_package_step_state_hash` (head actual).

Revision ID: 0050_cloudflare_connection
Revises: 0049_package_step_state_hash
Create Date: 2026-09-15
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0050_cloudflare_connection"
down_revision: str | None = "0049_package_step_state_hash"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE cloudflare_connection (
            id                      BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id),
            api_token_encrypted     BYTEA NOT NULL,
            account_id              TEXT,
            zones                   JSONB NOT NULL DEFAULT '[]'::jsonb,
            connected_at            TIMESTAMPTZ NOT NULL,
            connected_by_owner_id   UUID REFERENCES owners (id) ON DELETE SET NULL,
            updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        COMMENT ON TABLE cloudflare_connection IS
        'Conexion Cloudflare de la INSTALACION (owner decision, lane
        006-cloudflare-ui): una unica fila (id BOOLEAN PRIMARY KEY DEFAULT
        TRUE), igual que google.enc/meta.enc en el broker. api_token_encrypted
        es AES-256-GCM con ADS_TOTP_ENC_KEY (AesGcmTotpCipher reusada).'
    """)


def downgrade() -> None:
    op.execute("DROP TABLE cloudflare_connection")
