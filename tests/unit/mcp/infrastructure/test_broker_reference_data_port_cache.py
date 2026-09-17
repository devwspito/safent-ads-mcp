"""Perf (16-sep, item 3, measured through the MCP): `search_meta_targeting`
2.8-3.2 s, `get_google_keyword_ideas` 4.3-7.1 s, `list_meta_pages`/
`list_meta_pixels`/`list_meta_audiences`/`list_meta_catalogs` a similar
class -- identical repeated calls paid the full upstream round trip every
time. `BrokerReferenceDataPort` now caches by
(business_id, account_ref, tool, canonical arguments) with a bounded,
LRU-evicted, per-tool TTL cache (`TLRUCache`, `cachetools`)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from safent_ads.accounts.domain.refs import AccountRef
from safent_ads.mcp.infrastructure import broker_reference_data_port as module
from safent_ads.mcp.infrastructure.broker_reference_data_port import BrokerReferenceDataPort
from safent_ads.shared.ids import PlatformCode

_BUSINESS_ID = "11111111-1111-1111-1111-111111111111"
_OTHER_BUSINESS_ID = "99999999-9999-9999-9999-999999999999"
_ACCOUNT_REF = "meta:act_123"
_OTHER_ACCOUNT_REF = "meta:act_456"

_META_PAGE_ROW = {"id": "page-1", "name": "Acme"}
_TEN_MINUTES = 10 * 60.0
_ONE_HOUR = 60 * 60.0


class _ManualClock:
    """Reloj monotonico controlable a mano para probar la expiracion de la
    cache sin depender de `time.sleep`."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


def _port(
    *, meta_client: AsyncMock | None = None, cache_timer: _ManualClock | None = None
) -> tuple[BrokerReferenceDataPort, AsyncMock]:
    client = meta_client or AsyncMock()
    port = BrokerReferenceDataPort(
        client,
        AsyncMock(),
        AsyncMock(),
        object(),
        cache_timer=cache_timer or _ManualClock(),
    )
    return port, client


def _resolve_to(monkeypatch: pytest.MonkeyPatch, account_ref: str = "act_123") -> None:
    account = AccountRef(PlatformCode.META, account_ref)
    monkeypatch.setattr(module, "resolve_owned_account_ref", AsyncMock(return_value=account))


async def test_identical_calls_are_a_cache_hit_and_the_broker_runs_once(monkeypatch) -> None:
    _resolve_to(monkeypatch)
    meta_client = AsyncMock()
    meta_client.read.return_value = [_META_PAGE_ROW]
    port, _ = _port(meta_client=meta_client)

    first = await port.list_meta_pages(_BUSINESS_ID, _ACCOUNT_REF)
    second = await port.list_meta_pages(_BUSINESS_ID, _ACCOUNT_REF)

    assert first == second
    assert meta_client.read.await_count == 1


async def test_different_arguments_for_the_same_tool_are_different_cache_entries(
    monkeypatch,
) -> None:
    _resolve_to(monkeypatch)
    meta_client = AsyncMock()
    meta_client.read.return_value = []
    port, _ = _port(meta_client=meta_client)

    await port.search_meta_targeting(
        _BUSINESS_ID, _ACCOUNT_REF, kind=module.MetaTargetingKind.INTEREST, query="running shoes"
    )
    await port.search_meta_targeting(
        _BUSINESS_ID, _ACCOUNT_REF, kind=module.MetaTargetingKind.INTEREST, query="hiking boots"
    )

    assert meta_client.read.await_count == 2


async def test_cache_key_is_isolated_per_account_ref(monkeypatch) -> None:
    _resolve_to(monkeypatch)
    meta_client = AsyncMock()
    meta_client.read.return_value = [_META_PAGE_ROW]
    port, _ = _port(meta_client=meta_client)

    await port.list_meta_pages(_BUSINESS_ID, _ACCOUNT_REF)
    await port.list_meta_pages(_BUSINESS_ID, _OTHER_ACCOUNT_REF)

    assert meta_client.read.await_count == 2


async def test_cache_key_is_isolated_per_business_id(monkeypatch) -> None:
    _resolve_to(monkeypatch)
    meta_client = AsyncMock()
    meta_client.read.return_value = [_META_PAGE_ROW]
    port, _ = _port(meta_client=meta_client)

    await port.list_meta_pages(_BUSINESS_ID, _ACCOUNT_REF)
    await port.list_meta_pages(_OTHER_BUSINESS_ID, _ACCOUNT_REF)

    assert meta_client.read.await_count == 2


async def test_entry_is_refetched_once_its_ttl_expires(monkeypatch) -> None:
    """`list_meta_pages` cae en el cubo de 10 min (pages/pixels/audiences/
    catalogos, item 3)."""
    _resolve_to(monkeypatch)
    meta_client = AsyncMock()
    meta_client.read.return_value = [_META_PAGE_ROW]
    clock = _ManualClock()
    port, _ = _port(meta_client=meta_client, cache_timer=clock)

    await port.list_meta_pages(_BUSINESS_ID, _ACCOUNT_REF)
    clock.advance(_TEN_MINUTES - 1)
    await port.list_meta_pages(_BUSINESS_ID, _ACCOUNT_REF)
    assert meta_client.read.await_count == 1  # still fresh: cache hit

    clock.advance(2)  # crosses the 10-minute TTU
    await port.list_meta_pages(_BUSINESS_ID, _ACCOUNT_REF)

    assert meta_client.read.await_count == 2


async def test_reach_estimate_is_cached_for_an_hour_once_ready(monkeypatch) -> None:
    _resolve_to(monkeypatch)
    meta_client = AsyncMock()
    meta_client.read.return_value = [{"users_lower_bound": 100, "estimate_ready": True}]
    clock = _ManualClock()
    port, _ = _port(meta_client=meta_client, cache_timer=clock)

    await port.get_meta_reach_estimate(
        _BUSINESS_ID, _ACCOUNT_REF, optimization_goal="REACH", countries=("ES",)
    )
    clock.advance(_ONE_HOUR - 1)
    await port.get_meta_reach_estimate(
        _BUSINESS_ID, _ACCOUNT_REF, optimization_goal="REACH", countries=("ES",)
    )
    assert meta_client.read.await_count == 1

    clock.advance(2)
    await port.get_meta_reach_estimate(
        _BUSINESS_ID, _ACCOUNT_REF, optimization_goal="REACH", countries=("ES",)
    )
    assert meta_client.read.await_count == 2


async def test_a_not_ready_reach_estimate_is_never_cached(monkeypatch) -> None:
    _resolve_to(monkeypatch)
    meta_client = AsyncMock()
    meta_client.read.return_value = [{"estimate_ready": False}]
    port, _ = _port(meta_client=meta_client)

    first = await port.get_meta_reach_estimate(
        _BUSINESS_ID, _ACCOUNT_REF, optimization_goal="REACH", countries=("ES",)
    )
    second = await port.get_meta_reach_estimate(
        _BUSINESS_ID, _ACCOUNT_REF, optimization_goal="REACH", countries=("ES",)
    )

    assert first.estimate_ready is False
    assert second.estimate_ready is False
    assert meta_client.read.await_count == 2


async def test_a_broker_error_is_never_cached(monkeypatch) -> None:
    _resolve_to(monkeypatch)
    meta_client = AsyncMock()
    meta_client.read.side_effect = RuntimeError("synthetic broker failure")
    port, _ = _port(meta_client=meta_client)

    for _ in range(2):
        with pytest.raises(RuntimeError):
            await port.list_meta_pages(_BUSINESS_ID, _ACCOUNT_REF)

    assert meta_client.read.await_count == 2


async def test_list_google_conversion_actions_is_never_cached(monkeypatch) -> None:
    """Ausente de `_CACHE_TTL_SECONDS_BY_TOOL` a proposito -- "todo lo demas
    sin cache" (item 3)."""
    account = AccountRef(PlatformCode.GOOGLE, "act_123")
    monkeypatch.setattr(module, "resolve_owned_account_ref", AsyncMock(return_value=account))
    ads_platform_port = AsyncMock()
    ads_platform_port.run_gaql.return_value = []
    port = BrokerReferenceDataPort(AsyncMock(), AsyncMock(), ads_platform_port, object())

    await port.list_google_conversion_actions(_BUSINESS_ID, _ACCOUNT_REF)
    await port.list_google_conversion_actions(_BUSINESS_ID, _ACCOUNT_REF)

    assert ads_platform_port.run_gaql.await_count == 2


async def test_search_google_constants_is_cached_for_a_day(monkeypatch) -> None:
    account = AccountRef(PlatformCode.GOOGLE, "act_123")
    monkeypatch.setattr(module, "resolve_owned_account_ref", AsyncMock(return_value=account))
    ads_platform_port = AsyncMock()
    ads_platform_port.run_gaql.return_value = [
        {
            "geo_target_constant.resource_name": "geoTargetConstants/1",
            "geo_target_constant.name": "Madrid",
            "geo_target_constant.country_code": "ES",
        }
    ]
    port = BrokerReferenceDataPort(
        AsyncMock(), AsyncMock(), ads_platform_port, object(), cache_timer=_ManualClock()
    )

    await port.search_google_constants(
        _BUSINESS_ID,
        _ACCOUNT_REF,
        kind=module.GoogleConstantKind.GEO_TARGET,
        query="Madrid",
        country=None,
    )
    await port.search_google_constants(
        _BUSINESS_ID,
        _ACCOUNT_REF,
        kind=module.GoogleConstantKind.GEO_TARGET,
        query="Madrid",
        country=None,
    )

    assert ads_platform_port.run_gaql.await_count == 1


async def test_get_google_keyword_ideas_is_cached_for_a_day(monkeypatch) -> None:
    account = AccountRef(PlatformCode.GOOGLE, "act_123")
    monkeypatch.setattr(module, "resolve_owned_account_ref", AsyncMock(return_value=account))
    google_client = AsyncMock()
    google_client.read.return_value = [{"text": "zapatillas"}]
    port = BrokerReferenceDataPort(
        AsyncMock(), google_client, AsyncMock(), object(), cache_timer=_ManualClock()
    )

    for _ in range(2):
        await port.get_google_keyword_ideas(
            _BUSINESS_ID,
            _ACCOUNT_REF,
            seed_keywords=("zapatillas",),
            geo_target="2724",
            language="1003",
        )

    assert google_client.read.await_count == 1


async def test_cache_is_bounded_to_the_configured_max_entries() -> None:
    port, _ = _port()

    assert port._cache.maxsize == module._CACHE_MAX_ENTRIES
