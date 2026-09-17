"""`register_federated_login_routes` (spec 002b tasks.md T036,
threat-model.md C-74/C-78): switch off -> routes absent; switch on with
incomplete config -> no crash at startup, only a diagnostic event with no
values; `client_secret` never appears in any log line.

Deliberately built against `register_federated_login_routes` directly, NOT
`create_app` -- the one-line wiring `composition/api.py` still needs is
tasks.md T033, left to the parent of this lane. This proves the
composition helper itself is correct, so that wiring becomes exactly a
one-liner (pattern of `test_campaign_packages_route_gating.py` /
`test_mcp_static_token_warning.py`)."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import structlog.testing
from fastapi import FastAPI

from safent_ads.composition.api import (
    _AUTH_RATE_LIMIT_PREFIX,
    _FEDERATED_CALLBACK_RATE_LIMIT_CAPACITY,
    _FEDERATED_CALLBACK_RATE_LIMIT_PREFIX,
    _FEDERATED_START_RATE_LIMIT_CAPACITY,
    _FEDERATED_START_RATE_LIMIT_PREFIX,
    _FEDERATED_STATUS_RATE_LIMIT_CAPACITY,
    _FEDERATED_STATUS_RATE_LIMIT_PREFIX,
    _make_forwarded_for_rate_limit_key,
    _rate_limit_rules,
)
from safent_ads.composition.container import Container
from safent_ads.composition.federated_routes import register_federated_login_routes
from safent_ads.composition.settings import ApiSettings
from tests.unit.composition.factories import build_api_settings

_FEDERATED_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/api/v1/auth/federated/status"),
        ("POST", "/api/v1/auth/federated/start"),
        ("GET", "/api/v1/auth/federated/callback"),
    }
)

_CONFIGURED_OVERRIDES: dict[str, Any] = {
    "federated_login_enabled": True,
    "google_oidc_client_id": "client.apps.googleusercontent.com",
    "google_oidc_client_secret": "s3cr3t-value",  # noqa: S106 - test fixture, not a real secret
    "federated_allowed_emails": ["dueno@example.com"],
}


def _route_signatures(app: FastAPI) -> set[tuple[str, str]]:
    """Aplana `app.routes`: FastAPI envuelve cada `include_router` en un
    `_IncludedRouter` perezoso (`original_router`) en vez de exponer las
    `APIRoute` directamente (mismo criterio que
    `test_campaign_packages_route_gating.py`)."""
    signatures: set[tuple[str, str]] = set()
    pending: list[Any] = list(app.routes)
    while pending:
        route = pending.pop()
        nested_router = getattr(route, "original_router", None)
        if nested_router is not None:
            pending.extend(nested_router.routes)
            continue
        path = getattr(route, "path", None)
        methods: Iterable[str] | None = getattr(route, "methods", None)
        if path is None or methods is None:
            continue
        signatures.update((method, path) for method in methods)
    return signatures


def _register(**overrides: Any) -> FastAPI:
    settings: ApiSettings = build_api_settings(**overrides)
    container = Container.build(settings)
    app = FastAPI()
    register_federated_login_routes(app, settings, container)
    return app


def test_routes_are_absent_when_the_switch_is_off() -> None:
    app = _register(federated_login_enabled=False)

    assert _route_signatures(app).isdisjoint(_FEDERATED_ROUTES)


def test_routes_are_present_when_fully_configured() -> None:
    app = _register(**_CONFIGURED_OVERRIDES)

    assert _FEDERATED_ROUTES <= _route_signatures(app)


def test_incomplete_config_does_not_crash_and_emits_a_diagnostic_event() -> None:
    with structlog.testing.capture_logs() as logs:
        app = _register(
            federated_login_enabled=True,
            google_oidc_client_id=None,
            google_oidc_client_secret=None,
            federated_allowed_emails=[],
        )

    assert _route_signatures(app).isdisjoint(_FEDERATED_ROUTES)
    assert any(entry["event"] == "federated_login_disabled_incomplete_config" for entry in logs)


def test_client_secret_never_appears_in_any_log_line() -> None:
    secret = _CONFIGURED_OVERRIDES["google_oidc_client_secret"]

    with structlog.testing.capture_logs() as logs:
        _register(**_CONFIGURED_OVERRIDES)

    assert all(secret not in repr(entry) for entry in logs)


def _scope(*, forwarded_for: bytes | None, peer_ip: str = "172.30.93.2") -> dict[str, object]:
    headers = [(b"x-forwarded-for", forwarded_for)] if forwarded_for is not None else []
    return {"type": "http", "headers": headers, "client": (peer_ip, 1234)}


def test_federated_routes_have_their_own_ip_keyed_rate_limit() -> None:
    """T084 C-76: antes, `/api/v1/auth/federated/*` no tenia regla propia y
    caia en `_AUTH_RATE_LIMIT_PREFIX` (20/min, clave por defecto). Ahora
    cada ruta federada tiene su PROPIO presupuesto y va por
    `forwarded_for_rate_limit_key`, nunca por el defecto.

    Lee la regla DIRECTAMENTE del diccionario que arma `_rate_limit_rules`
    (nit, code review 17-sep) -- nunca reconstruyendo `RateLimitMiddleware`
    solo para llamar a su metodo privado `_rule_for`: lo que este test
    verifica es la CONSTRUCCION del diccionario, no el despacho ASGI del
    middleware (que no se ejercita aqui)."""
    key_of = _make_forwarded_for_rate_limit_key(1)
    rules = _rate_limit_rules(key_of)

    for prefix, expected_capacity in (
        (_FEDERATED_START_RATE_LIMIT_PREFIX, _FEDERATED_START_RATE_LIMIT_CAPACITY),
        (_FEDERATED_CALLBACK_RATE_LIMIT_PREFIX, _FEDERATED_CALLBACK_RATE_LIMIT_CAPACITY),
        (_FEDERATED_STATUS_RATE_LIMIT_PREFIX, _FEDERATED_STATUS_RATE_LIMIT_CAPACITY),
    ):
        rule = rules[prefix]
        assert rule.key_of is key_of

        key = f"probe:{prefix}"
        allowed = [rule.limiter.allow(key) for _ in range(expected_capacity)]
        assert all(allowed)
        assert rule.limiter.allow(key) is False


def test_hammering_status_does_not_exhaust_the_login_budget() -> None:
    """Antes de C-76, `/federated/status` (publica, sin sesion) compartia
    cubo con `/login` bajo `_AUTH_RATE_LIMIT_PREFIX`: 20 golpes anonimos a
    `status` dejaban al dueno sin poder entrar. Ahora son cubos distintos."""
    key_of = _make_forwarded_for_rate_limit_key(1)
    rules = _rate_limit_rules(key_of)
    scope = _scope(forwarded_for=b"203.0.113.9")

    status_rule = rules[_FEDERATED_STATUS_RATE_LIMIT_PREFIX]
    login_rule = rules[_AUTH_RATE_LIMIT_PREFIX]
    assert status_rule is not login_rule

    status_key = status_rule.key_of(scope)
    for _ in range(_FEDERATED_STATUS_RATE_LIMIT_CAPACITY):
        assert status_rule.limiter.allow(status_key) is True
    assert status_rule.limiter.allow(status_key) is False

    assert login_rule.limiter.allow(login_rule.key_of(scope)) is True


def test_two_forwarded_for_values_get_different_buckets_with_one_trusted_hop() -> None:
    """Con `ADS_TRUSTED_PROXY_HOPS=1` (Caddy), dos dueños detras del mismo
    proxy pero con `X-Forwarded-For` distinto no comparten cupo -- antes de
    C-76 la clave por defecto ignoraba la cabecera y usaba la IP de Caddy,
    constante para todo Internet."""
    key_of = _make_forwarded_for_rate_limit_key(1)
    rules = _rate_limit_rules(key_of)
    rule = rules[_FEDERATED_START_RATE_LIMIT_PREFIX]
    scope_a = _scope(forwarded_for=b"198.51.100.10")
    scope_b = _scope(forwarded_for=b"198.51.100.20")

    key_a = rule.key_of(scope_a)
    for _ in range(_FEDERATED_START_RATE_LIMIT_CAPACITY):
        assert rule.limiter.allow(key_a) is True
    exhausted_a = rule.limiter.allow(key_a)
    still_allowed_b = rule.limiter.allow(rule.key_of(scope_b))

    assert key_a != rule.key_of(scope_b)
    assert exhausted_a is False
    assert still_allowed_b is True
