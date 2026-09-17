"""`_unauthorized_response` (mcp.presentation.http, threat-model.md C-9):
fabrica -- no un `JSONResponse` unico a nivel de modulo, compartido (y
mutable) entre toda peticion y toda la bateria de tests del mismo proceso
de pytest.

`BearerTokenMiddleware` (la comparacion de bearer en tiempo constante que
antes vivia aqui) se borro (T045, revision de seguridad 17-sep): la
especificacion de OAuth ya la daba por borrada desde su fusion, pero la
clase seguia en el modulo sin que ningun camino de composicion la
montara -- una puerta sin cablear que un cableado futuro podia reconectar
sin darse cuenta. `SeatCredentialRouter` es el unico guardian de `/mcp`."""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.responses import PlainTextResponse

from safent_ads.mcp.presentation import http as mcp_http
from safent_ads.mcp.presentation.http import (
    ContentLengthLimitMiddleware,
    _allowed_hosts_for,
    _transport_security_for,
)

_PUBLIC_BASE_URL = "https://ads.example.com"


def test_two_consecutive_unauthorized_responses_are_independent_instances() -> None:
    first = mcp_http._unauthorized_response()
    second = mcp_http._unauthorized_response()

    assert first is not second


def test_two_consecutive_unauthorized_responses_do_not_share_header_mutations() -> None:
    first = mcp_http._unauthorized_response()
    first.headers["retry-after"] = "999"

    second = mcp_http._unauthorized_response()

    assert "retry-after" not in second.headers


def _content_length_client(*, max_body_bytes: int = 10) -> TestClient:
    app = FastAPI()
    reached = {"count": 0}

    @app.post("/mcp")
    async def _mcp_endpoint() -> PlainTextResponse:
        reached["count"] += 1
        return PlainTextResponse("ok")

    app.state.reached = reached
    app.add_middleware(ContentLengthLimitMiddleware, max_body_bytes=max_body_bytes)
    return TestClient(app)


def test_a_declared_content_length_over_the_cap_is_rejected_with_413() -> None:
    """M-4: el cuerpo nunca llega al parser del SDK -- `Content-Length`
    solo, sin cuerpo real, ya basta para rechazar."""
    client = _content_length_client(max_body_bytes=10)

    response = client.post("/mcp", content=b"x" * 20)

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"
    assert client.app.state.reached["count"] == 0  # type: ignore[attr-defined]


def test_a_declared_content_length_within_the_cap_reaches_the_app() -> None:
    client = _content_length_client(max_body_bytes=1_000)

    response = client.post("/mcp", content=b"x" * 10)

    assert response.status_code == 200
    assert client.app.state.reached["count"] == 1  # type: ignore[attr-defined]


def _chunked_body_client(*, max_body_bytes: int) -> tuple[TestClient, dict[str, int]]:
    """Endpoint que de verdad lee el cuerpo (`request.body()`), como hace
    el SDK MCP -- si el `Middleware` no consume `receive` el cuerpo nunca
    se cuenta, y la prueba no fijaria nada real."""
    app = FastAPI()
    reached = {"count": 0, "bytes": 0}

    @app.post("/mcp")
    async def _mcp_endpoint(request: Request) -> PlainTextResponse:
        body = await request.body()
        reached["count"] += 1
        reached["bytes"] = len(body)
        return PlainTextResponse("ok")

    app.state.reached = reached
    app.add_middleware(ContentLengthLimitMiddleware, max_body_bytes=max_body_bytes)
    return TestClient(app), reached


def _streamed_body_without_content_length(
    total_bytes: int, *, chunk_size: int = 10
) -> Iterator[bytes]:
    sent = 0
    while sent < total_bytes:
        chunk = min(chunk_size, total_bytes - sent)
        yield b"x" * chunk
        sent += chunk


def test_r3_a_streamed_body_without_content_length_is_capped_mid_stream() -> None:
    """R-3: sin `Content-Length` (equivalente a `Transfer-Encoding:
    chunked`) el tope anterior no contaba nada -- el cuerpo entero llegaba
    al handler sin cota. Ahora `receive` cuenta bytes en marcha y aborta
    con 413 en cuanto supera el tope, sin que el handler llegue a leer
    nada."""
    client, reached = _chunked_body_client(max_body_bytes=10)

    response = client.post("/mcp", content=_streamed_body_without_content_length(50))

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"
    assert reached == {"count": 0, "bytes": 0}


def test_r3_a_streamed_body_within_the_cap_still_reaches_the_app() -> None:
    client, reached = _chunked_body_client(max_body_bytes=1_000)

    response = client.post("/mcp", content=_streamed_body_without_content_length(50))

    assert response.status_code == 200
    assert reached == {"count": 1, "bytes": 50}


def test_transport_security_for_without_extra_hosts_keeps_only_the_public_domain() -> None:
    """Defecto real en la instancia de produccion (0.2.21): sin
    `ADS_MCP_EXTRA_ALLOWED_HOSTS`, el comportamiento debe ser identico a antes de anadir
    el parametro -- ningun host/origen extra."""
    security = _transport_security_for(_PUBLIC_BASE_URL)

    assert security.allowed_hosts == ["ads.example.com", "127.0.0.1", "localhost"]
    assert security.allowed_origins == [
        _PUBLIC_BASE_URL,
        "https://127.0.0.1",
        "http://127.0.0.1",
    ]


def test_transport_security_for_merges_extra_hosts_as_https_only() -> None:
    """`ADS_MCP_EXTRA_ALLOWED_HOSTS` (hostname provisional, p.ej. sslip.io
    servido por el mismo Caddy): siempre como origen `https://`, nunca
    `http://`, ademas del dominio publico real."""
    security = _transport_security_for(
        _PUBLIC_BASE_URL, extra_allowed_hosts=frozenset({"ads.example.test"})
    )

    assert "ads.example.test" in security.allowed_hosts
    assert "https://ads.example.test" in security.allowed_origins
    assert "http://ads.example.test" not in security.allowed_origins


def test_allowed_hosts_for_without_extra_hosts_keeps_only_the_public_domain() -> None:
    assert _allowed_hosts_for(_PUBLIC_BASE_URL) == frozenset(
        {"ads.example.com", "127.0.0.1", "localhost"}
    )


def test_allowed_hosts_for_merges_extra_hosts() -> None:
    hosts = _allowed_hosts_for(
        _PUBLIC_BASE_URL, extra_allowed_hosts=frozenset({"ads.example.test"})
    )

    assert hosts == frozenset(
        {"ads.example.com", "127.0.0.1", "localhost", "ads.example.test"}
    )


def test_a_request_without_a_body_is_never_rejected() -> None:
    """streamable-http usa tambien `GET`/`DELETE` sin `Content-Length`:
    nada que limitar, se deja pasar."""
    app = FastAPI()

    @app.get("/mcp")
    async def _mcp_endpoint() -> PlainTextResponse:
        return PlainTextResponse("ok")

    app.add_middleware(ContentLengthLimitMiddleware, max_body_bytes=10)
    client = TestClient(app)

    response = client.get("/mcp")

    assert response.status_code == 200
