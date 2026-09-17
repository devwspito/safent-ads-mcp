"""Provisioning subprocess has a bounded stdin-only secret boundary."""

import base64
import os
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from safent_ads.broker.infrastructure.composio_lease import ComposioLeaseStore
from tests.unit.broker.test_composio_lease import MASTER, b64, public


def run_cli(tmp_path, data, *, args=(), environment=None):
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[3] / "src")}
    if environment:
        env.update(environment)
    return subprocess.run(  # noqa: S603 - fixed module; arguments are synthetic test fixtures
        [sys.executable, "-m", "safent_ads.tools.composio_channel_key", *args],
        input=data,
        capture_output=True,
        cwd=tmp_path,
        env=env,
        timeout=10,
        check=False,
    )


def test_cli_only_emits_same_public_key_as_live_broker(tmp_path):
    result = run_cli(tmp_path, (MASTER + "\n").encode())
    assert result.returncode == 0 and result.stderr == b""
    assert len(result.stdout.splitlines()) == 1
    assert len(base64.b64decode(result.stdout.strip(), validate=True)) == 32
    assert MASTER.encode() not in result.stdout
    assert list(tmp_path.iterdir()) == []
    signer = Ed25519PrivateKey.generate()
    store = ComposioLeaseStore(
        master_key_b64=MASTER,
        issuer_public_key=b64(public(signer)),
        metadata_path=tmp_path / "broker" / "state.sqlite3",
    )
    assert result.stdout.decode() == store.channel()["public_key"] + "\n"


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"private-malformed-master-value",
        b"x" * 257,
        b"\xff",
        base64.b64encode(b"short"),
        (MASTER + "\n" + MASTER).encode(),
    ],
)
def test_invalid_and_oversize_input_never_appears_in_output(tmp_path, data):
    result = run_cli(tmp_path, data)
    assert result.returncode == 2
    assert result.stdout == b""
    assert result.stderr == b"COMPOSIO_CHANNEL_KEY_INVALID_INPUT\n"
    assert list(tmp_path.iterdir()) == []


def test_command_line_and_environment_are_not_secret_inputs(tmp_path):
    for result in (
        run_cli(tmp_path, b"", args=(MASTER,)),
        run_cli(tmp_path, b"", environment={"ADS_CREDENTIAL_MASTER_KEY": MASTER}),
    ):
        assert result.returncode == 2 and result.stdout == b""
        assert result.stderr == b"COMPOSIO_CHANNEL_KEY_INVALID_INPUT\n"


def test_installation_master_keys_produce_distinct_public_pins(tmp_path):
    first = run_cli(tmp_path, MASTER.encode())
    second = run_cli(tmp_path, base64.b64encode(b"different-master-key-32-bytes!!!!"[:32]))
    assert first.returncode == second.returncode == 0
    assert first.stdout != second.stdout
