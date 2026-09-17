"""`GenerateWebhookToken`/`AuthenticateWebhookToken` (T220): el crudo nunca
se persiste, regenerar invalida el token anterior."""

from __future__ import annotations

from safent_ads.crm.application.webhook_token import (
    AuthenticateWebhookToken,
    GenerateWebhookToken,
    hash_webhook_token,
)
from safent_ads.shared.ids import BusinessId

_BUSINESS_ID = BusinessId.new()


class _InMemoryWebhookTokens:
    def __init__(self) -> None:
        self._by_business: dict[str, str] = {}
        self._by_hash: dict[str, BusinessId] = {}

    async def upsert(self, *, business_id: BusinessId, token_hash: str) -> None:
        previous = self._by_business.get(str(business_id))
        if previous is not None:
            del self._by_hash[previous]
        self._by_business[str(business_id)] = token_hash
        self._by_hash[token_hash] = business_id

    async def find_business_id_by_token_hash(self, token_hash: str) -> BusinessId | None:
        return self._by_hash.get(token_hash)


async def test_generated_token_authenticates_to_the_right_business() -> None:
    tokens = _InMemoryWebhookTokens()
    raw_token = await GenerateWebhookToken(tokens=tokens).execute(_BUSINESS_ID)

    resolved = await AuthenticateWebhookToken(tokens=tokens).execute(raw_token)

    assert resolved == _BUSINESS_ID


async def test_only_the_hash_is_ever_stored() -> None:
    tokens = _InMemoryWebhookTokens()
    raw_token = await GenerateWebhookToken(tokens=tokens).execute(_BUSINESS_ID)

    assert raw_token not in tokens._by_hash  # noqa: SLF001 - inspeccion directa del doble
    assert hash_webhook_token(raw_token) in tokens._by_hash  # noqa: SLF001


async def test_regenerating_invalidates_the_previous_token() -> None:
    tokens = _InMemoryWebhookTokens()
    first_token = await GenerateWebhookToken(tokens=tokens).execute(_BUSINESS_ID)
    second_token = await GenerateWebhookToken(tokens=tokens).execute(_BUSINESS_ID)

    authenticator = AuthenticateWebhookToken(tokens=tokens)
    assert await authenticator.execute(first_token) is None
    assert await authenticator.execute(second_token) == _BUSINESS_ID


async def test_unknown_token_authenticates_to_nothing() -> None:
    tokens = _InMemoryWebhookTokens()

    resolved = await AuthenticateWebhookToken(tokens=tokens).execute("never-issued")

    assert resolved is None
