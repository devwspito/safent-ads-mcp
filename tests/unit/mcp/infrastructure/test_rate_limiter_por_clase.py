"""`InMemoryQuota` (004 tasks-2.md Carril Q, Q1; contracts/mcp.md §6): dos
ventanas por llamada -- `(caller_id, tool_name)` y, para toda clase que no
sea `read`, `(caller_id, "escrituras")` -- y niega si cualquiera esta
agotada. Cuatro nombres cargan un limite propio, mas estrecho que el
default de 60, configurable sin numeros magicos sueltos."""

from __future__ import annotations

from datetime import UTC, datetime

from safent_ads.mcp.infrastructure.rate_limiter import InMemoryQuota
from safent_ads.shared.clock import FixedClock

_NOW = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)


def _quota(**kwargs: object) -> InMemoryQuota:
    return InMemoryQuota(clock=FixedClock(_NOW), **kwargs)  # type: ignore[arg-type]


async def test_default_limit_is_sixty_per_caller_and_tool() -> None:
    quota = _quota()
    for _ in range(60):
        assert await quota.check_and_consume(
            caller_id="person:a", tool_name="list_businesses", tool_class="read"
        )
    assert not await quota.check_and_consume(
        caller_id="person:a", tool_name="list_businesses", tool_class="read"
    )


async def test_read_class_never_touches_the_writes_bucket() -> None:
    quota = _quota(writes_limit_per_minute=1)
    for name in ("list_businesses", "list_platform_accounts", "get_portfolio_overview"):
        assert await quota.check_and_consume(
            caller_id="person:a", tool_name=name, tool_class="read"
        )


async def test_writes_bucket_is_shared_across_distinct_tool_names_of_any_write_class() -> None:
    quota = _quota(writes_limit_per_minute=2)
    assert await quota.check_and_consume(
        caller_id="person:a", tool_name="propose_a", tool_class="proposal"
    )
    assert await quota.check_and_consume(
        caller_id="person:a", tool_name="create_offering", tool_class="catalog_write"
    )
    assert not await quota.check_and_consume(
        caller_id="person:a", tool_name="connect_platform_account", tool_class="connection_write"
    )


async def test_writes_bucket_is_scoped_per_caller() -> None:
    quota = _quota(writes_limit_per_minute=1)
    assert await quota.check_and_consume(
        caller_id="person:a", tool_name="propose_a", tool_class="proposal"
    )
    assert await quota.check_and_consume(
        caller_id="person:b", tool_name="propose_a", tool_class="proposal"
    )


async def test_denied_call_does_not_partially_consume_the_shared_writes_bucket() -> None:
    quota = _quota(writes_limit_per_minute=2, per_tool_limits={"propose_a": 1})
    assert await quota.check_and_consume(
        caller_id="person:a", tool_name="propose_a", tool_class="proposal"
    )
    # Denegada por el limite PROPIO de `propose_a` (1/1) -- el cubo
    # compartido de escrituras se queda en 1/2, sin tocar.
    assert not await quota.check_and_consume(
        caller_id="person:a", tool_name="propose_a", tool_class="proposal"
    )
    # Si la llamada denegada hubiera mordido el cubo compartido, esta
    # segunda herramienta lo encontraria ya agotado (2/2).
    assert await quota.check_and_consume(
        caller_id="person:a", tool_name="propose_b", tool_class="proposal"
    )


async def test_named_tool_gets_its_own_narrower_default_limit() -> None:
    quota = _quota()
    for _ in range(30):
        assert await quota.check_and_consume(
            caller_id="person:a", tool_name="get_meta_graph", tool_class="read"
        )
    assert not await quota.check_and_consume(
        caller_id="person:a", tool_name="get_meta_graph", tool_class="read"
    )


async def test_per_tool_limit_override_replaces_the_code_default() -> None:
    quota = _quota(per_tool_limits={"upload_creative_asset": 5})
    for _ in range(5):
        assert await quota.check_and_consume(
            caller_id="person:a", tool_name="upload_creative_asset", tool_class="catalog_write"
        )
    assert not await quota.check_and_consume(
        caller_id="person:a", tool_name="upload_creative_asset", tool_class="catalog_write"
    )
