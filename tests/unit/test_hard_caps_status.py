"""Loaded-policy ACK is not a digest of a newer unconsumed host file."""

import asyncio
import hashlib
import json
import os
import sys
from unittest.mock import Mock

import pytest

from safent_ads.broker.infrastructure.caps_config import CapsConfigError, load_caps_snapshot
from safent_ads.broker.presentation.dispatcher import BrokerRuntime, handle_payload
from safent_ads.broker.presentation.socket_server import serve
from safent_ads.tools import hard_caps_status
from tests.unit.test_caps_config import _VALID_YAML


def _runtime(status=None):
    return BrokerRuntime(
        adapters=Mock(), oauth_flow=Mock(), app_credentials=Mock(), hard_caps_status=status
    )


async def test_digest_is_exact_loaded_bytes_and_does_not_follow_replaced_file(tmp_path):
    path = tmp_path / "caps.yaml"
    raw = _VALID_YAML.replace("\n", "\r\n").encode()
    path.write_bytes(raw)
    loaded = load_caps_snapshot(path)
    runtime = _runtime(loaded.status)
    changed = raw + b"\r\n# replacement not yet applied\r\n"
    path.write_bytes(changed)
    response = json.loads(await handle_payload(b'{"op":"get_hard_caps_status"}', runtime))
    assert response == {
        "ok": True,
        "result": {
            "caps_digest": hashlib.sha256(raw).hexdigest(),
            "accounts_count": 2,
            # spec 008 T030/D14: aditivos. Sin sobre declarado no hay estado
            # del panel que informar, y `caps_digest` no cambia por ello.
            "panel_state_digest": None,
            "panel_accounts_count": 0,
        },
    }
    assert loaded.caps.resolve("acc-1").daily_cap_minor == 10000
    reloaded = load_caps_snapshot(path)
    assert reloaded.status.caps_digest == hashlib.sha256(changed).hexdigest()
    assert reloaded.status.caps_digest != loaded.status.caps_digest
    runtime.adapters.assert_not_called()
    runtime.oauth_flow.assert_not_called()


@pytest.mark.parametrize(
    "payload",
    [
        {"op": "get_hard_caps_status", "path": "/arbitrary"},
        {"op": "get_hard_caps_status", "reload": True},
        {"op": "get_hard_caps_status", "accounts": {}},
    ],
)
async def test_status_request_has_no_read_file_or_mutation_parameters(payload):
    reply = json.loads(await handle_payload(json.dumps(payload).encode(), _runtime()))
    assert reply == {"ok": False, "error_code": "DENIED", "reason": "invalid_schema"}


async def test_missing_startup_snapshot_cannot_report_success():
    reply = json.loads(await handle_payload(b'{"op":"get_hard_caps_status"}', _runtime()))
    assert reply["ok"] is False


def test_invalid_utf8_never_produces_digest(tmp_path):
    path = tmp_path / "caps.yaml"
    path.write_bytes(b"\xff")
    with pytest.raises(CapsConfigError, match="caps_invalid_encoding"):
        load_caps_snapshot(path)


@pytest.mark.parametrize(
    "result",
    [
        {"caps_digest": "A" * 64, "accounts_count": 1},
        {"caps_digest": "a" * 63, "accounts_count": 1},
        {"caps_digest": "a" * 64, "accounts_count": True},
        {"caps_digest": "a" * 64, "accounts_count": -1},
        {"caps_digest": "a" * 64, "accounts_count": 1, "key": "synthetic-sensitive"},
        {"caps_digest": "a" * 64, "accounts_count": 1, "panel_state_digest": "no-es-un-digest"},
        {"caps_digest": "a" * 64, "accounts_count": 1, "panel_accounts_count": -1},
        {"caps_digest": "a" * 64, "accounts_count": 1, "panel_accounts_count": True},
    ],
)
def test_helper_rejects_untrusted_status_shapes(result):
    with pytest.raises(ValueError):
        hard_caps_status._digest(json.dumps({"ok": True, "result": result}).encode())


def test_helper_failure_never_echoes_exception_or_arguments(monkeypatch, capsys):
    async def fail():
        raise RuntimeError("synthetic-sensitive")

    monkeypatch.setattr(hard_caps_status, "_read_status", fail)
    monkeypatch.setattr(sys, "argv", ["hard_caps_status"])
    with pytest.raises(SystemExit) as error:
        hard_caps_status.main()
    assert error.value.code == 1
    assert capsys.readouterr() == ("", "caps_status_unavailable\n")
    monkeypatch.setattr(sys, "argv", ["hard_caps_status", "synthetic-sensitive"])
    with pytest.raises(SystemExit) as error:
        hard_caps_status.main()
    assert error.value.code == 2
    assert capsys.readouterr() == ("", "caps_status_invalid_arguments\n")


async def test_real_cli_reads_running_socket_and_only_outputs_digest(tmp_path):
    path = tmp_path / "caps.yaml"
    path.write_text(_VALID_YAML)
    loaded = load_caps_snapshot(path)
    socket = tmp_path / "broker.sock"
    runtime = _runtime(loaded.status)
    server = await serve(socket, runtime, frozenset({os.getuid()}))
    async with server:
        # Only the test redirects the fixed production socket to its isolated socket.
        script = (
            "import sys; from safent_ads.tools import hard_caps_status as tool; "
            f"tool._SOCKET={str(socket)!r}; sys.argv=['hard_caps_status']; tool.main()"
        )
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
    assert process.returncode == 0 and stderr == b""
    assert (
        stdout
        == (
            json.dumps({"caps_digest": loaded.status.caps_digest}, separators=(",", ":")) + "\n"
        ).encode()
    )


async def test_helper_has_bounded_total_timeout(monkeypatch):
    async def stuck(_path):
        await asyncio.Event().wait()

    monkeypatch.setattr(asyncio, "open_unix_connection", stuck)
    monkeypatch.setattr(hard_caps_status, "_TIMEOUT_SECONDS", 0.01)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(hard_caps_status._read_status(), 0.5)
