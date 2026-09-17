from types import SimpleNamespace

import pytest

from safent_ads.accounts.domain.ad_entity import AdEntityStatus
from safent_ads.accounts.domain.budget import Budget, BudgetKind
from safent_ads.accounts.domain.money import Money
from safent_ads.composition.mcp_write_adapter import _current_budget, _pause_values
from safent_ads.mcp.application.errors import ToolValidationError


def test_pause_diff_uses_statuses_not_budget_values():
    entity = SimpleNamespace(status=AdEntityStatus.ACTIVE)
    assert _pause_values(entity) == ("active", "paused")


def test_budget_cannot_be_relabelled_as_another_currency():
    entity = SimpleNamespace(budget=Budget(Money(1000, "EUR"), BudgetKind.DAILY))
    assert _current_budget(entity, "EUR").amount == 10
    with pytest.raises(ToolValidationError):
        _current_budget(entity, "USD")


def test_already_paused_is_not_proposed_as_a_new_change():
    with pytest.raises(ToolValidationError):
        _pause_values(SimpleNamespace(status=AdEntityStatus.PAUSED))
