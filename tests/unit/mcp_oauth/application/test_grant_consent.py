"""`ApproveConsent`/`DenyConsent` (contracts/oauth.md §5, tasks.md T005):
aprobar fija `owner_id` y mina un codigo canjeable; denegar cierra la
solicitud sin fijar propietario. Ambos exigen que el `txn_id` exista."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from safent_ads.mcp_oauth.application.errors import (
    AuthorizationRequestNotFoundError,
    UnknownClientError,
)
from safent_ads.mcp_oauth.application.grant_consent import ApproveConsent, DenyConsent
from safent_ads.mcp_oauth.domain.authorization import (
    AuthorizationRequest,
    AuthorizationRequestState,
)
from safent_ads.mcp_oauth.domain.client import (
    OAuthClient,
    OAuthClientState,
    RedirectUri,
    TokenEndpointAuthMethod,
)
from safent_ads.mcp_oauth.domain.errors import InvalidRedirectUriError
from safent_ads.mcp_oauth.domain.resource import ResourceIndicator
from safent_ads.mcp_oauth.domain.scope import ScopeSet
from safent_ads.mcp_oauth.infrastructure.fakes import (
    InMemoryAuthorizationRequestRepository,
    InMemoryClientRepository,
)
from safent_ads.mcp_oauth.infrastructure.secrets_token_factory import SecretsOpaqueTokenFactory
from safent_ads.mcp_oauth.infrastructure.sha256_token_hasher import Sha256TokenHasher
from safent_ads.shared.clock import FixedClock

_NOW = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
_CODE_CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
_RESOURCE = ResourceIndicator("https://ads.example.com/mcp")


def _registered_client(client_id: str = "client-1") -> OAuthClient:
    return OAuthClient(
        client_id=client_id,
        client_name="Claude Code",
        redirect_uris=(RedirectUri("http://127.0.0.1:43123/callback"),),
        token_endpoint_auth_method=TokenEndpointAuthMethod.NONE,
        client_secret_hash=None,
        grant_types=("authorization_code", "refresh_token"),
        requested_scope=ScopeSet.parse("ads:read"),
        created_at=_NOW,
    )


async def _pending_request(
    repository: InMemoryAuthorizationRequestRepository,
) -> AuthorizationRequest:
    request = AuthorizationRequest(
        txn_id=uuid.uuid4(),
        client_id="client-1",
        redirect_uri="http://127.0.0.1:43123/callback",
        code_challenge=_CODE_CHALLENGE,
        client_state="xyz",
        scope_set=ScopeSet.parse("ads:read"),
        resource=_RESOURCE,
        created_at=_NOW,
        expires_at=_NOW + timedelta(minutes=10),
    )
    await repository.create(request)
    return request


async def test_approve_fixes_owner_and_mints_a_redeemable_code() -> None:
    requests = InMemoryAuthorizationRequestRepository()
    request = await _pending_request(requests)
    clients = InMemoryClientRepository([_registered_client()])
    owner_id = uuid.uuid4()
    use_case = ApproveConsent(
        authorization_requests=requests,
        clients=clients,
        token_hasher=Sha256TokenHasher(),
        token_factory=SecretsOpaqueTokenFactory(),
        clock=FixedClock(_NOW),
    )

    approved = await use_case.execute(txn_id=request.id, owner_id=owner_id)

    assert approved.redirect_uri == request.redirect_uri
    assert approved.client_state == "xyz"
    assert approved.code
    assert request.state is AuthorizationRequestState.CONSENTED
    assert request.owner_id == owner_id


async def test_approve_marks_the_client_as_trusted() -> None:
    """data-model.md: REGISTERED -> (primer consentimiento) TRUSTED --
    sin esto, `prune_stale_clients.py` (T016) podaria un cliente con
    concesiones vivas por parecer "sin consentir"."""
    requests = InMemoryAuthorizationRequestRepository()
    request = await _pending_request(requests)
    client = _registered_client()
    clients = InMemoryClientRepository([client])
    assert client.state is OAuthClientState.REGISTERED
    use_case = ApproveConsent(
        authorization_requests=requests,
        clients=clients,
        token_hasher=Sha256TokenHasher(),
        token_factory=SecretsOpaqueTokenFactory(),
        clock=FixedClock(_NOW),
    )

    await use_case.execute(txn_id=request.id, owner_id=uuid.uuid4())

    trusted = await clients.get_by_id(client.id)
    assert trusted is not None
    assert trusted.state is OAuthClientState.TRUSTED
    assert trusted.last_seen_at == _NOW


async def test_approve_fails_closed_when_the_client_no_longer_resolves() -> None:
    """I-2 (revision de seguridad 17-sep): `sql_client_repository.py`
    declara "autorizar y consentir fallan cerrado" -- un cliente ausente
    (fila borrada, o que ya no cumple una regla del dominio) hace que
    consentir se RECHACE entero, nunca que emita un codigo ni marque la
    solicitud CONSENTED con un cliente que ya no existe de verdad."""
    requests = InMemoryAuthorizationRequestRepository()
    request = await _pending_request(requests)
    clients = InMemoryClientRepository()  # el cliente de la solicitud no esta aqui

    use_case = ApproveConsent(
        authorization_requests=requests,
        clients=clients,
        token_hasher=Sha256TokenHasher(),
        token_factory=SecretsOpaqueTokenFactory(),
        clock=FixedClock(_NOW),
    )

    with pytest.raises(UnknownClientError):
        await use_case.execute(txn_id=request.id, owner_id=uuid.uuid4())

    assert request.state is AuthorizationRequestState.PENDING
    assert request.owner_id is None


class _DomainViolatingClientRepository:
    """Doble que simula una fila legado que ya no cumple una regla del
    dominio (destino remoto de D-11): `get_by_id` levanta `DomainError`
    en vez de devolver un `OAuthClient`, igual que `SqlClientRepository`
    lo haria al reconstruir el value object `RedirectUri`."""

    async def get_by_id(self, client_id: str) -> OAuthClient | None:
        del client_id
        raise InvalidRedirectUriError("redirect_uri remota, invalida desde D-11")

    async def get_many(self, client_ids):  # noqa: ANN001, ANN201 - doble minimo, no se usa aqui
        raise NotImplementedError

    async def save(self, client: OAuthClient) -> None:
        raise NotImplementedError

    async def count_unconsented(self) -> int:
        raise NotImplementedError

    async def evict_oldest_unconsented(self, *, cutoff: datetime) -> None:
        raise NotImplementedError


async def test_approve_fails_closed_when_the_client_row_violates_the_domain() -> None:
    requests = InMemoryAuthorizationRequestRepository()
    request = await _pending_request(requests)
    use_case = ApproveConsent(
        authorization_requests=requests,
        clients=_DomainViolatingClientRepository(),  # type: ignore[arg-type]
        token_hasher=Sha256TokenHasher(),
        token_factory=SecretsOpaqueTokenFactory(),
        clock=FixedClock(_NOW),
    )

    with pytest.raises(UnknownClientError):
        await use_case.execute(txn_id=request.id, owner_id=uuid.uuid4())

    assert request.state is AuthorizationRequestState.PENDING


async def test_approve_unknown_txn_raises() -> None:
    requests = InMemoryAuthorizationRequestRepository()
    use_case = ApproveConsent(
        authorization_requests=requests,
        clients=InMemoryClientRepository(),
        token_hasher=Sha256TokenHasher(),
        token_factory=SecretsOpaqueTokenFactory(),
        clock=FixedClock(_NOW),
    )

    with pytest.raises(AuthorizationRequestNotFoundError):
        await use_case.execute(txn_id=uuid.uuid4(), owner_id=uuid.uuid4())


async def test_deny_closes_the_request_without_an_owner() -> None:
    requests = InMemoryAuthorizationRequestRepository()
    request = await _pending_request(requests)
    use_case = DenyConsent(authorization_requests=requests, clock=FixedClock(_NOW))

    denied = await use_case.execute(txn_id=request.id)

    assert denied.redirect_uri == request.redirect_uri
    assert request.state is AuthorizationRequestState.DENIED
    assert request.owner_id is None


async def test_deny_unknown_txn_raises() -> None:
    requests = InMemoryAuthorizationRequestRepository()
    use_case = DenyConsent(authorization_requests=requests, clock=FixedClock(_NOW))

    with pytest.raises(AuthorizationRequestNotFoundError):
        await use_case.execute(txn_id=uuid.uuid4())
