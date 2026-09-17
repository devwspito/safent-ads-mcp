"""`StartAuthorization` (contracts/oauth.md §4, tasks.md T005): crea la
`AuthorizationRequest` PENDING; no emite codigo. Rechaza cliente
desconocido, `redirect_uri` no registrada, `resource` no canonico y el
tope de solicitudes pendientes por cliente (threat-model.md C-58)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from safent_ads.mcp_oauth.application.errors import (
    InvalidTargetError,
    RedirectUriMismatchError,
    TooManyPendingAuthorizationsError,
    UnknownClientError,
)
from safent_ads.mcp_oauth.application.start_authorization import (
    AuthorizationRequestDraft,
    StartAuthorization,
)
from safent_ads.mcp_oauth.domain.authorization import AuthorizationRequestState
from safent_ads.mcp_oauth.domain.client import OAuthClient, RedirectUri, TokenEndpointAuthMethod
from safent_ads.mcp_oauth.domain.errors import InvalidRedirectUriError
from safent_ads.mcp_oauth.domain.scope import ScopeSet
from safent_ads.mcp_oauth.infrastructure.fakes import (
    InMemoryAuthorizationRequestRepository,
    InMemoryClientRepository,
)
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import UuidIdGenerator

_NOW = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
_PUBLIC_BASE_URL = "https://ads.example.com"
_RESOURCE = f"{_PUBLIC_BASE_URL}/mcp"
_CODE_CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
_REGISTERED_REDIRECT_URI = "http://127.0.0.1:43123/callback"


def _client() -> OAuthClient:
    return OAuthClient(
        client_id="client-1",
        client_name="Claude Code",
        redirect_uris=(RedirectUri(_REGISTERED_REDIRECT_URI),),
        token_endpoint_auth_method=TokenEndpointAuthMethod.NONE,
        client_secret_hash=None,
        grant_types=("authorization_code", "refresh_token"),
        requested_scope=ScopeSet.parse("ads:read"),
        created_at=_NOW,
    )


def _draft(**overrides: object) -> AuthorizationRequestDraft:
    defaults: dict[str, object] = {
        "client_id": "client-1",
        "redirect_uri": _REGISTERED_REDIRECT_URI,
        "code_challenge": _CODE_CHALLENGE,
        "client_state": "xyz",
        "requested_scope": "ads:read",
        "resource": _RESOURCE,
    }
    defaults.update(overrides)
    return AuthorizationRequestDraft(**defaults)  # type: ignore[arg-type]


def _use_case(
    clients: InMemoryClientRepository, requests: InMemoryAuthorizationRequestRepository
) -> StartAuthorization:
    return StartAuthorization(
        clients=clients,
        authorization_requests=requests,
        id_generator=UuidIdGenerator(),
        clock=FixedClock(_NOW),
        public_base_url=_PUBLIC_BASE_URL,
    )


async def test_creates_a_pending_request_without_a_code() -> None:
    clients = InMemoryClientRepository([_client()])
    requests = InMemoryAuthorizationRequestRepository()
    use_case = _use_case(clients, requests)

    request = await use_case.execute(_draft())

    assert request.state is AuthorizationRequestState.PENDING
    assert request.code_hash is None
    assert await requests.get_by_id(request.id) is request


async def test_rejects_unknown_client() -> None:
    clients = InMemoryClientRepository()
    requests = InMemoryAuthorizationRequestRepository()
    use_case = _use_case(clients, requests)

    with pytest.raises(UnknownClientError):
        await use_case.execute(_draft())


class _DomainViolatingClientRepository:
    """I-2 (revision de seguridad 17-sep): fila legado que ya no cumple
    una regla del dominio (destino remoto de D-11) -- `get_by_id` levanta
    `DomainError` en vez de devolver un `OAuthClient`. `StartAuthorization`
    ya trataba esto como cliente desconocido (`get_client_tolerating_
    domain_violations`); este test lo deja fijado."""

    async def get_by_id(self, client_id: str) -> OAuthClient | None:
        del client_id
        raise InvalidRedirectUriError("redirect_uri remota, invalida desde D-11")


async def test_rejects_a_client_whose_row_violates_the_domain() -> None:
    requests = InMemoryAuthorizationRequestRepository()
    use_case = _use_case(_DomainViolatingClientRepository(), requests)  # type: ignore[arg-type]

    with pytest.raises(UnknownClientError):
        await use_case.execute(_draft())


async def test_rejects_unregistered_redirect_uri() -> None:
    clients = InMemoryClientRepository([_client()])
    requests = InMemoryAuthorizationRequestRepository()
    use_case = _use_case(clients, requests)

    with pytest.raises(RedirectUriMismatchError):
        await use_case.execute(_draft(redirect_uri="https://evil.example/callback"))


async def test_rejects_non_canonical_resource() -> None:
    clients = InMemoryClientRepository([_client()])
    requests = InMemoryAuthorizationRequestRepository()
    use_case = _use_case(clients, requests)

    with pytest.raises(InvalidTargetError):
        await use_case.execute(_draft(resource="https://other.example/mcp"))


async def test_rejects_when_client_has_too_many_pending_requests() -> None:
    clients = InMemoryClientRepository([_client()])
    requests = InMemoryAuthorizationRequestRepository()
    use_case = _use_case(clients, requests)
    for _ in range(5):
        await use_case.execute(_draft())

    with pytest.raises(TooManyPendingAuthorizationsError):
        await use_case.execute(_draft())
