"""M5 (secreview-mac-integration.md): denied native MCP reads must flow
through the audited `broker_op_denied` branch, the same as every other
error in `_KNOWN_ERROR_CODES` -- never through an early `except` that
returns before `logger.info` runs."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import structlog.testing

from safent_ads.broker.presentation import dispatcher
from safent_ads.broker.presentation.dispatcher import BrokerRuntime, handle_payload


def _runtime() -> BrokerRuntime:
    return BrokerRuntime(
        adapters=SimpleNamespace(),  # type: ignore[arg-type] - unused by native_ads_read
        oauth_flow=SimpleNamespace(),  # type: ignore[arg-type]
        app_credentials=SimpleNamespace(),  # type: ignore[arg-type]
        native_ads=None,
    )


async def test_denied_native_ads_read_is_audited(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = json.dumps(
        {
            "op": "native_ads_read",
            "platform": "google",
            "external_account_id": "1234567890",
        }
    ).encode()

    with structlog.testing.capture_logs() as logs:
        # Other tests may have cached the production logger before capture_logs
        # replaces its processors. Bind a fresh logger under the capture config.
        monkeypatch.setattr(dispatcher, "logger", structlog.get_logger())
        response = await handle_payload(payload, _runtime())

    assert json.loads(response) == {
        "ok": False,
        "error_code": "NATIVE_MCP_UNAVAILABLE",
        "reason": "denied",
    }
    assert {
        "event": "broker_op_denied",
        "op": "native_ads_read",
        "error_code": "NATIVE_MCP_UNAVAILABLE",
        "log_level": "info",
    } in logs
