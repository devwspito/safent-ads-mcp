"""`RegisterClient` (contracts/oauth.md §3, tasks.md T005): secreto
hasheado para clientes confidenciales, ninguno para publicos, y M3 de la
revision de seguridad (16-sep, threat-model.md C-42): al tope de clientes
sin consentir, desalojo del mas antiguo en vez de rechazo -- con un techo
duro (10x el tope) que si rechaza."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from safent_ads.mcp_oauth.application.errors import TooManyUnconsentedClientsError
from safent_ads.mcp_oauth.application.policy import (
    AUTHORIZATION_REQUEST_TTL,
    MAX_UNCONSENTED_CLIENTS,
    MAX_UNCONSENTED_CLIENTS_HARD_CEILING,
)
from safent_ads.mcp_oauth.application.register_client import ClientRegistration, RegisterClient
from safent_ads.mcp_oauth.domain.client import (
    OAuthClient,
    OAuthClientState,
    RedirectUri,
    TokenEndpointAuthMethod,
)
from safent_ads.mcp_oauth.domain.errors import InvalidRedirectUriError
from safent_ads.mcp_oauth.domain.scope import ScopeSet
from safent_ads.mcp_oauth.infrastructure.fakes import InMemoryClientRepository
from safent_ads.mcp_oauth.infrastructure.secrets_token_factory import SecretsOpaqueTokenFactory
from safent_ads.mcp_oauth.infrastructure.sha256_token_hasher import Sha256TokenHasher
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import UuidIdGenerator

_NOW = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)


def _use_case(clients: InMemoryClientRepository) -> RegisterClient:
    return RegisterClient(
        clients=clients,
        id_generator=UuidIdGenerator(),
        token_hasher=Sha256TokenHasher(),
        token_factory=SecretsOpaqueTokenFactory(),
        clock=FixedClock(_NOW),
    )


def _registration(**overrides: object) -> ClientRegistration:
    defaults: dict[str, object] = {
        "client_name": "Claude Code",
        "redirect_uris": ("http://127.0.0.1:54321/callback",),
        "token_endpoint_auth_method": TokenEndpointAuthMethod.NONE,
        "grant_types": ("authorization_code", "refresh_token"),
        "requested_scope": ScopeSet.parse("ads:read ads:propose"),
    }
    defaults.update(overrides)
    return ClientRegistration(**defaults)  # type: ignore[arg-type]


async def test_public_client_is_registered_without_a_secret() -> None:
    clients = InMemoryClientRepository()
    use_case = _use_case(clients)

    registered = await use_case.execute(_registration())

    assert registered.client_secret is None
    assert registered.client.client_secret_hash is None
    assert registered.client.state is OAuthClientState.REGISTERED
    assert await clients.get_by_id(registered.client.id) is registered.client


@pytest.mark.parametrize(
    "redirect_uri",
    (
        "https://agent.example/callback",
        "http://agent.example/callback",
        "http://127.0.0.1.evil.com/callback",
    ),
)
async def test_register_rejects_non_loopback_redirect_uri(redirect_uri: str) -> None:
    """D-11 (threat-model.md C-70 pieza 4): el registro dinamico solo
    admite destinos de la propia maquina. `sdk_provider.register_client`
    traduce este error a `invalid_client_metadata` (RFC 7591), asi que el
    cliente remoto nunca llega a existir en la BD."""
    clients = InMemoryClientRepository()
    use_case = _use_case(clients)

    with pytest.raises(InvalidRedirectUriError):
        await use_case.execute(_registration(redirect_uris=(redirect_uri,)))

    assert await clients.count_unconsented() == 0


async def test_register_accepts_a_loopback_redirect_uri_over_tls() -> None:
    """La cara amable de D-11: un agente que monta TLS en su propia
    maquina sigue pudiendo registrarse."""
    clients = InMemoryClientRepository()
    use_case = _use_case(clients)

    registered = await use_case.execute(
        _registration(redirect_uris=("https://localhost:8443/callback",))
    )

    assert [str(uri) for uri in registered.client.redirect_uris] == [
        "https://localhost:8443/callback"
    ]


async def test_confidential_client_gets_a_hashed_secret_returned_once() -> None:
    clients = InMemoryClientRepository()
    use_case = _use_case(clients)

    registered = await use_case.execute(
        _registration(token_endpoint_auth_method=TokenEndpointAuthMethod.CLIENT_SECRET_POST)
    )

    assert registered.client_secret is not None
    assert registered.client.client_secret_hash is not None
    assert registered.client_secret not in registered.client.client_secret_hash


def _unconsented_client(client_id: str, *, created_at: datetime = _NOW) -> OAuthClient:
    return OAuthClient(
        client_id=client_id,
        client_name="agente",
        redirect_uris=(RedirectUri("http://127.0.0.1:1/callback"),),
        token_endpoint_auth_method=TokenEndpointAuthMethod.NONE,
        client_secret_hash=None,
        grant_types=("authorization_code",),
        requested_scope=ScopeSet.parse("ads:read"),
        created_at=created_at,
    )


# Nit de la revision de seguridad final (16-sep): base lo bastante vieja
# para que TODOS los clientes de estas dos pruebas superen
# `AUTHORIZATION_REQUEST_TTL` (10 min) y sigan siendo desalojables tras el
# cambio -- con margen (49 segundos de deriva por indice) para que el mas
# nuevo del lote siga siendo mas viejo que el corte.
_STALE_BASE = _NOW - AUTHORIZATION_REQUEST_TTL - timedelta(minutes=1)


async def test_registration_at_the_cap_evicts_the_oldest_unconsented_client() -> None:
    """M3: la 51ª registración (con el tope en 50) tiene EXITO, y el
    cliente #1 (el mas antiguo) desaparece -- DCR abierta a Internet nunca
    se convierte en un cerrojo permanente para un atacante que rellena el
    cupo."""
    existing = [
        _unconsented_client(f"client-{i}", created_at=_STALE_BASE + timedelta(seconds=i))
        for i in range(MAX_UNCONSENTED_CLIENTS)
    ]
    clients = InMemoryClientRepository(existing)
    use_case = _use_case(clients)

    registered = await use_case.execute(_registration())

    assert await clients.get_by_id(registered.client.id) is not None
    assert await clients.get_by_id("client-0") is None
    assert await clients.count_unconsented() == MAX_UNCONSENTED_CLIENTS


async def test_registration_never_evicts_a_trusted_client() -> None:
    """Un cliente ya consentido (TRUSTED, `last_seen_at` fijado) no cuenta
    como "sin consentir" y nunca se desaloja, aunque sea el mas antiguo."""
    trusted = _unconsented_client("trusted-client", created_at=_NOW - timedelta(days=1))
    trusted.mark_trusted(now=_NOW)
    unconsented = [
        _unconsented_client(f"client-{i}", created_at=_STALE_BASE + timedelta(seconds=i))
        for i in range(MAX_UNCONSENTED_CLIENTS)
    ]
    clients = InMemoryClientRepository([trusted, *unconsented])
    use_case = _use_case(clients)

    await use_case.execute(_registration())

    assert await clients.get_by_id("trusted-client") is not None
    assert await clients.get_by_id("client-0") is None


async def test_registration_at_the_cap_spares_a_client_within_ttl() -> None:
    """Nit de la revision de seguridad final (16-sep): un REGISTERED de
    hace 1 minuto puede tener una `AuthorizationRequest` PENDING en curso
    (vive hasta `AUTHORIZATION_REQUEST_TTL`, 10 min) -- desalojarlo la
    tumbaria a medias via CASCADE. El registro nuevo igual tiene exito: el
    techo duro (10x), no la eviccion, es el freno de verdad."""
    existing = [
        _unconsented_client(f"client-{i}", created_at=_NOW - timedelta(minutes=1))
        for i in range(MAX_UNCONSENTED_CLIENTS)
    ]
    clients = InMemoryClientRepository(existing)
    use_case = _use_case(clients)

    registered = await use_case.execute(_registration())

    assert await clients.get_by_id(registered.client.id) is not None
    assert await clients.get_by_id("client-0") is not None
    assert await clients.count_unconsented() == MAX_UNCONSENTED_CLIENTS + 1


async def test_registration_at_the_cap_evicts_a_client_past_ttl() -> None:
    existing = [
        _unconsented_client(f"client-{i}", created_at=_NOW - timedelta(minutes=11))
        for i in range(MAX_UNCONSENTED_CLIENTS)
    ]
    clients = InMemoryClientRepository(existing)
    use_case = _use_case(clients)

    registered = await use_case.execute(_registration())

    assert await clients.get_by_id(registered.client.id) is not None
    assert await clients.get_by_id("client-0") is None
    assert await clients.count_unconsented() == MAX_UNCONSENTED_CLIENTS


async def test_registration_rejected_at_the_hard_ceiling() -> None:
    """El techo DURO (10x el tope) si rechaza -- freno real si el
    desalojo no pudiera seguir el ritmo."""
    existing = [
        _unconsented_client(f"client-{i}", created_at=_NOW + timedelta(seconds=i))
        for i in range(MAX_UNCONSENTED_CLIENTS_HARD_CEILING)
    ]
    clients = InMemoryClientRepository(existing)
    use_case = _use_case(clients)

    with pytest.raises(TooManyUnconsentedClientsError):
        await use_case.execute(_registration())
