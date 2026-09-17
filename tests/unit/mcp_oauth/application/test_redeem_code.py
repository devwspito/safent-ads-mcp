"""`RedeemCode` (contracts/oauth.md §6, threat-model.md C-38): canjea el
codigo una sola vez; el replay revoca la concesion emitida desde el;
`resource`/`redirect_uri`/`client_id`/PKCE deben coincidir con la
solicitud consentida."""

from __future__ import annotations

import base64
import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from safent_ads.mcp_oauth.application.errors import (
    ClientMismatchError,
    InvalidTargetError,
    PkceVerificationError,
    RedirectUriMismatchError,
    UnknownAuthorizationCodeError,
)
from safent_ads.mcp_oauth.application.grant_consent import ApproveConsent
from safent_ads.mcp_oauth.application.redeem_code import RedeemCode
from safent_ads.mcp_oauth.application.start_authorization import (
    AuthorizationRequestDraft,
    StartAuthorization,
)
from safent_ads.mcp_oauth.domain.client import OAuthClient, RedirectUri, TokenEndpointAuthMethod
from safent_ads.mcp_oauth.domain.errors import (
    AuthorizationRequestExpiredError,
    CodeAlreadyRedeemedError,
)
from safent_ads.mcp_oauth.domain.scope import ScopeSet
from safent_ads.mcp_oauth.infrastructure.fakes import (
    InMemoryAuthorizationRequestRepository,
    InMemoryClientRepository,
    InMemoryGrantRepository,
)
from safent_ads.mcp_oauth.infrastructure.secrets_token_factory import SecretsOpaqueTokenFactory
from safent_ads.mcp_oauth.infrastructure.sha256_token_hasher import Sha256TokenHasher
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import UuidIdGenerator

_NOW = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
_PUBLIC_BASE_URL = "https://ads.example.com"
_RESOURCE = f"{_PUBLIC_BASE_URL}/mcp"
_REDIRECT_URI = "http://127.0.0.1:43123/callback"
_CODE_VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"


def _code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _client() -> OAuthClient:
    return OAuthClient(
        client_id="client-1",
        client_name="Claude Code",
        redirect_uris=(RedirectUri(_REDIRECT_URI),),
        token_endpoint_auth_method=TokenEndpointAuthMethod.NONE,
        client_secret_hash=None,
        grant_types=("authorization_code", "refresh_token"),
        requested_scope=ScopeSet.parse("ads:read"),
        created_at=_NOW,
    )


class _Fixture:
    def __init__(self) -> None:
        self.clients = InMemoryClientRepository([_client()])
        self.authorization_requests = InMemoryAuthorizationRequestRepository()
        self.grants = InMemoryGrantRepository()
        self.clock = FixedClock(_NOW)
        self.hasher = Sha256TokenHasher()
        self.factory = SecretsOpaqueTokenFactory()
        self.id_generator = UuidIdGenerator()

    async def consented_code(
        self, *, code_verifier: str = _CODE_VERIFIER, owner_id: uuid.UUID | None = None
    ) -> tuple[str, uuid.UUID]:
        starter = StartAuthorization(
            clients=self.clients,
            authorization_requests=self.authorization_requests,
            id_generator=self.id_generator,
            clock=self.clock,
            public_base_url=_PUBLIC_BASE_URL,
        )
        request = await starter.execute(
            AuthorizationRequestDraft(
                client_id="client-1",
                redirect_uri=_REDIRECT_URI,
                code_challenge=_code_challenge(code_verifier),
                client_state="xyz",
                requested_scope="ads:read",
                resource=_RESOURCE,
            )
        )
        approver = ApproveConsent(
            authorization_requests=self.authorization_requests,
            clients=self.clients,
            token_hasher=self.hasher,
            token_factory=self.factory,
            clock=self.clock,
        )
        owner = owner_id or uuid.uuid4()
        approved = await approver.execute(txn_id=request.id, owner_id=owner)
        return approved.code, owner

    def use_case(self) -> RedeemCode:
        return RedeemCode(
            authorization_requests=self.authorization_requests,
            grants=self.grants,
            token_hasher=self.hasher,
            token_factory=self.factory,
            id_generator=self.id_generator,
            clock=self.clock,
        )


async def test_redeems_a_consented_code_for_a_token_pair() -> None:
    fixture = _Fixture()
    code, owner_id = await fixture.consented_code()

    pair = await fixture.use_case().execute(
        code=code,
        redirect_uri=_REDIRECT_URI,
        client_id="client-1",
        code_verifier=_CODE_VERIFIER,
        resource=_RESOURCE,
    )

    assert pair.access_token
    assert pair.refresh_token
    assert pair.access_token != pair.refresh_token
    grants = await fixture.grants.list_active_for_owner(owner_id)
    assert len(grants) == 1


async def test_redeeming_the_same_code_twice_revokes_the_issued_grant() -> None:
    fixture = _Fixture()
    code, owner_id = await fixture.consented_code()
    use_case = fixture.use_case()

    await use_case.execute(
        code=code,
        redirect_uri=_REDIRECT_URI,
        client_id="client-1",
        code_verifier=_CODE_VERIFIER,
        resource=_RESOURCE,
    )

    with pytest.raises(CodeAlreadyRedeemedError):
        await use_case.execute(
            code=code,
            redirect_uri=_REDIRECT_URI,
            client_id="client-1",
            code_verifier=_CODE_VERIFIER,
            resource=_RESOURCE,
        )

    grants = await fixture.grants.list_active_for_owner(owner_id)
    assert grants == []


async def test_unknown_code_raises() -> None:
    fixture = _Fixture()

    with pytest.raises(UnknownAuthorizationCodeError):
        await fixture.use_case().execute(
            code="never-issued",  # noqa: S106 - fixture, no secreto real
            redirect_uri=_REDIRECT_URI,
            client_id="client-1",
            code_verifier=_CODE_VERIFIER,
            resource=_RESOURCE,
        )


async def test_wrong_client_id_raises() -> None:
    fixture = _Fixture()
    code, _ = await fixture.consented_code()

    with pytest.raises(ClientMismatchError):
        await fixture.use_case().execute(
            code=code,
            redirect_uri=_REDIRECT_URI,
            client_id="someone-else",
            code_verifier=_CODE_VERIFIER,
            resource=_RESOURCE,
        )


async def test_wrong_redirect_uri_raises() -> None:
    fixture = _Fixture()
    code, _ = await fixture.consented_code()

    with pytest.raises(RedirectUriMismatchError):
        await fixture.use_case().execute(
            code=code,
            redirect_uri="http://127.0.0.1:43123/other",
            client_id="client-1",
            code_verifier=_CODE_VERIFIER,
            resource=_RESOURCE,
        )


async def test_wrong_code_verifier_raises() -> None:
    fixture = _Fixture()
    code, _ = await fixture.consented_code()

    with pytest.raises(PkceVerificationError):
        await fixture.use_case().execute(
            code=code,
            redirect_uri=_REDIRECT_URI,
            client_id="client-1",
            code_verifier="wrong-verifier-wrong-verifier-wrong-verif",
            resource=_RESOURCE,
        )


async def test_wrong_resource_raises_invalid_target() -> None:
    fixture = _Fixture()
    code, _ = await fixture.consented_code()

    with pytest.raises(InvalidTargetError):
        await fixture.use_case().execute(
            code=code,
            redirect_uri=_REDIRECT_URI,
            client_id="client-1",
            code_verifier=_CODE_VERIFIER,
            resource="https://other.example/mcp",
        )


async def test_execute_with_pkce_verified_upstream_skips_the_pkce_check() -> None:
    """`sdk_provider.py::exchange_authorization_code` no recibe `code_verifier`
    (el SDK ya lo verifico, ver docstring del modulo): el mismo canje debe
    completarse sin el."""
    fixture = _Fixture()
    code, owner_id = await fixture.consented_code()

    pair = await fixture.use_case().execute_with_pkce_verified_upstream(
        code=code, redirect_uri=_REDIRECT_URI, client_id="client-1", resource=_RESOURCE
    )

    assert pair.access_token
    grants = await fixture.grants.list_active_for_owner(owner_id)
    assert len(grants) == 1


async def test_execute_with_pkce_verified_upstream_still_checks_resource() -> None:
    fixture = _Fixture()
    code, _ = await fixture.consented_code()

    with pytest.raises(InvalidTargetError):
        await fixture.use_case().execute_with_pkce_verified_upstream(
            code=code,
            redirect_uri=_REDIRECT_URI,
            client_id="client-1",
            resource="https://other.example/mcp",
        )


async def test_expired_code_raises() -> None:
    fixture = _Fixture()
    code, _ = await fixture.consented_code()
    fixture.clock.advance_to(_NOW + timedelta(seconds=61))

    with pytest.raises(AuthorizationRequestExpiredError):
        await fixture.use_case().execute(
            code=code,
            redirect_uri=_REDIRECT_URI,
            client_id="client-1",
            code_verifier=_CODE_VERIFIER,
            resource=_RESOURCE,
        )
