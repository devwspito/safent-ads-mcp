"""Published guidance must describe the real argument names and pending phases."""

from tests.unit.bundle.test_bundle_consistency import BUNDLE, _json, _read


def test_all_campaign_entrypoints_describe_explicit_plan_and_paused_container():
    texts = [
        _read(BUNDLE / "agent/instructions.md"),
        _json(BUNDLE / "enterprise/agent_template.json")["instructions"],
        _json(BUNDLE / "triggers/weekly-opportunities.json")["task_instruction"],
        _read(BUNDLE / "skills/calendar-event-launch/SKILL.md"),
    ]
    for content in texts:
        assert "creation_plan" in content
        assert "daily_budget_amount" in content
        assert "account_ref" in content
        assert "PAUSED" in content
        assert "contenedor" in content
        assert "pendientes" in content
        assert "no ejecutable" in content or "no es ejecutable" in content


def test_calendar_skill_proposal_step_uses_current_argument_names():
    skill = _read(BUNDLE / "skills/calendar-event-launch/SKILL.md")
    step = skill.split("8. **Proponer.**", 1)[1].split("9. **Informe", 1)[0]
    for obsolete in ("test_budget", "creative_asset_ids", "`targeting`"):
        assert obsolete not in step
    assert "offering_id" in step
    assert "targeting_seed" in step
