"""`build_mcp_health_router` (mcp.presentation.health): verificador
compuesto, forma del payload y degradacion cuando la BD esta caida
(contrato del companion, en el runtime: "Lo que debe exponer el
servicio de ads"). Spec 002 (mcp_oauth) tasks.md T013/T014,
threat-model.md C-48: mismo `TokenVerifier` que `/mcp`, nunca un segundo
camino de verificacion."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp.server.auth.provider import AccessToken

from safent_ads.mcp.application.health import (
    CONTRACT_VERSION,
    AccountLinkStatusPort,
    DatabaseHealthPort,
    GetHealthStatus,
)
from safent_ads.mcp.presentation.health import build_mcp_health_router
from safent_ads.mcp_oauth.infrastructure.sha256_token_hasher import Sha256TokenHasher
from safent_ads.mcp_oauth.presentation.token_verifier import CompositeTokenVerifier
from safent_ads.shared.clock import SystemClock

_TOKEN = "test-mcp-token"  # noqa: S105 - fixture, no secreto real
_RESOURCE = "https://ads.test.ts.net/mcp"
_RESOURCE_METADATA_URL = "https://ads.test.ts.net/.well-known/oauth-protected-resource/mcp"
_REQUIRED_SCOPE = "ads:read"  # noqa: S105 - alcance, no un secreto


class _FakeAccounts:
    def __init__(self, linked: frozenset[str] = frozenset()) -> None:
        self._linked = linked

    async def linked_platforms(self) -> frozenset[str]:
        return self._linked


class _FakeDatabase:
    def __init__(self, *, healthy: bool) -> None:
        self._healthy = healthy

    async def ping(self) -> bool:
        return self._healthy


def _client(*, accounts: AccountLinkStatusPort, database: DatabaseHealthPort) -> TestClient:
    # `session_factory=None`: rama OAuth apagada -- estas pruebas ejercitan
    # solo el camino estatico, sin depender de Postgres (mismo criterio que
    # `tests/mcp/presentation/test_http.py`).
    verifier = CompositeTokenVerifier(
        session_factory=None,
        token_hasher=Sha256TokenHasher(),
        clock=SystemClock(),
        static_token=_TOKEN,
        resource=_RESOURCE,
    )
    router = build_mcp_health_router(
        get_health_status=GetHealthStatus(accounts=accounts, database=database),
        token_verifier=verifier,
        resource_metadata_url=_RESOURCE_METADATA_URL,
        required_scope=_REQUIRED_SCOPE,
        canonical_resource=_RESOURCE,
    )
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


class _StubTokenVerifier:
    """M1: verifica exactamente el `AccessToken` que la prueba quiere
    presentar, sin pasar por hash/introspeccion real -- solo hace falta
    para probar la comprobacion de alcance/recurso de la propia ruta."""

    def __init__(self, access_token: AccessToken | None) -> None:
        self._access_token = access_token

    async def verify_token(self, token: str) -> AccessToken | None:  # noqa: ARG002
        return self._access_token


def _client_with_access_token(access_token: AccessToken | None) -> TestClient:
    router = build_mcp_health_router(
        get_health_status=GetHealthStatus(
            accounts=_FakeAccounts(), database=_FakeDatabase(healthy=True)
        ),
        token_verifier=_StubTokenVerifier(access_token),
        resource_metadata_url=_RESOURCE_METADATA_URL,
        required_scope=_REQUIRED_SCOPE,
        canonical_resource=_RESOURCE,
    )
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_returns_401_without_authorization_header() -> None:
    client = _client(accounts=_FakeAccounts(), database=_FakeDatabase(healthy=True))

    response = client.get("/mcp/health")

    assert response.status_code == 401
    assert response.json() == {
        "error": "invalid_token",
        "error_description": "Authentication required",
    }
    assert _RESOURCE_METADATA_URL in response.headers["www-authenticate"]


def test_returns_401_with_wrong_bearer() -> None:
    client = _client(accounts=_FakeAccounts(), database=_FakeDatabase(healthy=True))

    response = client.get("/mcp/health", headers={"Authorization": "Bearer wrong-token"})

    assert response.status_code == 401


def test_returns_401_with_a_non_ascii_bearer_instead_of_500() -> None:
    """Un bearer que no es ASCII es una credencial invalida, o sea un 401 de
    manual. Comparandolo como `str`, `hmac.compare_digest` lanzaba
    `TypeError` y el borde respondia 500 -- que ademas distingue: le dice a
    quien prueba que ese byte llego mas lejos que los otros."""
    client = _client(accounts=_FakeAccounts(), database=_FakeDatabase(healthy=True))

    # Los bytes tal cual llegan por el cable: una cabecera HTTP es latin-1,
    # asi que un `ñ` en UTF-8 se descodifica como dos caracteres no ASCII y
    # el token que recibe el borde es un `str` que no cabe en ASCII.
    response = client.get("/mcp/health", headers={"Authorization": b"Bearer \xc3\xb1"})

    assert response.status_code == 401


def test_payload_shape_when_healthy_and_both_platforms_linked() -> None:
    client = _client(
        accounts=_FakeAccounts(frozenset({"google", "meta"})),
        database=_FakeDatabase(healthy=True),
    )

    response = client.get("/mcp/health", headers={"Authorization": f"Bearer {_TOKEN}"})

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "contract_version": CONTRACT_VERSION,
        "accounts_linked": {"google": True, "meta": True},
        "db": "ok",
    }


def test_payload_reports_no_platform_linked() -> None:
    client = _client(accounts=_FakeAccounts(), database=_FakeDatabase(healthy=True))

    response = client.get("/mcp/health", headers={"Authorization": f"Bearer {_TOKEN}"})

    assert response.json()["accounts_linked"] == {"google": False, "meta": False}


def test_degraded_when_database_is_down() -> None:
    client = _client(accounts=_FakeAccounts(), database=_FakeDatabase(healthy=False))

    response = client.get("/mcp/health", headers={"Authorization": f"Bearer {_TOKEN}"})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["db"] == "error"


def test_response_has_no_extra_fields() -> None:
    client = _client(accounts=_FakeAccounts(), database=_FakeDatabase(healthy=True))

    response = client.get("/mcp/health", headers={"Authorization": f"Bearer {_TOKEN}"})

    assert set(response.json().keys()) == {"status", "contract_version", "accounts_linked", "db"}


def test_lowercase_bearer_scheme_is_accepted() -> None:
    """M1: `shared/bearer.py::extract_bearer_token` es insensible a
    mayusculas en el esquema, igual que `BearerAuthBackend` del SDK ya lo
    es para `/mcp`."""
    client = _client(accounts=_FakeAccounts(), database=_FakeDatabase(healthy=True))

    response = client.get("/mcp/health", headers={"Authorization": f"bearer {_TOKEN}"})

    assert response.status_code == 200


def test_propose_only_grant_is_rejected_with_the_same_body_as_mcp() -> None:
    """M1: un token valido pero sin `ads:read` (solo `ads:propose`) no
    debe pasar aqui aunque `verify_token` lo acepte -- `/mcp`
    (`RequireAuthMiddleware`) ya lo exige, y esta ruta vive fuera de esa
    sub-app."""
    propose_only = AccessToken(
        token=_TOKEN,  # noqa: S106 - fixture, no secreto real
        client_id="oauth-client",
        scopes=["ads:propose"],
        expires_at=None,
        resource=_RESOURCE,
    )
    client = _client_with_access_token(propose_only)

    response = client.get("/mcp/health", headers={"Authorization": f"Bearer {_TOKEN}"})

    assert response.status_code == 401
    assert response.json() == {
        "error": "invalid_token",
        "error_description": "Authentication required",
    }
    assert _RESOURCE_METADATA_URL in response.headers["www-authenticate"]


def test_token_issued_for_a_different_resource_is_rejected() -> None:
    """M1: `ads:read` no basta si el token se emitio para OTRO recurso --
    confusion de audiencia, threat-model.md C-46/C-47."""
    wrong_resource = AccessToken(
        token=_TOKEN,  # noqa: S106 - fixture, no secreto real
        client_id="oauth-client",
        scopes=["ads:read"],
        expires_at=None,
        resource="https://ads.test.ts.net/other-resource",
    )
    client = _client_with_access_token(wrong_resource)

    response = client.get("/mcp/health", headers={"Authorization": f"Bearer {_TOKEN}"})

    assert response.status_code == 401
