"""Cableado del resolutor de alcance de `/mcp` tras la fusion lane/003 +
spec 002 (`composition/app.py::_build_caller_scope_resolver`).

Es la costura que hace que `/mcp` acepte las TRES credenciales a la vez: un
token OAuth propio, el puesto de Enterprise y el bearer estatico
`ADS_MCP_TOKEN`. Sin esta cadena, encender `ADS_MCP_OAUTH_ENABLED` dejaria
`/mcp` sin OAuth (solo `GET /mcp/health` lo miraria) o, al reves, apagaria
el modo de un solo propietario."""

from __future__ import annotations

import pytest

from safent_ads.composition.app import _build_caller_scope_resolver
from safent_ads.composition.container import Container
from safent_ads.mcp.application.seat_authority import SeatAuthorityDeniedError
from safent_ads.mcp.infrastructure.enterprise_seat_caller_scope_resolver import (
    EnterpriseSeatCallerScopeResolver,
)
from safent_ads.mcp.infrastructure.single_owner_caller_scope_resolver import (
    SingleOwnerCallerScopeResolver,
)
from safent_ads.mcp_oauth.presentation.caller_scope import OAuthCallerScopeResolver
from tests.unit.composition.factories import build_api_settings


class _StubTokenVerifier:
    async def verify_token(self, token: str) -> None:  # noqa: ARG002
        return None


def _container(settings: object) -> Container:
    return Container.build(settings)  # type: ignore[arg-type]


def test_with_oauth_on_the_chain_starts_with_the_oauth_resolver() -> None:
    settings = build_api_settings(mcp_oauth_enabled=True)

    resolver = _build_caller_scope_resolver(
        settings, _container(settings), token_verifier=_StubTokenVerifier()
    )

    assert isinstance(resolver, OAuthCallerScopeResolver)


def test_with_oauth_on_the_seat_resolver_is_still_behind_it() -> None:
    """La concesion OAuth no sustituye al puesto: cualquier bearer que no
    sea una concesion viva sigue cayendo en Enterprise."""
    settings = build_api_settings(mcp_oauth_enabled=True)

    resolver = _build_caller_scope_resolver(
        settings, _container(settings), token_verifier=_StubTokenVerifier()
    )

    assert isinstance(resolver, OAuthCallerScopeResolver)
    assert isinstance(resolver._fallback, EnterpriseSeatCallerScopeResolver)  # noqa: SLF001


def test_with_oauth_off_the_seat_resolver_is_used_directly() -> None:
    settings = build_api_settings(mcp_oauth_enabled=False)

    resolver = _build_caller_scope_resolver(
        settings, _container(settings), token_verifier=_StubTokenVerifier()
    )

    assert isinstance(resolver, EnterpriseSeatCallerScopeResolver)


def test_single_owner_mode_keeps_the_static_bearer_behind_the_oauth_resolver() -> None:
    """Modo de un solo propietario (motor Hermes) con OAuth encendido: el
    bearer estatico de siempre lo sigue resolviendo
    `SingleOwnerCallerScopeResolver`, ahora detras de la cadena OAuth."""
    settings = build_api_settings(
        mcp_oauth_enabled=True, seat_authority_enabled=False, single_owner_mode=True
    )

    resolver = _build_caller_scope_resolver(
        settings, _container(settings), token_verifier=_StubTokenVerifier()
    )

    assert isinstance(resolver, OAuthCallerScopeResolver)
    assert isinstance(resolver._fallback, SingleOwnerCallerScopeResolver)  # noqa: SLF001


async def test_single_owner_mode_without_a_static_bearer_denies_every_token() -> None:
    """Sin `ADS_MCP_TOKEN` (lo normal en el producto estandar: la via
    estatica nace apagada) la cadena se construye igual -- `None` llega
    hasta el resolutor sin reventar -- y deniega todo."""
    settings = build_api_settings(
        mcp_oauth_enabled=False,
        seat_authority_enabled=False,
        single_owner_mode=True,
        mcp_token=None,
    )

    resolver = _build_caller_scope_resolver(
        settings, _container(settings), token_verifier=_StubTokenVerifier()
    )

    assert isinstance(resolver, SingleOwnerCallerScopeResolver)
    with pytest.raises(SeatAuthorityDeniedError):
        await resolver.resolve("cualquier-bearer")


async def test_single_owner_mode_with_static_token_disabled_denies_the_configured_bearer() -> None:
    """T045 (spec 008, CWE-288): `ADS_MCP_TOKEN` sigue en el entorno pero
    `ADS_MCP_STATIC_TOKEN_ENABLED=false` -- el bearer configurado, el que
    un atacante con lectura del entorno conoce, tiene que seguir sin abrir
    la puerta. Antes del fix el resolutor ignoraba el interruptor y
    comparaba igual contra `settings.mcp_token`."""
    settings = build_api_settings(
        mcp_oauth_enabled=False,
        seat_authority_enabled=False,
        single_owner_mode=True,
        mcp_static_token_enabled=False,
        mcp_token="test-mcp-token-abc123",
    )

    resolver = _build_caller_scope_resolver(
        settings, _container(settings), token_verifier=_StubTokenVerifier()
    )

    assert isinstance(resolver, SingleOwnerCallerScopeResolver)
    with pytest.raises(SeatAuthorityDeniedError):
        await resolver.resolve("test-mcp-token-abc123")
