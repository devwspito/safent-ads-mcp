"""`MetaPagesPublishAsLookup` (T101): resuelve `publish_as` desde la
pagina de Meta ya conectada, en vez del `NullPublishAsLookup` documentado
como hueco."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from safent_ads.packages.infrastructure.publish_as_lookup import MetaPagesPublishAsLookup
from safent_ads.shared.ids import EntityLevel, EntityRef, PlatformCode

_BUSINESS_ID = uuid.UUID("8f14e45f-ceea-4f9a-9c1e-000000000001")
_CONNECTION_ID = uuid.UUID("8f14e45f-ceea-4f9a-9c1e-000000000002")


@dataclass(frozen=True, slots=True)
class _FakeMetaPage:
    page_id: str
    name: str


class _FakeMetaPageLookupPort:
    def __init__(self, pages: tuple[_FakeMetaPage, ...]) -> None:
        self._pages = pages
        self.calls: list[tuple[str, str]] = []

    async def list_meta_pages(
        self, *, business_id: str, account_ref: str
    ) -> tuple[_FakeMetaPage, ...]:
        self.calls.append((business_id, account_ref))
        return self._pages


def _account_ref() -> EntityRef:
    return EntityRef(
        platform=PlatformCode.META,
        level=EntityLevel.ACCOUNT,
        external_id="act_123",
        business_id=_BUSINESS_ID,
        connection_id=_CONNECTION_ID,
    )


class TestResolvesTheSingleConnectedPage:
    async def test_resolves_page_id_and_name(self) -> None:
        port = _FakeMetaPageLookupPort((_FakeMetaPage(page_id="998877", name="Clinica X"),))
        lookup = MetaPagesPublishAsLookup(port)

        resolved = await lookup.resolve(account_ref=_account_ref())

        assert resolved is not None
        assert resolved.page_id == "998877"
        assert resolved.page_name == "Clinica X"

    async def test_scopes_the_lookup_by_business_and_account(self) -> None:
        port = _FakeMetaPageLookupPort((_FakeMetaPage(page_id="1", name="Solo"),))
        lookup = MetaPagesPublishAsLookup(port)
        account_ref = _account_ref()

        await lookup.resolve(account_ref=account_ref)

        assert port.calls == [(str(_BUSINESS_ID), str(account_ref))]


class TestFailsClosedWhenAmbiguousOrAbsent:
    async def test_no_pages_resolves_to_none(self) -> None:
        lookup = MetaPagesPublishAsLookup(_FakeMetaPageLookupPort(()))

        assert await lookup.resolve(account_ref=_account_ref()) is None

    async def test_more_than_one_page_resolves_to_none_never_guesses(self) -> None:
        port = _FakeMetaPageLookupPort(
            (_FakeMetaPage(page_id="1", name="Uno"), _FakeMetaPage(page_id="2", name="Dos"))
        )
        lookup = MetaPagesPublishAsLookup(port)

        assert await lookup.resolve(account_ref=_account_ref()) is None

    async def test_account_ref_without_business_scope_resolves_to_none(self) -> None:
        port = _FakeMetaPageLookupPort((_FakeMetaPage(page_id="1", name="Uno"),))
        lookup = MetaPagesPublishAsLookup(port)
        unscoped = EntityRef(
            platform=PlatformCode.META, level=EntityLevel.ACCOUNT, external_id="act_123"
        )

        assert await lookup.resolve(account_ref=unscoped) is None
        assert port.calls == []
