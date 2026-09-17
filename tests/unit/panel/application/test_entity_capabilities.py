"""`entity_capabilities` (design.md §0.5-0.7): puro,
sin sesion -- solo `status` + `is_controllable`."""

from __future__ import annotations

import pytest

from safent_ads.panel.application.entity_capabilities import entity_capabilities


def test_active_controllable_entity_can_only_be_paused_or_deleted() -> None:
    capabilities = entity_capabilities(status="active", is_controllable=True)

    assert capabilities.can_pause is True
    assert capabilities.can_resume is False
    assert capabilities.can_delete is True


def test_paused_controllable_entity_can_only_be_resumed_or_deleted() -> None:
    capabilities = entity_capabilities(status="paused", is_controllable=True)

    assert capabilities.can_pause is False
    assert capabilities.can_resume is True
    assert capabilities.can_delete is True


def test_removed_entity_cannot_be_touched_again() -> None:
    capabilities = entity_capabilities(status="removed", is_controllable=True)

    assert capabilities.can_pause is False
    assert capabilities.can_resume is False
    assert capabilities.can_delete is False


@pytest.mark.parametrize("status", ["active", "paused", "drifted", "learning"])
def test_not_controllable_entity_has_no_capability(status: str) -> None:
    capabilities = entity_capabilities(status=status, is_controllable=False)

    assert capabilities.can_pause is False
    assert capabilities.can_resume is False
    assert capabilities.can_delete is False


def test_status_comparison_is_case_insensitive() -> None:
    """`ad_entities.status` viaja en MAYUSCULAS en Postgres (0003_ad_entities)
    pero en minusculas en los dobles de prueba -- ambas formas deben
    resolver la misma capacidad."""
    assert entity_capabilities(status="ACTIVE", is_controllable=True).can_pause is True
