"""0054_mcp_oauth_loopback_resource: `oauth_authorization_requests_resource_
check`/`oauth_grants_resource_check` (0035_mcp_oauth) solo admitian
`resource` https -- T049 (spec 008): con un `ADS_PUBLIC_BASE_URL` de bucle
local (el `127.0.0.1:8410` que documenta el README), el primer INSERT de
CUALQUIER cliente violaba el CHECK y `SdkOAuthProvider.authorize()` dejaba
escapar un `IntegrityError` crudo -- 500 `server_error` en `/authorize`.

Contra la base compartida ya migrada a cabecera (fixture `pg`, mismo patron
que `test_0035_mcp_oauth.py`): aisla el CHECK de toda la maquinaria de
aplicacion, insertando directamente.

Revision de seguridad (PR 44): cada caso se comprueba tambien contra
`mcp_oauth.domain.resource.ResourceIndicator` -- el agregado que
`ApiSettings` construye EN EL ARRANQUE
(`composition/settings.py::_require_a_valid_oauth_resource`) para fallar
ahi, con un mensaje que nombra `ADS_PUBLIC_BASE_URL`, en vez de en el
primer `/authorize` real. Si el CHECK de Postgres y `ResourceIndicator`
llegasen a divergir, es AQUI donde se veria."""

from __future__ import annotations

import base64
import secrets
import uuid

import asyncpg
import pytest

from safent_ads.mcp_oauth.domain.errors import InvalidResourceError
from safent_ads.mcp_oauth.domain.resource import ResourceIndicator

pytestmark = pytest.mark.integration

_REDIRECT_URI = "http://127.0.0.1:54321/callback"

_INSERT_CLIENT = """
    INSERT INTO oauth_clients (client_id, client_name, redirect_uris,
                               token_endpoint_auth_method, client_secret_hash,
                               grant_types, requested_scopes, created_at)
    VALUES ($1, '0054 resource check client',
            '["http://127.0.0.1:54321/callback"]'::jsonb, 'none', NULL,
            '["authorization_code"]'::jsonb, 'ads:read', now())
"""

_INSERT_AUTHORIZATION_REQUEST = """
    INSERT INTO oauth_authorization_requests (txn_id, client_id, redirect_uri,
                                              redirect_uri_explicit, code_challenge,
                                              scopes, resource, created_at, expires_at)
    VALUES ($1, $2, $3, TRUE, $4, 'ads:read', $5, now(), now() + interval '10 minutes')
"""

_INSERT_GRANT = """
    INSERT INTO oauth_grants (grant_id, owner_id, client_id, scopes, resource, created_at)
    VALUES ($1, $2, $3, 'ads:read', $4, now())
"""


def _code_challenge() -> str:
    """PKCE S256: 43 caracteres base64url sin relleno (RFC 7636 SS4.2)."""
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")


async def _make_client(pg: asyncpg.Connection) -> str:
    client_id = str(uuid.uuid4())
    await pg.execute(_INSERT_CLIENT, client_id)
    return client_id


async def _make_owner(pg: asyncpg.Connection) -> uuid.UUID:
    owner_id = uuid.uuid4()
    await pg.execute(
        "INSERT INTO owners (id, email, password_hash) "
        "VALUES ($1, $2, 'argon2id$fixture$not-a-real-hash')",
        owner_id,
        f"owner-{owner_id.hex[:8]}@safent.example",
    )
    return owner_id


async def _insert_authorization_request(
    pg: asyncpg.Connection, *, client_id: str, resource: str
) -> None:
    await pg.execute(
        _INSERT_AUTHORIZATION_REQUEST,
        uuid.uuid4(),
        client_id,
        _REDIRECT_URI,
        _code_challenge(),
        resource,
    )


async def _insert_grant(
    pg: asyncpg.Connection, *, owner_id: uuid.UUID, client_id: str, resource: str
) -> None:
    await pg.execute(_INSERT_GRANT, uuid.uuid4(), owner_id, client_id, resource)


_ACCEPTED_RESOURCES = [
    "https://ads.example.com/mcp",
    "https://ads.example.com:9443/mcp",
    "http://127.0.0.1/mcp",
    "http://127.0.0.1:8410/mcp",
    "http://localhost:8410/mcp",
    "http://[::1]:8410/mcp",
    "http://127.0.0.1:1/mcp",
    "http://127.0.0.1:65535/mcp",
]

_REJECTED_RESOURCES = [
    "http://ads.example.com/mcp",
    "http://127.0.0.1.evil.example:8410/mcp",
    "http://localhost.evil/mcp",
    "http://127.0.0.2/mcp",
    "ftp://127.0.0.1/mcp",
    "https://ads.example.com/mcp?debug=1",
    "https://ads.example.com/mcp#section",
    "http://127.0.0.1:0/mcp",
    "http://127.0.0.1:65536/mcp",
    "http://127.0.0.1:99999/mcp",
]


def _agrees_with_the_domain(resource: str) -> bool:
    try:
        ResourceIndicator(resource)
    except InvalidResourceError:
        return False
    return True


@pytest.mark.parametrize("resource", _ACCEPTED_RESOURCES)
async def test_resource_check_accepts_https_and_loopback_http(
    pg: asyncpg.Connection, resource: str
) -> None:
    client_id = await _make_client(pg)

    await _insert_authorization_request(pg, client_id=client_id, resource=resource)

    assert _agrees_with_the_domain(resource) is True


@pytest.mark.parametrize("resource", _REJECTED_RESOURCES)
async def test_resource_check_still_rejects_everything_else(
    pg: asyncpg.Connection, resource: str
) -> None:
    client_id = await _make_client(pg)

    with pytest.raises(
        asyncpg.CheckViolationError, match="oauth_authorization_requests_resource_check"
    ):
        await _insert_authorization_request(pg, client_id=client_id, resource=resource)

    assert _agrees_with_the_domain(resource) is False


@pytest.mark.parametrize("resource", _ACCEPTED_RESOURCES)
async def test_grants_resource_check_accepts_https_and_loopback_http(
    pg: asyncpg.Connection, resource: str
) -> None:
    client_id = await _make_client(pg)
    owner_id = await _make_owner(pg)

    await _insert_grant(pg, owner_id=owner_id, client_id=client_id, resource=resource)


@pytest.mark.parametrize("resource", _REJECTED_RESOURCES)
async def test_grants_resource_check_still_rejects_everything_else(
    pg: asyncpg.Connection, resource: str
) -> None:
    client_id = await _make_client(pg)
    owner_id = await _make_owner(pg)

    with pytest.raises(asyncpg.CheckViolationError, match="oauth_grants_resource_check"):
        await _insert_grant(pg, owner_id=owner_id, client_id=client_id, resource=resource)
