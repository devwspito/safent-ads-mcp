"""`exchange_federated_code`/`ResolveFederatedLogin` (002b tasks.md T022,
contracts/federated-login.md §1, threat-model.md C-75 pieza 2): fases (b) y
(c) del callback, orquestacion pura sobre dobles -- ni red, ni base de
datos, ni HTTP. Ninguna de las dos sabe que existe un 303: devuelven un
desenlace o lanzan errores tipados que la presentacion traduce a
`federated_error=<codigo>`.

La fase (a) -- consumir la referencia -- tiene su propia suite en
`test_consume_federated_transaction.py`: aqui la transaccion siempre llega
YA consumida, como la entrega `federated_router.py::_run_callback_phases`
tras cerrar esa primera sesion de BD."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from safent_ads.iam.application.errors import (
    FederatedDenialReason,
    FederatedIdentityMismatchError,
    FederatedIdentityNotAuthorizedError,
    FederatedTransactionInvalidError,
    OwnerBoundElsewhereError,
)
from safent_ads.iam.application.ports import (
    AuthorizedEmailList,
    FederatedIdentityClaims,
    OwnerFederatedIdentityResolution,
    ResolvedFederatedOwner,
)
from safent_ads.iam.application.resolve_federated_login import (
    FederatedCallbackFailed,
    FederatedIdentificationRefreshed,
    FederatedLoginOutcome,
    FederatedSessionIssued,
    ResolveFederatedLogin,
    exchange_federated_code,
)
from safent_ads.iam.application.session_policy import SESSION_ABSOLUTE_TTL
from safent_ads.iam.domain.email import Email
from safent_ads.iam.domain.federated_identity import (
    FederatedIdentity,
    FederatedIssuer,
    FederatedSubject,
)
from safent_ads.iam.domain.federated_transaction import FederatedLoginTransaction, ReferenceHash
from safent_ads.iam.domain.session import Session, SessionOrigin
from safent_ads.iam.infrastructure.in_memory_session_repository import InMemorySessionRepository
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.errors import InfrastructureError
from safent_ads.shared.ids import UuidIdGenerator

_NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
_TTL = timedelta(minutes=10)
_OWNER_ID = uuid.uuid4()
_OWNER_EMAIL = "duenyo@example.com"
_STATE = "state-en-claro"
_NONCE = "nonce-en-claro"
_CODE = "codigo-de-google"
_REDIRECT_URI = "https://ads.example.com/api/v1/auth/federated/callback"
_ALLOW_LIST = AuthorizedEmailList.from_raw((_OWNER_EMAIL,))


def _cause(exc_info: pytest.ExceptionInfo[FederatedCallbackFailed]) -> Exception:
    """Cualquier fallo posterior a consumir la referencia llega envuelto en
    `FederatedCallbackFailed` (T064, revision de codigo): esto desenvuelve
    para seguir comprobando el tipo ORIGINAL, como antes del envoltorio."""
    return exc_info.value.cause


def _claims(*, email: str = _OWNER_EMAIL, email_verified: bool = True) -> FederatedIdentityClaims:
    return FederatedIdentityClaims(
        issuer=FederatedIssuer.GOOGLE,
        subject=FederatedSubject("112233445566778899000"),
        email=Email(email),
        email_verified=email_verified,
    )


class _FakeProvider:
    def __init__(
        self,
        *,
        claims: FederatedIdentityClaims | None = None,
        error: Exception | None = None,
    ) -> None:
        self._claims = claims or _claims()
        self._error = error
        self.seen_nonce_hash: ReferenceHash | None = None
        self.calls = 0

    def authorization_url(self, *, state: str, nonce: str, redirect_uri: str) -> str:
        return f"https://accounts.google.test/auth?{state}&{nonce}&{redirect_uri}"

    async def exchange_code(
        self, *, code: str, redirect_uri: str, expected_nonce_hash: ReferenceHash
    ) -> FederatedIdentityClaims:
        del code, redirect_uri
        self.calls += 1
        self.seen_nonce_hash = expected_nonce_hash
        if self._error is not None:
            raise self._error
        return self._claims


class _FakeOwnerIdentities:
    def __init__(
        self,
        *,
        owner_id: uuid.UUID = _OWNER_ID,
        resolution: OwnerFederatedIdentityResolution = OwnerFederatedIdentityResolution.CREATED,
        error: Exception | None = None,
    ) -> None:
        self._owner_id = owner_id
        self._resolution = resolution
        self._error = error
        self.calls = 0

    async def resolve_for_subject(
        self,
        *,
        issuer: FederatedIssuer,
        subject: FederatedSubject,
        email: Email,
        now: datetime,
    ) -> ResolvedFederatedOwner:
        del issuer, subject, email, now
        self.calls += 1
        if self._error is not None:
            raise self._error
        return ResolvedFederatedOwner(owner_id=self._owner_id, resolution=self._resolution)

    async def find_for_owner(self, *, owner_id: uuid.UUID) -> FederatedIdentity | None:
        del owner_id
        return None


def _login_transaction(*, txn_id: uuid.UUID | None = None) -> FederatedLoginTransaction:
    transaction = FederatedLoginTransaction.open_for_login(
        state_hash=ReferenceHash.of(_STATE),
        nonce_hash=ReferenceHash.of(_NONCE),
        txn_id=txn_id,
        created_at=_NOW,
        ttl=_TTL,
    )
    transaction.consume(_NOW)
    return transaction


def _reidentify_transaction(
    *, session_id: uuid.UUID, txn_id: uuid.UUID | None = None
) -> FederatedLoginTransaction:
    transaction = FederatedLoginTransaction.open_for_reidentification(
        state_hash=ReferenceHash.of(_STATE),
        nonce_hash=ReferenceHash.of(_NONCE),
        session_id=session_id,
        txn_id=txn_id,
        created_at=_NOW,
        ttl=_TTL,
    )
    transaction.consume(_NOW)
    return transaction


def _session(
    *,
    owner_id: uuid.UUID = _OWNER_ID,
    origin: SessionOrigin = SessionOrigin.PASSWORD,
    last_federated_auth_at: datetime | None = None,
    revoked_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> Session:
    return Session(
        session_id=uuid.uuid4(),
        owner_id=owner_id,
        token_hash="b" * 64,
        created_at=_NOW - timedelta(minutes=10),
        expires_at=expires_at or _NOW + timedelta(minutes=20),
        revoked_at=revoked_at,
        origin=origin,
        last_federated_auth_at=last_federated_auth_at,
    )


def _resolver(
    *,
    owner_identities: _FakeOwnerIdentities | None = None,
    sessions: InMemorySessionRepository | None = None,
    allowed_emails: AuthorizedEmailList = _ALLOW_LIST,
) -> ResolveFederatedLogin:
    return ResolveFederatedLogin(
        owner_identities=owner_identities or _FakeOwnerIdentities(),  # type: ignore[arg-type]
        sessions=sessions or InMemorySessionRepository(),
        allowed_emails=allowed_emails,
        id_generator=UuidIdGenerator(),
        clock=FixedClock(_NOW),
    )


# ---------------------------------------------------------------------------
# Fase (b): exchange_federated_code
# ---------------------------------------------------------------------------


async def test_the_provider_is_asked_to_check_the_nonce_of_this_transaction() -> None:
    provider = _FakeProvider()
    transaction = _login_transaction()

    await exchange_federated_code(
        provider, transaction, code=_CODE, redirect_uri=_REDIRECT_URI
    )

    assert provider.seen_nonce_hash == ReferenceHash.of(_NONCE)


async def test_a_provider_failure_is_wrapped_with_the_transactions_context() -> None:
    provider = _FakeProvider(error=InfrastructureError("Google no responde"))
    txn_id = uuid.uuid4()
    transaction = _login_transaction(txn_id=txn_id)

    with pytest.raises(FederatedCallbackFailed) as failure:
        await exchange_federated_code(provider, transaction, code=_CODE, redirect_uri=_REDIRECT_URI)

    assert isinstance(_cause(failure), InfrastructureError)
    assert failure.value.context.txn_id == txn_id


# ---------------------------------------------------------------------------
# Fase (c): ResolveFederatedLogin
# ---------------------------------------------------------------------------


async def test_login_issues_a_federated_session_and_registers_the_owner() -> None:
    sessions = InMemorySessionRepository()
    txn_id = uuid.uuid4()
    resolver = _resolver(sessions=sessions)

    result = await resolver.execute(_login_transaction(txn_id=txn_id), _claims())

    assert isinstance(result, FederatedSessionIssued)
    assert result.outcome is FederatedLoginOutcome.SESSION_ISSUED
    assert result.owner_id == _OWNER_ID
    assert result.txn_id == txn_id
    assert result.resolution is OwnerFederatedIdentityResolution.CREATED
    issued = result.authenticated_session.session
    assert issued.origin is SessionOrigin.FEDERATED
    assert issued.last_federated_auth_at == _NOW
    assert await sessions.get_by_id(issued.id) is issued


async def test_login_on_an_installation_that_already_has_an_owner_does_not_register_again() -> None:
    resolver = _resolver(
        owner_identities=_FakeOwnerIdentities(
            resolution=OwnerFederatedIdentityResolution.ALREADY_BOUND
        )
    )

    result = await resolver.execute(_login_transaction(), _claims())

    assert isinstance(result, FederatedSessionIssued)
    assert result.resolution is OwnerFederatedIdentityResolution.ALREADY_BOUND


async def test_an_unverified_email_is_denied_without_resolving_any_owner() -> None:
    owner_identities = _FakeOwnerIdentities()
    resolver = _resolver(owner_identities=owner_identities)

    with pytest.raises(FederatedCallbackFailed) as failure:
        await resolver.execute(_login_transaction(), _claims(email_verified=False))

    cause = _cause(failure)
    assert isinstance(cause, FederatedIdentityNotAuthorizedError)
    assert cause.reason is FederatedDenialReason.EMAIL_NOT_VERIFIED
    assert owner_identities.calls == 0


async def test_an_email_outside_the_allow_list_is_denied() -> None:
    owner_identities = _FakeOwnerIdentities()
    resolver = _resolver(owner_identities=owner_identities)

    with pytest.raises(FederatedCallbackFailed) as failure:
        await resolver.execute(_login_transaction(), _claims(email="intruso@example.com"))

    cause = _cause(failure)
    assert isinstance(cause, FederatedIdentityNotAuthorizedError)
    assert cause.reason is FederatedDenialReason.EMAIL_NOT_ALLOWED
    assert owner_identities.calls == 0


async def test_an_empty_allow_list_authorizes_nobody() -> None:
    resolver = _resolver(allowed_emails=AuthorizedEmailList.from_raw(()))

    with pytest.raises(FederatedCallbackFailed) as failure:
        await resolver.execute(_login_transaction(), _claims())

    cause = _cause(failure)
    assert isinstance(cause, FederatedIdentityNotAuthorizedError)
    assert cause.reason is FederatedDenialReason.EMAIL_NOT_ALLOWED


async def test_an_owner_bound_to_another_identity_is_reported_as_such() -> None:
    resolver = _resolver(
        owner_identities=_FakeOwnerIdentities(error=OwnerBoundElsewhereError("ya atado"))
    )

    with pytest.raises(FederatedCallbackFailed) as failure:
        await resolver.execute(_login_transaction(), _claims())
    assert isinstance(_cause(failure), OwnerBoundElsewhereError)


async def test_reidentification_marks_freshness_on_the_very_session_that_jumped() -> None:
    sessions = InMemorySessionRepository()
    session = _session()
    await sessions.create(session)
    txn_id = uuid.uuid4()
    resolver = _resolver(
        owner_identities=_FakeOwnerIdentities(
            resolution=OwnerFederatedIdentityResolution.ALREADY_BOUND
        ),
        sessions=sessions,
    )

    result = await resolver.execute(
        _reidentify_transaction(session_id=session.id, txn_id=txn_id), _claims()
    )

    assert isinstance(result, FederatedIdentificationRefreshed)
    assert result.outcome is FederatedLoginOutcome.IDENTIFICATION_REFRESHED
    assert result.session_id == session.id
    assert result.txn_id == txn_id
    assert session.last_federated_auth_at == _NOW


async def test_reidentification_never_rewrites_the_origin_nor_extends_the_session() -> None:
    sessions = InMemorySessionRepository()
    session = _session()
    expires_at_before = session.expires_at
    created_at_before = session.created_at
    await sessions.create(session)
    resolver = _resolver(sessions=sessions)

    await resolver.execute(_reidentify_transaction(session_id=session.id), _claims())

    assert session.origin is SessionOrigin.PASSWORD
    assert session.expires_at == expires_at_before
    assert session.created_at == created_at_before


async def test_reidentification_with_another_owners_account_marks_nothing() -> None:
    sessions = InMemorySessionRepository()
    session = _session(owner_id=uuid.uuid4())
    await sessions.create(session)
    resolver = _resolver(
        owner_identities=_FakeOwnerIdentities(owner_id=_OWNER_ID),
        sessions=sessions,
    )

    with pytest.raises(FederatedCallbackFailed) as failure:
        await resolver.execute(_reidentify_transaction(session_id=session.id), _claims())
    assert isinstance(_cause(failure), FederatedIdentityMismatchError)

    assert session.last_federated_auth_at is None


async def test_reidentification_of_a_session_that_no_longer_exists_is_rejected() -> None:
    resolver = _resolver()

    with pytest.raises(FederatedCallbackFailed) as failure:
        await resolver.execute(_reidentify_transaction(session_id=uuid.uuid4()), _claims())
    assert isinstance(_cause(failure), FederatedTransactionInvalidError)


async def test_reidentification_of_a_revoked_session_marks_nothing() -> None:
    sessions = InMemorySessionRepository()
    session = _session(revoked_at=_NOW - timedelta(minutes=1))
    await sessions.create(session)
    resolver = _resolver(sessions=sessions)

    with pytest.raises(FederatedCallbackFailed) as failure:
        await resolver.execute(_reidentify_transaction(session_id=session.id), _claims())
    assert isinstance(_cause(failure), FederatedTransactionInvalidError)

    assert session.last_federated_auth_at is None


async def test_reidentification_of_an_expired_session_marks_nothing() -> None:
    sessions = InMemorySessionRepository()
    session = _session(expires_at=_NOW - timedelta(seconds=1))
    await sessions.create(session)
    resolver = _resolver(sessions=sessions)

    with pytest.raises(FederatedCallbackFailed) as failure:
        await resolver.execute(_reidentify_transaction(session_id=session.id), _claims())
    assert isinstance(_cause(failure), FederatedTransactionInvalidError)

    assert session.last_federated_auth_at is None


async def test_reidentification_of_a_session_past_its_absolute_ttl_marks_nothing() -> None:
    sessions = InMemorySessionRepository()
    session = Session(
        session_id=uuid.uuid4(),
        owner_id=_OWNER_ID,
        token_hash="c" * 64,
        created_at=_NOW - SESSION_ABSOLUTE_TTL - timedelta(minutes=1),
        expires_at=_NOW + timedelta(minutes=20),
        revoked_at=None,
    )
    await sessions.create(session)
    resolver = _resolver(sessions=sessions)

    with pytest.raises(FederatedCallbackFailed) as failure:
        await resolver.execute(_reidentify_transaction(session_id=session.id), _claims())
    assert isinstance(_cause(failure), FederatedTransactionInvalidError)

    assert session.last_federated_auth_at is None
