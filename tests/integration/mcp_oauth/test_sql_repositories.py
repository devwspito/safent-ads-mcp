"""Repositorios SQL de `mcp_oauth` (0035_mcp_oauth, tasks.md T006) contra
Postgres real: `db_session`/`owner_factory` (conftest.py) por transaccion,
deshecha al terminar cada test."""

from __future__ import annotations

import base64
import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.mcp_oauth.application.policy import AUTHORIZATION_REQUEST_TTL
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
from safent_ads.mcp_oauth.domain.errors import (
    AuthorizationRequestExpiredError,
    AuthorizationRequestNotPendingError,
    CodeAlreadyRedeemedError,
)
from safent_ads.mcp_oauth.domain.grant import Grant, IssuedToken, TokenHash, TokenKind, TokenState
from safent_ads.mcp_oauth.domain.resource import ResourceIndicator
from safent_ads.mcp_oauth.domain.scope import ScopeSet
from safent_ads.mcp_oauth.infrastructure.sql_authorization_repository import (
    SqlAuthorizationRequestRepository,
)
from safent_ads.mcp_oauth.infrastructure.sql_client_repository import SqlClientRepository
from safent_ads.mcp_oauth.infrastructure.sql_grant_repository import SqlGrantRepository
from safent_ads.shared.clock import FixedClock
from tests.conftest import OwnerFactory

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
_RESOURCE = ResourceIndicator("https://ads.example.com/mcp")


def _pkce_pair(seed: str) -> tuple[str, str]:
    verifier = (seed * 5)[:43]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _client(client_id: str = "client-1") -> OAuthClient:
    return OAuthClient(
        client_id=client_id,
        client_name="Claude Code",
        redirect_uris=(RedirectUri("http://127.0.0.1:54321/callback"),),
        token_endpoint_auth_method=TokenEndpointAuthMethod.NONE,
        client_secret_hash=None,
        grant_types=("authorization_code", "refresh_token"),
        requested_scope=ScopeSet.parse("ads:read ads:propose"),
        created_at=_NOW,
    )


def _pending_request(client_id: str, txn_id: uuid.UUID | None = None) -> AuthorizationRequest:
    _, challenge = _pkce_pair("v")
    return AuthorizationRequest(
        txn_id=txn_id or uuid.uuid4(),
        client_id=client_id,
        redirect_uri="http://127.0.0.1:54321/callback",
        code_challenge=challenge,
        client_state="xyz",
        scope_set=ScopeSet.parse("ads:read"),
        resource=_RESOURCE,
        created_at=_NOW,
        expires_at=_NOW + timedelta(minutes=10),
    )


def _grant(
    owner_id: uuid.UUID,
    txn_id: uuid.UUID,
    client_id: str = "client-1",
    *,
    access_hash: str = "a" * 64,
    refresh_hash: str = "b" * 64,
) -> Grant:
    access = IssuedToken(
        token_hash=TokenHash(access_hash), kind=TokenKind.ACCESS, issued_at=_NOW,
        expires_at=_NOW + timedelta(minutes=60),
    )
    refresh = IssuedToken(
        token_hash=TokenHash(refresh_hash), kind=TokenKind.REFRESH, issued_at=_NOW,
        expires_at=_NOW + timedelta(days=30),
    )
    return Grant(
        grant_id=uuid.uuid4(),
        authorization_request_id=txn_id,
        owner_id=owner_id,
        client_id=client_id,
        scope_set=ScopeSet.parse("ads:read"),
        resource=_RESOURCE,
        created_at=_NOW,
        tokens=(access, refresh),
    )


class TestSqlClientRepository:
    async def test_save_and_get_by_id_round_trip(self, db_session: AsyncSession) -> None:
        repository = SqlClientRepository(db_session)
        client = _client()

        await repository.save(client)
        loaded = await repository.get_by_id(client.id)

        assert loaded is not None
        assert loaded.id == client.id
        assert loaded.client_name == "Claude Code"
        assert [str(u) for u in loaded.redirect_uris] == [str(u) for u in client.redirect_uris]
        assert loaded.grant_types == client.grant_types
        assert str(loaded.requested_scope) == "ads:propose ads:read"
        assert loaded.state is OAuthClientState.REGISTERED

    async def test_unknown_client_returns_none(self, db_session: AsyncSession) -> None:
        repository = SqlClientRepository(db_session)

        assert await repository.get_by_id("does-not-exist") is None

    async def test_mark_trusted_persists_as_trusted_and_leaves_the_count(
        self, db_session: AsyncSession
    ) -> None:
        repository = SqlClientRepository(db_session)
        client = _client()
        await repository.save(client)

        client.mark_trusted(_NOW)
        await repository.save(client)
        loaded = await repository.get_by_id(client.id)

        assert loaded is not None
        assert loaded.state is OAuthClientState.TRUSTED
        assert loaded.last_seen_at == _NOW

    async def test_count_unconsented_only_counts_registered_clients(
        self, db_session: AsyncSession
    ) -> None:
        repository = SqlClientRepository(db_session)
        registered = _client("client-registered")
        trusted = _client("client-trusted")
        trusted.mark_trusted(_NOW)
        await repository.save(registered)
        await repository.save(trusted)

        assert await repository.count_unconsented() == 1

    async def test_evict_oldest_unconsented_deletes_only_the_oldest_registered_client(
        self, db_session: AsyncSession
    ) -> None:
        """M3 (threat-model.md C-42): entre dos REGISTERED, borra el de
        `created_at` mas antiguo; uno TRUSTED de por medio nunca es
        candidato aunque sea aun mas antiguo."""
        repository = SqlClientRepository(db_session)
        oldest = OAuthClient(
            client_id="client-oldest",
            client_name="oldest",
            redirect_uris=(RedirectUri("http://127.0.0.1:1/callback"),),
            token_endpoint_auth_method=TokenEndpointAuthMethod.NONE,
            client_secret_hash=None,
            grant_types=("authorization_code",),
            requested_scope=ScopeSet.parse("ads:read"),
            created_at=_NOW - timedelta(days=2),
        )
        newer = OAuthClient(
            client_id="client-newer",
            client_name="newer",
            redirect_uris=(RedirectUri("http://127.0.0.1:1/callback"),),
            token_endpoint_auth_method=TokenEndpointAuthMethod.NONE,
            client_secret_hash=None,
            grant_types=("authorization_code",),
            requested_scope=ScopeSet.parse("ads:read"),
            created_at=_NOW,
        )
        oldest_trusted = OAuthClient(
            client_id="client-oldest-trusted",
            client_name="oldest trusted",
            redirect_uris=(RedirectUri("http://127.0.0.1:1/callback"),),
            token_endpoint_auth_method=TokenEndpointAuthMethod.NONE,
            client_secret_hash=None,
            grant_types=("authorization_code",),
            requested_scope=ScopeSet.parse("ads:read"),
            created_at=_NOW - timedelta(days=30),
        )
        oldest_trusted.mark_trusted(_NOW - timedelta(days=29))
        await repository.save(oldest)
        await repository.save(newer)
        await repository.save(oldest_trusted)

        await repository.evict_oldest_unconsented(cutoff=_NOW)

        assert await repository.get_by_id("client-oldest") is None
        assert await repository.get_by_id("client-newer") is not None
        assert await repository.get_by_id("client-oldest-trusted") is not None

    async def test_evict_oldest_unconsented_is_a_no_op_without_any_registered_client(
        self, db_session: AsyncSession
    ) -> None:
        repository = SqlClientRepository(db_session)

        await repository.evict_oldest_unconsented(cutoff=_NOW)  # no debe lanzar

    async def test_evict_oldest_unconsented_spares_a_client_within_ttl(
        self, db_session: AsyncSession
    ) -> None:
        """Nit de la revision de seguridad final (16-sep): un REGISTERED de
        hace 1 minuto puede tener una `AuthorizationRequest` PENDING en
        vuelo (vive hasta `AUTHORIZATION_REQUEST_TTL`, 10 min) --
        desalojarlo la tumbaria a medias via CASCADE."""
        repository = SqlClientRepository(db_session)
        recent = OAuthClient(
            client_id="client-recent",
            client_name="recent",
            redirect_uris=(RedirectUri("http://127.0.0.1:1/callback"),),
            token_endpoint_auth_method=TokenEndpointAuthMethod.NONE,
            client_secret_hash=None,
            grant_types=("authorization_code",),
            requested_scope=ScopeSet.parse("ads:read"),
            created_at=_NOW - timedelta(minutes=1),
        )
        await repository.save(recent)

        await repository.evict_oldest_unconsented(cutoff=_NOW - AUTHORIZATION_REQUEST_TTL)

        assert await repository.get_by_id("client-recent") is not None

    async def test_evict_oldest_unconsented_evicts_a_client_past_the_ttl(
        self, db_session: AsyncSession
    ) -> None:
        repository = SqlClientRepository(db_session)
        stale = OAuthClient(
            client_id="client-stale",
            client_name="stale",
            redirect_uris=(RedirectUri("http://127.0.0.1:1/callback"),),
            token_endpoint_auth_method=TokenEndpointAuthMethod.NONE,
            client_secret_hash=None,
            grant_types=("authorization_code",),
            requested_scope=ScopeSet.parse("ads:read"),
            created_at=_NOW - timedelta(minutes=10, seconds=1),
        )
        await repository.save(stale)

        await repository.evict_oldest_unconsented(cutoff=_NOW - AUTHORIZATION_REQUEST_TTL)

        assert await repository.get_by_id("client-stale") is None

    async def test_get_many_returns_only_the_requested_clients_that_exist(
        self, db_session: AsyncSession
    ) -> None:
        """I7 de la revision de seguridad (16-sep): `ListGrants` la usa
        para resolver el cliente de varias concesiones con una sola
        consulta -- un `client_id` que ya no existe simplemente no
        aparece en el resultado, igual que `get_by_id()` devuelve `None`."""
        repository = SqlClientRepository(db_session)
        await repository.save(_client("client-a"))
        await repository.save(_client("client-b"))

        found = await repository.get_many(["client-a", "client-b", "client-does-not-exist"])

        assert set(found.keys()) == {"client-a", "client-b"}
        assert found["client-a"].id == "client-a"

    async def test_get_many_with_no_ids_returns_empty_without_querying(
        self, db_session: AsyncSession
    ) -> None:
        repository = SqlClientRepository(db_session)

        assert await repository.get_many([]) == {}


class TestSqlAuthorizationRequestRepository:
    async def test_create_and_get_by_id_round_trip(self, db_session: AsyncSession) -> None:
        clients = SqlClientRepository(db_session)
        await clients.save(_client())
        requests = SqlAuthorizationRequestRepository(db_session, clock=FixedClock(_NOW))
        request = _pending_request("client-1")

        await requests.create(request)
        loaded = await requests.get_by_id(request.id)

        assert loaded is not None
        assert loaded.state is AuthorizationRequestState.PENDING
        assert loaded.client_id == "client-1"
        assert loaded.code_hash is None

    async def test_count_pending_for_client(self, db_session: AsyncSession) -> None:
        clients = SqlClientRepository(db_session)
        await clients.save(_client())
        requests = SqlAuthorizationRequestRepository(db_session, clock=FixedClock(_NOW))
        await requests.create(_pending_request("client-1"))
        await requests.create(_pending_request("client-1"))

        assert await requests.count_pending_for_client("client-1") == 2
        assert await requests.count_pending_for_client("someone-else") == 0

    async def test_consent_then_redeem_round_trip(
        self, db_session: AsyncSession, owner_factory: OwnerFactory
    ) -> None:
        clients = SqlClientRepository(db_session)
        await clients.save(_client())
        requests = SqlAuthorizationRequestRepository(db_session, clock=FixedClock(_NOW))
        request = _pending_request("client-1")
        await requests.create(request)
        owner_id = await owner_factory.create()
        code_hash = TokenHash("c" * 64)

        request.consent(
            owner_id=owner_id, code_hash=code_hash, now=_NOW, code_ttl=timedelta(seconds=60)
        )
        await requests.save(request)
        by_hash = await requests.get_by_code_hash(code_hash)
        assert by_hash is not None
        assert by_hash.state is AuthorizationRequestState.CONSENTED
        assert by_hash.owner_id == owner_id

        by_hash.redeem(_NOW + timedelta(seconds=1))
        await requests.save(by_hash)
        redeemed = await requests.get_by_id(request.id)

        assert redeemed is not None
        assert redeemed.state is AuthorizationRequestState.REDEEMED
        assert redeemed.code_hash == code_hash

    async def test_redeeming_an_already_redeemed_row_raises(
        self, db_session: AsyncSession, owner_factory: OwnerFactory
    ) -> None:
        clients = SqlClientRepository(db_session)
        await clients.save(_client())
        requests = SqlAuthorizationRequestRepository(db_session, clock=FixedClock(_NOW))
        request = _pending_request("client-1")
        await requests.create(request)
        owner_id = await owner_factory.create()
        code_hash = TokenHash("d" * 64)
        request.consent(
            owner_id=owner_id, code_hash=code_hash, now=_NOW, code_ttl=timedelta(seconds=60)
        )
        await requests.save(request)

        first = await requests.get_by_code_hash(code_hash)
        second = await requests.get_by_code_hash(code_hash)
        assert first is not None
        assert second is not None
        first.redeem(_NOW + timedelta(seconds=1))
        second.redeem(_NOW + timedelta(seconds=1))
        await requests.save(first)

        with pytest.raises(CodeAlreadyRedeemedError):
            await requests.save(second)

    async def test_redeeming_a_consented_row_past_its_code_ttl_raises_expired_not_replayed(
        self, db_session: AsyncSession, owner_factory: OwnerFactory
    ) -> None:
        """M5 de la revision de seguridad (16-sep): la fila en Postgres
        SIGUE en CONSENTED (nunca se canjeo) pero su codigo ya supero su
        TTL de 60s -- el motivo real es la expiracion, no un replay.

        `AuthorizationRequest.redeem()` ya rechaza esto a nivel de dominio
        ANTES de llegar aqui (el mismo `_reject_if_expired`), asi que la
        unica forma de ejercitar la guarda nueva de la capa SQL es
        construir a mano un `AuthorizationRequest` que ya declara
        REDEEMED -- el escenario real es una segunda transaccion que gana
        la carrera de `UPDATE ... WHERE state = 'CONSENTED'` justo cuando
        el plazo ya paso: antes de este fix, 0 filas afectadas colapsaba
        siempre en `CodeAlreadyRedeemedError`, sin importar la causa real."""
        clients = SqlClientRepository(db_session)
        await clients.save(_client())
        requests = SqlAuthorizationRequestRepository(db_session, clock=FixedClock(_NOW))
        request = _pending_request("client-1")
        await requests.create(request)
        owner_id = await owner_factory.create()
        code_hash = TokenHash("e" * 64)
        request.consent(
            owner_id=owner_id, code_hash=code_hash, now=_NOW, code_ttl=timedelta(seconds=60)
        )
        await requests.save(request)

        past_ttl = _NOW + timedelta(seconds=61)
        hand_built_redeemed = AuthorizationRequest(
            txn_id=request.id,
            client_id=request.client_id,
            redirect_uri=request.redirect_uri,
            code_challenge=request.code_challenge,
            client_state=request.client_state,
            scope_set=request.scope_set,
            resource=request.resource,
            created_at=request.created_at,
            expires_at=request.code_expires_at,
            state=AuthorizationRequestState.REDEEMED,
            code_hash=code_hash,
            owner_id=owner_id,
            consented_at=_NOW,
            redeemed_at=past_ttl,
            code_expires_at=request.code_expires_at,
        )
        expired_requests = SqlAuthorizationRequestRepository(
            db_session, clock=FixedClock(past_ttl)
        )

        with pytest.raises(AuthorizationRequestExpiredError):
            await expired_requests.save(hand_built_redeemed)

        still_consented = await requests.get_by_id(request.id)
        assert still_consented is not None
        assert still_consented.state is AuthorizationRequestState.CONSENTED
        assert still_consented.redeemed_at is None

    async def test_deny_transitions_pending_to_denied(self, db_session: AsyncSession) -> None:
        clients = SqlClientRepository(db_session)
        await clients.save(_client())
        requests = SqlAuthorizationRequestRepository(db_session, clock=FixedClock(_NOW))
        request = _pending_request("client-1")
        await requests.create(request)

        request.deny(_NOW)
        await requests.save(request)
        loaded = await requests.get_by_id(request.id)

        assert loaded is not None
        assert loaded.state is AuthorizationRequestState.DENIED

    async def test_saving_expired_over_a_redeemed_row_changes_nothing(
        self, db_session: AsyncSession, owner_factory: OwnerFactory
    ) -> None:
        """Nit de la revision de seguridad final (16-sep):
        `_UPDATE_TO_EXPIRED_SQL` exige `state IN ('PENDING', 'CONSENTED')`
        -- una transicion a EXPIRED construida sobre una fila que YA es
        REDEEMED (p.ej. una poda en carrera con un canje) nunca debe
        sobrescribirla."""
        clients = SqlClientRepository(db_session)
        await clients.save(_client())
        requests = SqlAuthorizationRequestRepository(db_session, clock=FixedClock(_NOW))
        request = _pending_request("client-1")
        await requests.create(request)
        owner_id = await owner_factory.create()
        code_hash = TokenHash("f" * 64)
        request.consent(
            owner_id=owner_id, code_hash=code_hash, now=_NOW, code_ttl=timedelta(seconds=60)
        )
        await requests.save(request)
        request.redeem(_NOW + timedelta(seconds=1))
        await requests.save(request)

        hand_built_expired = AuthorizationRequest(
            txn_id=request.id,
            client_id=request.client_id,
            redirect_uri=request.redirect_uri,
            code_challenge=request.code_challenge,
            client_state=request.client_state,
            scope_set=request.scope_set,
            resource=request.resource,
            created_at=request.created_at,
            expires_at=request.code_expires_at,
            state=AuthorizationRequestState.EXPIRED,
            code_hash=code_hash,
            owner_id=owner_id,
            consented_at=_NOW,
            redeemed_at=request.redeemed_at,
            code_expires_at=request.code_expires_at,
        )

        with pytest.raises(AuthorizationRequestNotPendingError):
            await requests.save(hand_built_expired)

        still_redeemed = await requests.get_by_id(request.id)
        assert still_redeemed is not None
        assert still_redeemed.state is AuthorizationRequestState.REDEEMED
        assert still_redeemed.code_hash == code_hash


class TestSqlGrantRepository:
    async def test_create_and_get_by_id_round_trip(
        self, db_session: AsyncSession, owner_factory: OwnerFactory
    ) -> None:
        clients = SqlClientRepository(db_session)
        await clients.save(_client())
        requests = SqlAuthorizationRequestRepository(db_session, clock=FixedClock(_NOW))
        request = _pending_request("client-1")
        await requests.create(request)
        owner_id = await owner_factory.create()
        grants = SqlGrantRepository(db_session)
        grant = _grant(owner_id, request.id)

        await grants.create(grant)
        loaded = await grants.get_by_id(grant.id)

        assert loaded is not None
        assert loaded.owner_id == owner_id
        assert loaded.authorization_request_id == request.id
        assert {t.kind for t in loaded.tokens} == {TokenKind.ACCESS, TokenKind.REFRESH}
        assert all(t.state is TokenState.ACTIVE for t in loaded.tokens)

    async def test_get_by_token_hash_and_by_authorization_request_id(
        self, db_session: AsyncSession, owner_factory: OwnerFactory
    ) -> None:
        clients = SqlClientRepository(db_session)
        await clients.save(_client())
        requests = SqlAuthorizationRequestRepository(db_session, clock=FixedClock(_NOW))
        request = _pending_request("client-1")
        await requests.create(request)
        owner_id = await owner_factory.create()
        grants = SqlGrantRepository(db_session)
        grant = _grant(owner_id, request.id)
        await grants.create(grant)

        by_token = await grants.get_by_token_hash(TokenHash("a" * 64))
        by_txn = await grants.get_by_authorization_request_id(request.id)

        assert by_token is not None
        assert by_token.id == grant.id
        assert by_txn is not None
        assert by_txn.id == grant.id

    async def test_revoke_marks_active_tokens_revoked(
        self, db_session: AsyncSession, owner_factory: OwnerFactory
    ) -> None:
        clients = SqlClientRepository(db_session)
        await clients.save(_client())
        requests = SqlAuthorizationRequestRepository(db_session, clock=FixedClock(_NOW))
        request = _pending_request("client-1")
        await requests.create(request)
        owner_id = await owner_factory.create()
        grants = SqlGrantRepository(db_session)
        grant = _grant(owner_id, request.id)
        await grants.create(grant)

        grant.revoke(now=_NOW, reason="owner_requested")
        await grants.save(grant)
        loaded = await grants.get_by_id(grant.id)

        assert loaded is not None
        assert loaded.is_revoked is True
        assert all(t.state is TokenState.REVOKED for t in loaded.tokens)

        active = await grants.list_active_for_owner(owner_id)
        assert active == []

    async def test_rotate_refresh_token_persists_old_as_rotated_and_new_as_active(
        self, db_session: AsyncSession, owner_factory: OwnerFactory
    ) -> None:
        clients = SqlClientRepository(db_session)
        await clients.save(_client())
        requests = SqlAuthorizationRequestRepository(db_session, clock=FixedClock(_NOW))
        request = _pending_request("client-1")
        await requests.create(request)
        owner_id = await owner_factory.create()
        grants = SqlGrantRepository(db_session)
        grant = _grant(owner_id, request.id)
        await grants.create(grant)
        new_access = IssuedToken(
            token_hash=TokenHash("e" * 64), kind=TokenKind.ACCESS, issued_at=_NOW,
            expires_at=_NOW + timedelta(minutes=60),
        )
        new_refresh = IssuedToken(
            token_hash=TokenHash("f" * 64), kind=TokenKind.REFRESH, issued_at=_NOW,
            expires_at=_NOW + timedelta(days=30),
        )

        grant.rotate_refresh_token(
            presented_hash=TokenHash("b" * 64), new_access=new_access, new_refresh=new_refresh,
            now=_NOW,
        )
        await grants.save(grant)
        loaded = await grants.get_by_id(grant.id)

        assert loaded is not None
        by_hash = {str(t.token_hash): t for t in loaded.tokens}
        assert by_hash["a" * 64].state is TokenState.ROTATED
        assert by_hash["b" * 64].state is TokenState.ROTATED
        assert by_hash["e" * 64].state is TokenState.ACTIVE
        assert by_hash["f" * 64].state is TokenState.ACTIVE
        assert by_hash["f" * 64].rotated_from == TokenHash("b" * 64)
        active_access = loaded.active_token(TokenKind.ACCESS)
        assert active_access is not None
        assert active_access.token_hash == TokenHash("e" * 64)

    async def test_list_active_for_owner_excludes_other_owners(
        self, db_session: AsyncSession, owner_factory: OwnerFactory
    ) -> None:
        clients = SqlClientRepository(db_session)
        await clients.save(_client())
        requests = SqlAuthorizationRequestRepository(db_session, clock=FixedClock(_NOW))
        request = _pending_request("client-1")
        await requests.create(request)
        owner_id = await owner_factory.create()
        other_owner_id = await owner_factory.create()
        grants = SqlGrantRepository(db_session)
        await grants.create(_grant(owner_id, request.id))

        active = await grants.list_active_for_owner(other_owner_id)

        assert active == []

    async def test_list_active_for_owner_batches_tokens_across_several_grants(
        self, db_session: AsyncSession, owner_factory: OwnerFactory
    ) -> None:
        """I7 de la revision de seguridad (16-sep): `list_active_for_owner`
        cargaba los tokens de cada concesion con UNA consulta POR concesion
        (N+1). Con 3 concesiones del mismo propietario, cada una debe
        recuperar EXACTAMENTE sus propios dos tokens -- la consulta en
        lote (`WHERE grant_id = ANY(...)`) no puede mezclar los de una
        concesion con los de otra al agrupar en memoria."""
        clients = SqlClientRepository(db_session)
        requests = SqlAuthorizationRequestRepository(db_session, clock=FixedClock(_NOW))
        owner_id = await owner_factory.create()
        grants = SqlGrantRepository(db_session)

        seeded_grants = []
        for i in range(3):
            client_id = f"client-{i}"
            await clients.save(_client(client_id))
            request = _pending_request(client_id, uuid.uuid4())
            await requests.create(request)
            grant = _grant(
                owner_id,
                request.id,
                client_id,
                access_hash=f"{i}" * 64,
                refresh_hash=f"{i}f" * 32,
            )
            await grants.create(grant)
            seeded_grants.append(grant)

        active = await grants.list_active_for_owner(owner_id)

        assert len(active) == 3
        active_by_id = {grant.id: grant for grant in active}
        for seeded in seeded_grants:
            loaded = active_by_id[seeded.id]
            assert {str(t.token_hash) for t in loaded.tokens} == {
                str(t.token_hash) for t in seeded.tokens
            }
