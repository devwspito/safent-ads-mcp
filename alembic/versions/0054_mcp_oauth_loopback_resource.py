"""0054_mcp_oauth_loopback_resource: T049 (spec 008) -- `oauth_authorization_
requests_resource_check`/`oauth_grants_resource_check` (0035_mcp_oauth) solo
admitian `resource` https, pero `composition/settings.py` ya admite
`ADS_PUBLIC_BASE_URL=http://localhost` (y, tras el fix del mismo lote,
`http://127.0.0.1`/`http://[::1]`) para desarrollo local -- exactamente el
`127.0.0.1:8410` que documenta el README. `ResourceIndicator.canonical()`
(`mcp_oauth/domain/resource.py`) construye `resource` SIEMPRE a partir de
`public_base_url`, asi que con esa base de bucle local el primer `INSERT
INTO oauth_authorization_requests` de CUALQUIER cliente (reproducido con
Codex, T049) violaba el CHECK https-only y `SdkOAuthProvider.authorize()`
dejaba escapar un `IntegrityError` crudo -- 500 `server_error` en
`/authorize` (`sdk:handlers/authorize.py` catch-all).

El arreglo es ampliar el CHECK, no relajar el dominio: mismo patron que
`oauth_authorization_requests_redirect_uri_check` (0035_mcp_oauth) ya usa
para `redirect_uri` -- https en general, o bucle local por `http` con
`127.0.0.1`/`localhost`/`[::1]` y el puerto que sea. Un `resource` remoto
por `http` sigue rechazado igual que antes; nada cambia para un
`public_base_url` https de produccion.

`oauth_grants.resource` hereda su valor del mismo `ResourceIndicator`
(`RedeemCode`/`RefreshGrant` nunca lo recalculan), asi que necesita
exactamente la misma ampliacion o el canje de codigo repetiria el mismo
`IntegrityError` un paso mas tarde.

Sin migracion de datos: las dos columnas solo podian contener `https://...`
hasta hoy (el CHECK anterior lo garantizaba), asi que ninguna fila existente
dejaria de cumplir el CHECK ampliado.

Revision ID: 0054_mcp_oauth_loopback_resource
Revises: 0053_federated_transaction_ip
Create Date: 2026-09-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0054_mcp_oauth_loopback_resource"
down_revision: str | None = "0053_federated_transaction_ip"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

# Revision de codigo (PR 44): puerto acotado a 1-65535 de verdad, no
# `[0-9]{1,5}` (que admitiria `:99999`) -- esta migracion no ha llegado a
# produccion todavia, asi que corregir el patron aqui mismo no reescribe
# nada ya desplegado. MISMO rango que `mcp_oauth/domain/resource.py::
# ResourceIndicator`/`RedirectUri` (agreement probado por
# `tests/integration/migrations/test_0054_mcp_oauth_loopback_resource.py`).
_VALID_PORT = (
    r"(6553[0-5]|655[0-2][0-9]|65[0-4][0-9]{2}|6[0-4][0-9]{3}|[1-5][0-9]{4}|[1-9][0-9]{0,3})"
)

# RFC 8707: https en general; o bucle local por http (RFC 8252 SS7.3, mismo
# conjunto cerrado que `RedirectUri`/`oauth_authorization_requests_redirect_
# uri_check`) -- sin query ni fragmento en ninguna de las dos ramas (el
# recurso es `<base>/mcp` y nada mas, igual que antes).
_RESOURCE_CHECK = (
    r"resource ~ '^https://[^/?#]+(/[^?#]*)?$' "
    rf"OR resource ~ '^http://(127\.0\.0\.1|localhost|\[::1\])(:{_VALID_PORT})?(/[^?#]*)?$'"
)

_TABLE_CONSTRAINT_NAMES = (
    ("oauth_authorization_requests", "oauth_authorization_requests_resource_check"),
    ("oauth_grants", "oauth_grants_resource_check"),
)

# Constraints originales (0035_mcp_oauth), para un downgrade fiel.
_ORIGINAL_RESOURCE_CHECK = "resource ~ '^https://[^/?#]+(/[^?#]*)?$'"


def upgrade() -> None:
    for table, constraint in _TABLE_CONSTRAINT_NAMES:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {constraint}")
        op.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT {constraint} CHECK ({_RESOURCE_CHECK})"
        )


def downgrade() -> None:
    # Sin guarda de datos (como 0053): una fila `http://` de bucle local
    # viva en el momento del downgrade rompe el `ADD CONSTRAINT` original
    # -- esperado, el downgrade es para revertir el despliegue, no para
    # convivir con datos que solo el nuevo CHECK admite.
    for table, constraint in _TABLE_CONSTRAINT_NAMES:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {constraint}")
        op.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT {constraint} "
            f"CHECK ({_ORIGINAL_RESOURCE_CHECK})"
        )
