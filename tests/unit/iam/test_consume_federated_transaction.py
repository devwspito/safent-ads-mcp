"""`ConsumeFederatedTransaction` (002b threat-model.md C-75 pieza 2, T084
hardening): fase (a) del callback, aislada -- consumir la referencia de un
solo uso ANTES de canjear nada contra Google. Pura sobre un doble del
repositorio, sin red ni base de datos."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from safent_ads.iam.application.consume_federated_transaction import ConsumeFederatedTransaction
from safent_ads.iam.application.errors import FederatedTransactionInvalidError
from safent_ads.iam.domain.federated_transaction import FederatedLoginTransaction, ReferenceHash
from safent_ads.shared.clock import FixedClock

_NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
_TTL = timedelta(minutes=10)
_STATE = "state-en-claro"
_NONCE = "nonce-en-claro"


class _FakeTransactions:
    def __init__(self, *transactions: FederatedLoginTransaction) -> None:
        self._by_state = {t.state_hash: t for t in transactions}
        self.seen_state_hash: ReferenceHash | None = None

    async def create(self, transaction: FederatedLoginTransaction) -> None:
        self._by_state[transaction.state_hash] = transaction

    async def consume(
        self, *, state_hash: ReferenceHash, now: datetime
    ) -> FederatedLoginTransaction | None:
        self.seen_state_hash = state_hash
        transaction = self._by_state.get(state_hash)
        if transaction is None or not transaction.is_consumable(now):
            return None
        transaction.consume(now)
        return transaction

    async def purge_expired(self, now: datetime) -> int:
        del now
        return 0


def _login_transaction(
    *, txn_id: uuid.UUID | None = None, created_at: datetime = _NOW
) -> FederatedLoginTransaction:
    return FederatedLoginTransaction.open_for_login(
        state_hash=ReferenceHash.of(_STATE),
        nonce_hash=ReferenceHash.of(_NONCE),
        txn_id=txn_id,
        created_at=created_at,
        ttl=_TTL,
    )


def _use_case(transactions: _FakeTransactions) -> ConsumeFederatedTransaction:
    return ConsumeFederatedTransaction(
        transactions=transactions,  # type: ignore[arg-type]
        clock=FixedClock(_NOW),
    )


async def test_the_state_reaches_the_repository_only_as_a_fingerprint() -> None:
    transactions = _FakeTransactions(_login_transaction())

    await _use_case(transactions).execute(_STATE)

    assert transactions.seen_state_hash == ReferenceHash.of(_STATE)


async def test_an_unknown_state_is_rejected() -> None:
    with pytest.raises(FederatedTransactionInvalidError):
        await _use_case(_FakeTransactions()).execute("state-que-nadie-abrio")


async def test_an_expired_state_is_rejected() -> None:
    transactions = _FakeTransactions(_login_transaction(created_at=_NOW - _TTL))

    with pytest.raises(FederatedTransactionInvalidError):
        await _use_case(transactions).execute(_STATE)


async def test_a_state_can_be_used_only_once() -> None:
    transactions = _FakeTransactions(_login_transaction())
    use_case = _use_case(transactions)

    consumed = await use_case.execute(_STATE)

    assert consumed.state_hash == ReferenceHash.of(_STATE)
    with pytest.raises(FederatedTransactionInvalidError):
        await use_case.execute(_STATE)


async def test_a_consumed_transaction_carries_its_txn_id() -> None:
    txn_id = uuid.uuid4()
    transactions = _FakeTransactions(_login_transaction(txn_id=txn_id))

    consumed = await _use_case(transactions).execute(_STATE)

    assert consumed.txn_id == txn_id
