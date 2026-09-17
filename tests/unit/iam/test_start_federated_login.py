"""`StartFederatedLogin` (002b tasks.md T022, contracts/federated-login.md §1
`POST /auth/federated/start`): el proposito lo decide el SERVIDOR (hay sesion
viva o no la hay), las referencias solo se persisten como huella y la respuesta
nunca lleva `state` ni `nonce` sueltos."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest

from safent_ads.iam.application.errors import TooManyPendingFederatedTransactionsError
from safent_ads.iam.application.session_policy import (
    FEDERATED_TRANSACTION_TTL,
    MAX_PENDING_FEDERATED_TRANSACTIONS_PER_IP,
)
from safent_ads.iam.application.start_federated_login import StartFederatedLogin
from safent_ads.iam.domain.federated_transaction import (
    FederatedLoginTransaction,
    ReferenceHash,
    TransactionPurpose,
)
from safent_ads.shared.clock import FixedClock

_NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
_REDIRECT_URI = "https://ads.example.com/api/v1/auth/federated/callback"
_IP = "203.0.113.9"


class _RecordingProvider:
    def __init__(self) -> None:
        self.state: str | None = None
        self.nonce: str | None = None

    def authorization_url(self, *, state: str, nonce: str, redirect_uri: str) -> str:
        self.state = state
        self.nonce = nonce
        return f"https://accounts.google.test/auth?state={state}&nonce={nonce}&ru={redirect_uri}"


class _RecordingTransactions:
    def __init__(self, *, pending_for_ip: int = 0) -> None:
        self.created: list[FederatedLoginTransaction] = []
        self._pending_for_ip = pending_for_ip
        self.seen_ip_address: str | None = None
        self.seen_now: datetime | None = None

    async def create(self, transaction: FederatedLoginTransaction) -> None:
        self.created.append(transaction)

    async def count_pending_for_ip(self, *, ip_address: str, now: datetime) -> int:
        self.seen_ip_address = ip_address
        self.seen_now = now
        return self._pending_for_ip

    async def consume(
        self, *, state_hash: ReferenceHash, now: datetime
    ) -> FederatedLoginTransaction | None:
        del state_hash, now
        return None

    async def purge_expired(self, now: datetime) -> int:
        del now
        return 0


class _TickingClock:
    """Devuelve un instante DISTINTO en cada llamada -- si `execute()`
    llamara al reloj mas de una vez (nit, code review 17-sep), el tope
    por IP y la transaccion abierta verian instantes diferentes."""

    def __init__(self, start: datetime) -> None:
        self._next = start

    def now(self) -> datetime:
        value = self._next
        self._next = self._next + timedelta(seconds=1)
        return value


def _use_case(
    provider: _RecordingProvider,
    transactions: _RecordingTransactions,
    *,
    clock: FixedClock | _TickingClock | None = None,
) -> StartFederatedLogin:
    return StartFederatedLogin(
        provider=provider,  # type: ignore[arg-type]
        transactions=transactions,  # type: ignore[arg-type]
        clock=clock or FixedClock(_NOW),  # type: ignore[arg-type]
        redirect_uri=_REDIRECT_URI,
    )


async def test_without_a_live_session_the_purpose_is_login() -> None:
    provider, transactions = _RecordingProvider(), _RecordingTransactions()
    txn_id = uuid.uuid4()

    started = await _use_case(provider, transactions).execute(
        txn_id=txn_id, session_id=None, ip_address=_IP
    )

    opened = transactions.created[0]
    assert opened.purpose is TransactionPurpose.LOGIN
    assert opened.session_id is None
    assert opened.txn_id == txn_id
    assert started.expires_at == _NOW + FEDERATED_TRANSACTION_TTL


async def test_with_a_live_session_the_purpose_is_reidentify_and_it_is_bound_to_it() -> None:
    provider, transactions = _RecordingProvider(), _RecordingTransactions()
    session_id = uuid.uuid4()

    await _use_case(provider, transactions).execute(
        txn_id=None, session_id=session_id, ip_address=_IP
    )

    opened = transactions.created[0]
    assert opened.purpose is TransactionPurpose.REIDENTIFY
    assert opened.session_id == session_id


async def test_only_the_fingerprints_are_persisted() -> None:
    provider, transactions = _RecordingProvider(), _RecordingTransactions()

    await _use_case(provider, transactions).execute(txn_id=None, session_id=None, ip_address=_IP)

    opened = transactions.created[0]
    assert provider.state is not None
    assert provider.nonce is not None
    assert opened.state_hash == ReferenceHash.of(provider.state)
    assert opened.nonce_hash == ReferenceHash.of(provider.nonce)
    assert provider.state != provider.nonce


async def test_the_clear_references_travel_only_inside_the_authorization_url() -> None:
    provider, transactions = _RecordingProvider(), _RecordingTransactions()

    started = await _use_case(provider, transactions).execute(
        txn_id=None, session_id=None, ip_address=_IP
    )

    query = parse_qs(urlparse(started.authorization_url).query)
    assert query["state"] == [provider.state]
    assert query["nonce"] == [provider.nonce]


async def test_every_jump_opens_a_different_pair_of_references() -> None:
    provider, transactions = _RecordingProvider(), _RecordingTransactions()
    use_case = _use_case(provider, transactions)

    await use_case.execute(txn_id=None, session_id=None, ip_address=_IP)
    first = transactions.created[0]
    await use_case.execute(txn_id=None, session_id=None, ip_address=_IP)
    second = transactions.created[1]

    assert first.state_hash != second.state_hash
    assert first.nonce_hash != second.nonce_hash


async def test_the_ip_that_opened_the_jump_travels_with_the_transaction() -> None:
    provider, transactions = _RecordingProvider(), _RecordingTransactions()

    await _use_case(provider, transactions).execute(txn_id=None, session_id=None, ip_address=_IP)

    assert transactions.created[0].ip_address == _IP
    assert transactions.seen_ip_address == _IP


async def test_pending_federated_transactions_are_capped_per_ip() -> None:
    """threat-model.md C-79: al tope (5), rechaza SIN crear una sexta."""
    provider = _RecordingProvider()
    transactions = _RecordingTransactions(pending_for_ip=MAX_PENDING_FEDERATED_TRANSACTIONS_PER_IP)

    with pytest.raises(TooManyPendingFederatedTransactionsError):
        await _use_case(provider, transactions).execute(
            txn_id=None, session_id=None, ip_address=_IP
        )

    assert transactions.created == []


async def test_below_the_cap_the_jump_still_opens() -> None:
    provider = _RecordingProvider()
    transactions = _RecordingTransactions(
        pending_for_ip=MAX_PENDING_FEDERATED_TRANSACTIONS_PER_IP - 1
    )

    await _use_case(provider, transactions).execute(txn_id=None, session_id=None, ip_address=_IP)

    assert len(transactions.created) == 1


async def test_the_pending_ip_cap_and_the_new_transaction_see_the_same_instant() -> None:
    """Nit (code review 17-sep): un solo `now` por `execute()` -- el tope
    por IP y la transaccion abierta deben ver el MISMO instante, no dos
    lecturas del reloj a milisegundos de distancia."""
    provider = _RecordingProvider()
    transactions = _RecordingTransactions()
    use_case = _use_case(provider, transactions, clock=_TickingClock(_NOW))

    await use_case.execute(txn_id=None, session_id=None, ip_address=_IP)

    assert transactions.seen_now == transactions.created[0].created_at
