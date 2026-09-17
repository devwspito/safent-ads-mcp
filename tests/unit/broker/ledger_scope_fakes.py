"""Explicit server-binding fake for SDK-free unit fixtures, never production wiring."""

from uuid import NAMESPACE_URL, UUID, uuid5

from safent_ads.accounts.application.ports import WriteIntent
from safent_ads.broker.domain.ledger_scope import LedgerScope

BUSINESS = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")


def fake_scope(intent: WriteIntent, account: str) -> LedgerScope:
    try:
        business = UUID(intent.business_id) if intent.business_id else BUSINESS
    except ValueError:
        business = uuid5(NAMESPACE_URL, intent.business_id)
    return LedgerScope(business, intent.entity_ref.platform, account)
