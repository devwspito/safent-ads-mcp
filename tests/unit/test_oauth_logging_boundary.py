"""Actual configure_logging + Meta adapter + HTTPX, with zero network calls."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

from safent_ads.broker.platforms.error_sanitizer import redact_sdk_error
from safent_ads.logging_setup import _REDACTED, redact_secrets


@pytest.mark.parametrize("level", ["INFO", "DEBUG"])
def test_real_logging_boundary_never_emits_oauth_wire_or_exception_bodies(level):
    script = textwrap.dedent(r"""
        import asyncio, contextlib, io, logging, logging.config
        from datetime import UTC, datetime
        import httpx, structlog
        import safent_ads.broker.platforms.oauth_http as transport
        from safent_ads.broker.platforms.meta_oauth_adapter import (
            MetaOAuthAdapter, MetaOAuthAdapterConfig,
        )
        from safent_ads.logging_setup import configure_logging
        from uvicorn.config import LOGGING_CONFIG

        capture = io.StringIO()
        marker = "SYNTHETIC_PRIVATE_CANARY"
        calls = []
        def response(request):
            calls.append(request)
            return httpx.Response(200, json={"access_token":marker+"_token", "expires_in":60})
        def client_factory(**kwargs):
            return httpx.AsyncClient(transport=httpx.MockTransport(response), **kwargs)
        transport.build_guarded_async_client = client_factory
        class Clock:
            def now(self): return datetime.now(UTC)
        wire_child = logging.getLogger("httpcore.custom")
        wire_child.propagate = False
        wire_child.addHandler(logging.StreamHandler(capture))
        async def run():
            with contextlib.redirect_stdout(capture), contextlib.redirect_stderr(capture):
                logging.config.dictConfig(LOGGING_CONFIG)
                configure_logging(level=logging.LEVEL)
                access = logging.getLogger("uvicorn.access")
                access.setLevel(logging.DEBUG)
                access.info("%s - %s %s HTTP/%s %d", "127.0.0.1", "GET",
                    "/ads/callback?code="+marker+"&state="+marker, "1.1", 200)
                configure_logging(level=logging.LEVEL)
                # Even diagnostic code raising vendor levels cannot bypass filters.
                for name in ("httpx", "httpcore.custom", "google.auth", "facebook_business"):
                    logger = logging.getLogger(name)
                    logger.disabled = False
                    logger.setLevel(logging.DEBUG)
                    logger.debug("opaque response %s", marker)
                    logger.warning("opaque response %s", marker)
                adapter = MetaOAuthAdapter(
                    MetaOAuthAdapterConfig("123", marker+"_secret"),
                    transport.HttpxOAuthHttpClient(), Clock(),
                )
                await adapter.exchange_code_for_long_lived_token(
                    code=marker+"_code", redirect_uri="http://127.0.0.1:4321/ads/callback",
                )
                logging.getLogger("safent.test").info(
                    "request %s", "https://graph.facebook.com/token?code="+marker,
                )
                logging.getLogger("safent.test").info(
                    "payload %s", {"client_secret":marker, "state":marker},
                )
                try:
                    raise ValueError(marker)
                except ValueError:
                    logging.getLogger("safent.test").exception("operation_failed")
                    logging.getLogger("uvicorn.error").exception("server_operation_failed")
                    structlog.get_logger().exception("structured_failed")
                structlog.get_logger().info(
                    "oauth_completed", provider="meta", state=marker, code=marker,
                )
                async def rejected():
                    return httpx.Response(400, request=httpx.Request(
                        "GET", "https://graph.facebook.com/token?code="+marker,
                    ), json={"error":{"message":marker}})
                try:
                    await transport.HttpxOAuthHttpClient()._send(rejected())
                except transport.OAuthHttpError as error:
                    assert marker not in str(error)
                    logging.getLogger("safent.test").warning("denied %s", error)
                else:
                    raise AssertionError("expected provider denial")
                async def disconnected():
                    raise httpx.ReadTimeout(marker, request=httpx.Request(
                        "GET", "https://graph.facebook.com/token?code="+marker,
                    ))
                try:
                    await transport.HttpxOAuthHttpClient()._send(disconnected())
                except transport.OAuthHttpError as error:
                    assert str(error) == "OAuth transport request failed"
                    assert error.__cause__ is None
                    logging.getLogger("safent.test").exception("transport_failed")
                    structlog.get_logger().exception("structured_transport_failed")
                else:
                    raise AssertionError("expected transport failure")
            assert len(calls) == 2
            assert calls[0].url.params["client_secret"] == marker+"_secret"
            assert calls[0].url.params["code"] == marker+"_code"
            assert calls[1].url.params["fb_exchange_token"] == marker+"_token"
            rendered = capture.getvalue()
            assert marker not in rendered, "canary escaped the logging boundary"
            assert "operation_failed" in rendered and "oauth_completed" in rendered
            assert "ValueError" in rendered
            print("offline_logging_boundary_pass")
        asyncio.run(run())
    """).replace("logging.LEVEL", f"logging.{level}")
    result = subprocess.run(  # noqa: S603 - fixed offline script, interpreter and two literal levels
        [sys.executable, "-c", script],
        env={**os.environ, "PYTHONPATH": "src"},
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "offline_logging_boundary_pass"


@pytest.mark.parametrize(
    "value",
    [
        "https://graph.facebook.com/token?client_secret=PRIVATE&code=PRIVATE",
        "https://PRIVATE@example.test/token?arbitrary=PRIVATE",
        "client_secret: 'PRIVATE'",
        '{"client_secret": "PRIVATE"}',
        "code=PRIVATE",
        "state='PRIVATE'",
        "code_verifier=PRIVATE",
        "fb_exchange_token=PRIVATE",
        "access_token=PRIVATE",
        "refresh_token=PRIVATE",
    ],
)
def test_error_strings_and_structured_messages_share_redaction(value):
    assert "PRIVATE" not in redact_sdk_error(RuntimeError(value))
    result = redact_secrets(None, "error", {"event": value})
    assert "PRIVATE" not in result["event"]


@pytest.mark.parametrize("key", ["code", "state", "nonce", "code_verifier", "code_challenge"])
def test_opaque_oauth_fields_are_redacted(key):
    assert redact_secrets(None, "info", {key: "PRIVATE"})[key] == _REDACTED
