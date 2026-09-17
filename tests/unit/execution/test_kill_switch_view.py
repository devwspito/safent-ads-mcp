"""No synthetic empty inventory: effective safety includes OAuth siblings."""

from datetime import UTC, datetime

import pytest

from safent_ads.execution.infrastructure.kill_switch_view import serialize_kill_switch_view

NOW = datetime(2026, 9, 14, tzinfo=UTC)


def account(identifier="a", business="business", external="123"):
    return {
        "id": identifier,
        "business_id": business,
        "platform": "google",
        "external_account_id": external,
        "account_ref": f"google:{external}",
    }


def brake(kind="platform_account", mode="ALL", **values):
    return {
        "id": "brake",
        "scope_kind": kind,
        "business_id": "business",
        "account_business_id": "business",
        "platform": "google",
        "external_account_id": "123",
        "account_ref": "google:123",
        "platform_account_id": "retired",
        "mode": mode,
        "reason": "Owner requested",
        "engaged_at": NOW,
        "engaged_by": "owner:synthetic@example.test",
        "business_name": "Synthetic business",
        **values,
    }


def test_empty_state_has_complete_explicit_nullable_contract():
    result = serialize_kill_switch_view([account()], [])
    assert result == {
        "items": [],
        "effective": {
            "engaged": False,
            "mode": None,
            "scope_kind": None,
            "scope_id": None,
            "engaged_at": None,
            "reason": None,
        },
        "by_account": [
            {"platform_account_id": "a", "engaged": False, "mode": None, "source_scope_kind": None}
        ],
    }


def test_physical_siblings_inherit_brake_without_affecting_other_assets():
    result = serialize_kill_switch_view(
        [
            account("retired"),
            account("current"),
            account("other", external="999"),
        ],
        [brake()],
    )
    assert [row["engaged"] for row in result["by_account"]] == [True, True, False]
    assert result["items"][0]["scope_id"] == "google:123"
    assert result["items"][0]["engaged_by"] == "owner:synthetic@example.test"
    assert result["effective"]["mode"] == "ALL"


@pytest.mark.parametrize("specific", ["business", "platform_account"])
def test_specific_all_beats_global_autonomous(specific):
    result = serialize_kill_switch_view(
        [account()],
        [
            brake("global", "AUTONOMOUS", id="global"),
            brake(specific),
        ],
    )
    assert len(result["items"]) == 2
    assert result["effective"]["mode"] == "ALL"
    assert result["effective"]["scope_kind"] == specific
    assert result["by_account"][0]["mode"] == "ALL"


def test_physical_equivalence_never_crosses_business_or_platform():
    foreign = account(business="other")
    meta = {**account(), "platform": "meta"}
    result = serialize_kill_switch_view([foreign, meta], [brake()])
    assert all(not row["engaged"] for row in result["by_account"])
