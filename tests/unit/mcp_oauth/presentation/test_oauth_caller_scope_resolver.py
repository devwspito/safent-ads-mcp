"""`OAuthCallerScopeResolver` (tasks.md T010, threat-model.md C-57) tras la
fusion con lane/003: recibe el bearer CRUDO (la autenticacion de `/mcp` la
hace `SeatCredentialRouter`, no el `RequireAuthMiddleware` del SDK) y
encadena.

- Token que NO es una concesion OAuth viva -> delega en el resolutor de
  puesto (`SingleOwnerCallerScopeResolver`/`EnterpriseSeatCallerScopeResolver`).
- Bearer estatico (`STATIC_CALLER_CLIENT_ID`) -> tambien delega: el alcance
  real del dueno lo calcula el resolutor de puesto, nunca este modulo.
- Concesion OAuth viva -> `caller_id="oauth:{client_id}:{owner_id}"`,
  `granted_scopes` explicitos (nunca `None`), `allowed_business_ids`
  enumerados (nunca un comodin) y `permission` derivado del alcance:
  `ads:propose` -> `proponer`, solo `ads:read` -> `ver`. Nunca `aprobar`.
"""

from __future__ import annotations

from mcp.server.auth.provider import AccessToken

from safent_ads.mcp.application.caller_scope import CallerScope, Permission
from safent_ads.mcp_oauth.presentation.caller_scope import OAuthCallerScopeResolver
from safent_ads.mcp_oauth.presentation.token_verifier import STATIC_CALLER_CLIENT_ID

BUSINESS_A = "11111111-1111-1111-1111-111111111111"
BUSINESS_B = "22222222-2222-2222-2222-222222222222"

_FALLBACK_SCOPE = CallerScope(
    caller_id="owner",
    allowed_business_ids=frozenset({BUSINESS_A}),
    permission=Permission.APPROVE,
    person_label="Dueño",
)


class _FakeActiveBusinesses:
    async def active_business_ids(self) -> frozenset[str]:
        return frozenset({BUSINESS_A, BUSINESS_B})


class _FakeSeatResolver:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.closed = False

    async def resolve(self, bearer_token: str) -> CallerScope:
        self.calls.append(bearer_token)
        return _FALLBACK_SCOPE

    async def aclose(self) -> None:
        self.closed = True


class _FakeTokenVerifier:
    def __init__(self, access_token: AccessToken | None) -> None:
        self._access_token = access_token

    async def verify_token(self, token: str) -> AccessToken | None:  # noqa: ARG002
        return self._access_token


def _oauth_token(*, scopes: list[str], client_id: str = "claude-code") -> AccessToken:
    return AccessToken(
        token="opaque",  # noqa: S106 - valor sintetico de prueba
        client_id=client_id,
        scopes=scopes,
        expires_at=None,
        resource="https://ads.test.ts.net/mcp",
        subject="owner-1",
    )


def _resolver(
    access_token: AccessToken | None, fallback: _FakeSeatResolver
) -> OAuthCallerScopeResolver:
    return OAuthCallerScopeResolver(
        token_verifier=_FakeTokenVerifier(access_token),
        active_businesses=_FakeActiveBusinesses(),
        fallback=fallback,
    )


async def test_a_token_that_is_not_a_live_grant_falls_back_to_the_seat_resolver() -> None:
    fallback = _FakeSeatResolver()

    scope = await _resolver(None, fallback).resolve("un-bearer-cualquiera")

    assert scope == _FALLBACK_SCOPE
    assert fallback.calls == ["un-bearer-cualquiera"]


async def test_the_static_bearer_is_resolved_by_the_seat_resolver_not_here() -> None:
    """El alcance del bearer estatico son los negocios ACTIVOS con permiso
    `aprobar` (`SingleOwnerCallerScopeResolver`), nunca un `CallerScope`
    paralelo inventado en este modulo."""
    fallback = _FakeSeatResolver()
    static_token = _oauth_token(scopes=["ads:read"], client_id=STATIC_CALLER_CLIENT_ID)

    scope = await _resolver(static_token, fallback).resolve("el-token-estatico")

    assert scope == _FALLBACK_SCOPE
    assert fallback.calls == ["el-token-estatico"]


async def test_oauth_credential_resolves_caller_id_from_client_and_owner() -> None:
    scope = await _resolver(_oauth_token(scopes=["ads:read"]), _FakeSeatResolver()).resolve("tok")

    assert scope.caller_id == "oauth:claude-code:owner-1"


async def test_oauth_caller_scope_is_never_wildcard() -> None:
    scope = await _resolver(_oauth_token(scopes=["ads:read"]), _FakeSeatResolver()).resolve("tok")

    assert scope.allowed_business_ids == frozenset({BUSINESS_A, BUSINESS_B})


async def test_oauth_granted_scopes_come_from_the_access_token() -> None:
    scope = await _resolver(_oauth_token(scopes=["ads:read"]), _FakeSeatResolver()).resolve("tok")

    assert scope.granted_scopes == frozenset({"ads:read"})
    assert scope.has_scope("ads:read") is True
    assert scope.has_scope("ads:propose") is False


async def test_read_only_scope_maps_to_the_view_permission() -> None:
    scope = await _resolver(_oauth_token(scopes=["ads:read"]), _FakeSeatResolver()).resolve("tok")

    assert scope.permission is Permission.VIEW


async def test_propose_scope_maps_to_the_propose_permission_never_approve() -> None:
    """Aprobar es una decision de empresa (panel, sesion + TOTP): ningun
    alcance OAuth la concede, por mucho que pida (contracts/mcp.md SS3.4)."""
    token = _oauth_token(scopes=["ads:read", "ads:propose"])

    scope = await _resolver(token, _FakeSeatResolver()).resolve("tok")

    assert scope.permission is Permission.PROPOSE


async def test_aclose_closes_the_wrapped_seat_resolver() -> None:
    fallback = _FakeSeatResolver()

    await _resolver(None, fallback).aclose()

    assert fallback.closed is True
