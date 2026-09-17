"""`McpRateLimiter`/`_mcp_rate_limit_key` (composition/api.py): `/mcp`
particionado en cubeta por sesion MCP (`_MCP_SESSION_RATE_LIMIT_CAPACITY`,
600/min) y cubeta sin sesion (`_MCP_SESSION_LESS_RATE_LIMIT_CAPACITY`,
60/min -- el mismo cupo de antes de este cambio, donde `initialize` todavia
no trae `Mcp-Session-Id`). Regresion: antes, todo `/mcp` compartia una
unica cubeta de 60/min -- un ciclo de agente normal (`initialize` +
`tools/list` + una docena de lecturas, varios ciclos por hora) se comia el
cupo de cualquier otra sesion abierta en el mismo proceso `ads-api`."""

from __future__ import annotations

from safent_ads.composition.api import (
    _MCP_SESSION_LESS_RATE_LIMIT_CAPACITY,
    _MCP_SESSION_RATE_LIMIT_CAPACITY,
    McpRateLimiter,
    TokenBucketRateLimiter,
    _mcp_rate_limit_key,
)


def _mcp_rate_limiter() -> McpRateLimiter:
    return McpRateLimiter(
        session_limiter=TokenBucketRateLimiter(
            capacity=_MCP_SESSION_RATE_LIMIT_CAPACITY,
            refill_per_second=_MCP_SESSION_RATE_LIMIT_CAPACITY / 60,
        ),
        session_less_limiter=TokenBucketRateLimiter(
            capacity=_MCP_SESSION_LESS_RATE_LIMIT_CAPACITY,
            refill_per_second=_MCP_SESSION_LESS_RATE_LIMIT_CAPACITY / 60,
        ),
    )


def _scope(headers: dict[bytes, bytes]) -> dict[str, object]:
    return {
        "type": "http",
        "headers": list(headers.items()),
        "client": ("203.0.113.5", 1234),
    }


def test_key_uses_mcp_session_id_header_when_present() -> None:
    key = _mcp_rate_limit_key(_scope({b"mcp-session-id": b"session-a"}))

    assert key == "session:session-a"


def test_key_falls_back_to_bearer_without_session_id() -> None:
    key = _mcp_rate_limit_key(_scope({b"authorization": b"Bearer secret-token"}))

    assert key == "anon:Bearer secret-token"


def test_key_falls_back_to_ip_without_session_id_or_bearer() -> None:
    key = _mcp_rate_limit_key(_scope({}))

    assert key == "anon:203.0.113.5"


def test_session_budget_is_exhausted_after_capacity_calls() -> None:
    limiter = _mcp_rate_limiter()
    key = "session:session-a"

    allowed = [limiter.allow(key) for _ in range(_MCP_SESSION_RATE_LIMIT_CAPACITY)]

    assert all(allowed)
    assert limiter.allow(key) is False


def test_session_less_budget_is_exhausted_after_capacity_calls() -> None:
    limiter = _mcp_rate_limiter()
    key = "anon:some-caller"

    allowed = [limiter.allow(key) for _ in range(_MCP_SESSION_LESS_RATE_LIMIT_CAPACITY)]

    assert all(allowed)
    assert limiter.allow(key) is False


def test_session_a_exhausted_does_not_affect_session_b() -> None:
    limiter = _mcp_rate_limiter()
    session_a = "session:session-a"
    session_b = "session:session-b"

    for _ in range(_MCP_SESSION_RATE_LIMIT_CAPACITY):
        assert limiter.allow(session_a) is True
    exhausted = limiter.allow(session_a)
    still_allowed = limiter.allow(session_b)

    assert exhausted is False
    assert still_allowed is True


def test_session_less_budget_still_protects_initialize_floods() -> None:
    """Una sesion ya abierta, con su cupo de sesion intacto, no libera a
    `initialize` (sin sesion) de su propio cupo -- mas estrecho -- para
    seguir cortando una inundacion de `initialize` sin sesion."""
    limiter = _mcp_rate_limiter()
    open_session = "session:session-a"
    session_less = "anon:some-caller"

    limiter.allow(open_session)
    for _ in range(_MCP_SESSION_LESS_RATE_LIMIT_CAPACITY):
        assert limiter.allow(session_less) is True
    flooded = limiter.allow(session_less)

    assert flooded is False
