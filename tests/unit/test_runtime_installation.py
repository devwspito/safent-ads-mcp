import json
import plistlib
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from safent_ads.runtime import installation


@pytest.mark.parametrize(
    "url", ["http://host", "https://u:p@host", "https://host/path", "https://host?q=x"]
)
def test_origin_rejects_credentials_paths_and_insecure_transport(url):
    with pytest.raises(ValueError):
        installation.validated_origin(url)


def test_private_profile_is_atomic_restricted_and_rejects_symlinks(tmp_path):
    path = tmp_path / "profile" / "profile.json"
    data = {"url": "https://ads.example.com", "runtime": "codex", "token": "synthetic"}
    installation.private_write(path, json.dumps(data).encode())
    assert path.stat().st_mode & 0o777 == 0o600
    assert installation.read_profile(path) == data
    path.chmod(0o644)
    with pytest.raises(ValueError):
        installation.read_profile(path)
    other = tmp_path / "link"
    other.symlink_to(path)
    with pytest.raises(ValueError):
        installation.private_write(other, b"overwrite")
    assert path.read_text() == json.dumps(data)


def test_existing_mcp_is_reused_and_other_origin_never_overwritten(monkeypatch):
    operation = Mock(
        return_value=subprocess.CompletedProcess(
            [], 0, json.dumps({"transport": {"url": "https://ads.example.com/mcp"}}), ""
        )
    )
    monkeypatch.setattr(installation, "command", operation)
    installation.register_mcp("/codex", "codex", "friendog", "https://ads.example.com")
    assert operation.call_count == 1
    with pytest.raises(ValueError):
        installation.register_mcp("/codex", "codex", "friendog", "https://other.example.com")
    assert operation.call_count == 2


@pytest.mark.parametrize("runtime", ["codex", "claude"])
def test_install_uses_native_cli_argv_without_shell_or_secrets(monkeypatch, runtime):
    operation = Mock(
        side_effect=[
            subprocess.CompletedProcess([], 1, "", ""),
            subprocess.CompletedProcess([], 0, "", ""),
        ]
    )
    monkeypatch.setattr(installation, "command", operation)
    installation.register_mcp("/cli", runtime, "friendog", "https://ads.example.com")
    arguments = operation.call_args.args[0]
    assert arguments[:3] == ["/cli", "mcp", "add"]
    assert "https://ads.example.com/mcp" in arguments
    assert "token" not in " ".join(arguments)


def test_service_config_has_no_token_and_is_stable_per_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    profile = installation.profile_path("https://ads.example.com", "codex")
    identifier, path, content = installation.service_spec(profile, "Darwin")
    data = plistlib.loads(content)
    assert data["Label"] == identifier
    assert path.parent == tmp_path / "Library" / "LaunchAgents"
    assert data["ProgramArguments"][-1] == str(profile)
    assert "SAFENT_RUNTIME_TOKEN" not in content.decode()
    assert data["KeepAlive"] == {"SuccessfulExit": False}
    _, linux_path, unit = installation.service_spec(profile, "Linux")
    assert linux_path.parent == tmp_path / ".config/systemd/user"
    assert b"Restart=on-failure" in unit
    with pytest.raises(ValueError):
        installation.service_spec(profile, "Windows")


async def test_ping_does_not_claim_jobs_and_does_not_trust_cookies(monkeypatch):
    calls = []

    def handle(request):
        calls.append(request.url.path)
        assert request.headers["authorization"] == "Bearer synthetic"
        return httpx.Response(200, json={"connection_id": "id", "business_id": "business"})

    monkeypatch.setattr(
        installation,
        "build_pinned_async_client",
        lambda **_kwargs: httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )
    assert await installation.ping(
        {
            "url": "https://ads.example.com",
            "token": "synthetic",
            "connection_id": "id",
            "business_id": "business",
        }
    )
    assert calls == ["/runtime/v1/ping"]


async def test_revocation_exit_does_not_restart_daemon_forever(tmp_path, monkeypatch):
    profile = tmp_path / "profile.json"
    installation.private_write(
        profile,
        json.dumps(
            {
                "url": "https://ads.example.com",
                "runtime": "codex",
                "token": "synthetic",
                "executable": "/codex",
            }
        ).encode(),
    )
    monkeypatch.setattr(installation, "serve", AsyncMock(side_effect=SystemExit("revoked")))
    await installation.run_profile(profile)
    assert "SAFENT_RUNTIME_TOKEN" not in installation.os.environ
