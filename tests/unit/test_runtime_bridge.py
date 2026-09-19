import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from safent_ads.mcp.application.caller_scope import Permission
from safent_ads.mcp.presentation.catalog import registries_by_permission
from safent_ads.mcp.presentation.registry import ToolClass, ToolRegistry
from safent_ads.mcp.presentation.runtime_tools import JobReportArgs, build_runtime_tools
from safent_ads.runtime.bridge import (
    output_schema,
    read_result,
    run_job,
    runtime_command,
    runtime_environment,
    safe_job,
)
from safent_ads.runtime.contracts import RuntimeResult


def valid_result():
    return {
        "outcome": "blocked",
        "summary": "Missing budget",
        "blockers": ["Budget"],
        "campaign": {"title": "Opening"},
    }


@pytest.mark.parametrize("runtime", ["codex", "claude"])
def test_runtime_commands_do_not_bypass_permissions_or_inherit_tools(tmp_path, runtime):
    command = runtime_command(runtime, "/runtime-cli", tmp_path)
    assert command[0] == "/runtime-cli"
    assert not any(
        "bypass" in arg or "danger" in arg or "skip-permissions" in arg for arg in command
    )
    if runtime == "codex":
        assert command[command.index("--sandbox") + 1] == "read-only"
        assert "--ignore-user-config" in command
    else:
        assert command[command.index("--tools") + 1] == ""
        assert "--strict-mcp-config" in command


def test_tokens_and_private_metadata_never_enter_model_context(monkeypatch):
    monkeypatch.setenv("SAFENT_RUNTIME_TOKEN", "bridge-secret")
    monkeypatch.setenv("META_ACCESS_TOKEN", "provider-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "api-secret")
    environment = runtime_environment()
    assert not any("secret" in value for value in environment.values())
    assert safe_job(
        {
            "id": "one",
            "context": {},
            "existing_drafts": [],
            "lease_token": "private",
            "holder": "secret",
        }
    ) == {"id": "one", "context": {}, "existing_drafts": []}


@pytest.mark.parametrize("runtime", ["codex", "claude"])
def test_adapter_parses_real_cli_response_shapes(tmp_path, runtime):
    data = valid_result()
    name = "result.json" if runtime == "codex" else "stdout.json"
    payload = data if runtime == "codex" else {"structured_output": data, "is_error": False}
    (tmp_path / name).write_text(json.dumps(payload))
    assert read_result(tmp_path, runtime, 0).campaign.title == "Opening"
    assert read_result(tmp_path, runtime, 1).outcome == "failed"
    (tmp_path / name).write_text("not JSON")
    assert read_result(tmp_path, runtime, 0).outcome == "failed"


@pytest.mark.parametrize(
    "changes",
    [
        {"outcome": "prepared", "campaign": None, "blockers": []},
        {"outcome": "prepared"},
        {"blockers": []},
        {"blockers": [" "]},
        {"blockers": ["x" * 501]},
        {"campaign": {"title": "Opening", "activate": True}},
        {"execute": True},
    ],
)
def test_runtime_result_rejects_fake_success_and_undeclared_actions(changes):
    with pytest.raises(ValidationError):
        RuntimeResult.model_validate(valid_result() | changes)


def test_output_schema_is_closed_and_requires_explicit_unknowns():
    schema = output_schema()
    Draft202012Validator.check_schema(schema)
    campaign = schema["$defs"]["DraftFields"]
    assert campaign["additionalProperties"] is False
    assert set(campaign["required"]) == set(campaign["properties"])
    assert campaign["properties"]["creation_plan"] == {"type": "null"}


def test_mcp_job_coordination_is_not_read_only_or_an_approval():
    registry = ToolRegistry(build_runtime_tools(AsyncMock()))
    views = registries_by_permission(registry)
    assert {tool.name for tool in views[Permission.VIEW]} == {
        "list_runtime_jobs",
        "get_runtime_job",
    }
    assert len(views[Permission.PROPOSE]) == 5
    assert registry.get("claim_runtime_job").tool_class == ToolClass.RUNTIME_WRITE
    assert registry.get("propose_runtime_result").tool_class == ToolClass.PROPOSAL


def test_report_allows_only_validated_draft_urls():
    values = {
        "business_id": str(uuid4()),
        "job_id": str(uuid4()),
        "runtime": "codex",
        "instance_id": "local",
        "lease_token": "x" * 43,
        "result": valid_result(),
    }
    values["result"]["campaign"]["landing_url"] = "https://example.com/opening"
    assert JobReportArgs.model_validate(values).result.campaign.landing_url
    values["result"]["campaign"]["landing_url"] = "http://127.0.0.1/private"
    with pytest.raises(ValidationError):
        JobReportArgs.model_validate(values)


def test_missing_runtime_output_is_a_failure(tmp_path: Path):
    assert read_result(tmp_path, "codex", 0).outcome == "failed"


async def test_real_subprocess_round_trip_without_model_or_credentials(tmp_path, monkeypatch):
    # A deterministic fake CLI tests transport/process handling, not a second AI agent.
    program = tmp_path / "fake_runtime.py"
    program.write_text(
        "import sys,json,os,time\nfrom pathlib import Path\n"
        "payload=sys.stdin.read()\n"
        "assert 'lease-private-value' not in payload\n"
        "assert 'SAFENT_RUNTIME_TOKEN' not in os.environ\n"
        "time.sleep(0.1)\n"
        f"Path('result.json').write_text({json.dumps(json.dumps(valid_result()))})\n"
    )
    monkeypatch.setenv("SAFENT_RUNTIME_TOKEN", "bridge-private-value")
    monkeypatch.setattr(
        "safent_ads.runtime.bridge.runtime_command", lambda *_args: [sys.executable, str(program)]
    )
    monkeypatch.setattr("safent_ads.runtime.bridge.HEARTBEAT_SECONDS", 0.03)
    calls = []

    async def handle(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"state": "running"})

    job = {"id": "job", "lease_token": "lease-private-value", "context": {}, "existing_drafts": []}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="https://example.com/"
    ) as client:
        result = await asyncio.wait_for(run_job(client, job, "codex", "fake", 60), timeout=5)
    assert result.campaign.title == "Opening"
    assert calls and calls[0]["lease_token"] == "lease-private-value"  # noqa: S105 - synthetic lease


async def test_revocation_terminates_the_running_cli(tmp_path, monkeypatch):
    program = tmp_path / "slow_runtime.py"
    program.write_text("import sys,time\nsys.stdin.read()\ntime.sleep(60)\n")
    monkeypatch.setattr(
        "safent_ads.runtime.bridge.runtime_command", lambda *_args: [sys.executable, str(program)]
    )
    monkeypatch.setattr("safent_ads.runtime.bridge.HEARTBEAT_SECONDS", 0.03)
    job = {"id": "job", "lease_token": "private", "context": {}, "existing_drafts": []}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(401)),
        base_url="https://example.com/",
    ) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await asyncio.wait_for(run_job(client, job, "codex", "fake", 60), timeout=5)
