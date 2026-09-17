"""Fusion de las dos cabezas que salieron de `0034_execution_reservations`:
`0035_mcp_oauth` (spec 002, tablas `oauth_clients`/`oauth_grants`/
`oauth_tokens`/`oauth_authorization_requests`) y la cadena de lane/003
(`0035_connection_identity` .. `0050_cloudflare_connection`).

Revision de fusion pura: no crea, altera ni borra nada. Las dos ramas tocan
conjuntos de tablas disjuntos -- el prefijo `oauth_` de spec 002 no colisiona
con `oauth_connect_sessions` (estado del conectar-cuenta de plataforma, otra
cosa) ni con ninguna tabla de paquetes/campanas de la lane.

Revision ID: 0051_merge_oauth_lane003
"""

from __future__ import annotations

# Regla del repo: id de revision <= 32 caracteres.
revision: str = "0051_merge_oauth_lane003"
down_revision: tuple[str, str] = ("0035_mcp_oauth", "0050_cloudflare_connection")
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    """Sin operaciones: la fusion solo reune las dos ramas."""


def downgrade() -> None:
    """Sin operaciones: deshacer la fusion vuelve a dejar dos cabezas."""
