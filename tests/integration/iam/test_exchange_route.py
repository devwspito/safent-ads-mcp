"""`POST /api/v1/auth/exchange` de punta a punta (026, tasks.md T002,
contracts/sso.md §4) contra Postgres real, vía `create_app` -- mismo patron
de `tests/integration/iam/test_login_lockout_persists.py`. Usa
`isolated_iam_database_url`: la resolución de propietario no filtra por
`business_id` (conftest.py: "consultas GLOBALES por diseño")."""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from safent_ads.composition.app import create_app
from safent_ads.composition.settings import ApiSettings
from safent_ads.iam.presentation.dependencies import SESSION_COOKIE_NAME

pytestmark = pytest.mark.integration

_VALID_32_BYTE_KEY_B64 = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="
_SLUG = "safent-ads"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _sign(private_key: Ed25519PrivateKey, payload: dict[str, object]) -> str:
    payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    signature = private_key.sign(payload_bytes)
    return f"{_b64url(payload_bytes)}.{_b64url(signature)}"


def _payload(*, jti: str | None = None, **overrides: object) -> dict[str, object]:
    now = datetime.now(UTC)
    base: dict[str, object] = {
        "v": 1,
        "iss": "safent-runtime",
        "aud": "safent-ads",
        "slug": _SLUG,
        "sub": "sub-owner-e2e",
        "jti": jti or str(uuid.uuid4()),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=60)).timestamp()),
        "purpose": "cockpit_session",
        "surface": "safent_cockpit",
    }
    base.update(overrides)
    return base


@pytest.fixture
def keypair() -> tuple[Ed25519PrivateKey, str]:
    private_key = Ed25519PrivateKey.generate()
    return private_key, _b64url(private_key.public_key().public_bytes_raw())


@pytest.fixture
def tls_material(tmp_path: Path) -> tuple[Path, Path]:
    certfile = tmp_path / "cert.pem"
    keyfile = tmp_path / "key.pem"
    certfile.write_text("not-a-real-cert-just-needs-to-be-readable", encoding="utf-8")
    keyfile.write_text("not-a-real-key-just-needs-to-be-readable", encoding="utf-8")
    return certfile, keyfile


def _companion_settings(
    *, database_url: str, sso_public_key: str, tls_material: tuple[Path, Path]
) -> ApiSettings:
    certfile, keyfile = tls_material
    return ApiSettings(
        database_url=database_url,
        session_secret="test-session-secret-0123456789abcdef",
        totp_enc_key=_VALID_32_BYTE_KEY_B64,
        mcp_token="test-mcp-token-abc123",
        broker_socket_path="/tmp/safent-ads-test/exchange.sock",
        public_base_url="https://ads.safent.internal:8443",
        telegram_bot_token="123456:test-bot-token",
        telegram_owner_chat_ids=[111222333],
        approval_signing_key=_VALID_32_BYTE_KEY_B64,
        companion_mode=True,
        tls_certfile=certfile,
        tls_keyfile=keyfile,
        sso_public_key=sso_public_key,
        single_owner_mode=True,
    )


@pytest.fixture
async def clean_isolated_db(isolated_iam_database_url: str) -> AsyncIterator[str]:
    engine = create_async_engine(isolated_iam_database_url, pool_pre_ping=True)
    async with engine.begin() as connection:
        # This module owns its database; deleting fixture rows never truncates
        # append-only approval history through the connection ownership FK.
        await connection.execute(text("DELETE FROM sessions"))
        await connection.execute(text("DELETE FROM owners"))
        await connection.execute(text("DELETE FROM sso_assertions_seen"))
    try:
        yield isolated_iam_database_url
    finally:
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM sessions"))
            await connection.execute(text("DELETE FROM owners"))
            await connection.execute(text("DELETE FROM sso_assertions_seen"))
        await engine.dispose()


async def test_exchange_is_404_outside_companion_mode(api_settings: ApiSettings) -> None:
    app = create_app(api_settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://test") as client:
        response = await client.post("/api/v1/auth/exchange", json={"assertion": "whatever"})

    assert response.status_code == 404


async def test_successful_exchange_sets_the_session_cookie(
    clean_isolated_db: str,
    keypair: tuple[Ed25519PrivateKey, str],
    tls_material: tuple[Path, Path],
) -> None:
    private_key, public_key_b64 = keypair
    settings = _companion_settings(
        database_url=clean_isolated_db, sso_public_key=public_key_b64, tls_material=tls_material
    )
    app = create_app(settings)
    assertion = _sign(private_key, _payload())

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://test") as client:
        response = await client.post("/api/v1/auth/exchange", json={"assertion": assertion})

    assert response.status_code == 200, response.text
    assert SESSION_COOKIE_NAME in response.cookies
    body = response.json()
    assert body["surface"] == "safent_cockpit"
    assert "totp_enrolled" not in body


async def test_forged_signature_is_rejected(
    clean_isolated_db: str,
    keypair: tuple[Ed25519PrivateKey, str],
    tls_material: tuple[Path, Path],
) -> None:
    _, public_key_b64 = keypair
    forger_key = Ed25519PrivateKey.generate()
    settings = _companion_settings(
        database_url=clean_isolated_db, sso_public_key=public_key_b64, tls_material=tls_material
    )
    app = create_app(settings)
    assertion = _sign(forger_key, _payload())

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://test") as client:
        response = await client.post("/api/v1/auth/exchange", json={"assertion": assertion})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "ASSERTION_INVALID"


async def test_repeated_jti_is_rejected(
    clean_isolated_db: str,
    keypair: tuple[Ed25519PrivateKey, str],
    tls_material: tuple[Path, Path],
) -> None:
    private_key, public_key_b64 = keypair
    settings = _companion_settings(
        database_url=clean_isolated_db, sso_public_key=public_key_b64, tls_material=tls_material
    )
    app = create_app(settings)
    jti = str(uuid.uuid4())
    assertion = _sign(private_key, _payload(jti=jti))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://test") as client:
        first = await client.post("/api/v1/auth/exchange", json={"assertion": assertion})
        second = await client.post("/api/v1/auth/exchange", json={"assertion": assertion})

    assert first.status_code == 200, first.text
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "ASSERTION_REPLAYED"


async def test_expired_assertion_is_rejected(
    clean_isolated_db: str,
    keypair: tuple[Ed25519PrivateKey, str],
    tls_material: tuple[Path, Path],
) -> None:
    private_key, public_key_b64 = keypair
    settings = _companion_settings(
        database_url=clean_isolated_db, sso_public_key=public_key_b64, tls_material=tls_material
    )
    app = create_app(settings)
    expired_at = datetime.now(UTC) - timedelta(minutes=5)
    assertion = _sign(
        private_key,
        _payload(
            iat=int((expired_at - timedelta(seconds=60)).timestamp()),
            exp=int(expired_at.timestamp()),
        ),
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://test") as client:
        response = await client.post("/api/v1/auth/exchange", json={"assertion": assertion})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "ASSERTION_EXPIRED"


async def test_foreign_slug_is_rejected(
    clean_isolated_db: str,
    keypair: tuple[Ed25519PrivateKey, str],
    tls_material: tuple[Path, Path],
) -> None:
    private_key, public_key_b64 = keypair
    settings = _companion_settings(
        database_url=clean_isolated_db, sso_public_key=public_key_b64, tls_material=tls_material
    )
    app = create_app(settings)
    assertion = _sign(private_key, _payload(slug="some-other-companion"))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://test") as client:
        response = await client.post("/api/v1/auth/exchange", json={"assertion": assertion})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "ASSERTION_INVALID"


async def test_owner_bound_to_another_subject_is_rejected(
    clean_isolated_db: str,
    keypair: tuple[Ed25519PrivateKey, str],
    tls_material: tuple[Path, Path],
) -> None:
    private_key, public_key_b64 = keypair
    settings = _companion_settings(
        database_url=clean_isolated_db, sso_public_key=public_key_b64, tls_material=tls_material
    )
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://test") as client:
        first = await client.post(
            "/api/v1/auth/exchange",
            json={"assertion": _sign(private_key, _payload(sub="sub-owner-a"))},
        )
        second = await client.post(
            "/api/v1/auth/exchange",
            json={"assertion": _sign(private_key, _payload(sub="sub-owner-b"))},
        )

    assert first.status_code == 200, first.text
    assert second.status_code == 403
    assert second.json()["error"]["code"] == "OWNER_BOUND_ELSEWHERE"


async def test_exchange_request_is_exempt_from_csrf(
    clean_isolated_db: str,
    keypair: tuple[Ed25519PrivateKey, str],
    tls_material: tuple[Path, Path],
) -> None:
    """Sin `X-CSRF-Token` ni cookie `ads_csrf` previa (sso.md §4: "exento de
    CSRF, no lleva cookie de sesión") -- a diferencia de `/auth/login`, no
    hace falta una petición GET previa para obtener la cookie."""
    private_key, public_key_b64 = keypair
    settings = _companion_settings(
        database_url=clean_isolated_db, sso_public_key=public_key_b64, tls_material=tls_material
    )
    app = create_app(settings)
    assertion = _sign(private_key, _payload())

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://test") as client:
        response = await client.post("/api/v1/auth/exchange", json={"assertion": assertion})

    assert response.status_code == 200, response.text
