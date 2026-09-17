"""`CallerScope` deniega por defecto (004 tasks.md A1): sin `None`, un
alcance vacio no accede a ningun negocio."""

from __future__ import annotations

import pytest

from safent_ads.mcp.application.caller_scope import CallerScope, Permission


def test_empty_scope_accesses_no_business():
    scope = CallerScope("person:1", frozenset(), Permission.APPROVE, "Ana")

    assert scope.can_access("any-business") is False


def test_scope_only_accesses_its_own_businesses():
    scope = CallerScope("person:1", frozenset({"b1"}), Permission.VIEW, "Ana")

    assert scope.can_access("b1") is True
    assert scope.can_access("b2") is False


def test_constructor_requires_allowed_business_ids_explicitly():
    with pytest.raises(TypeError):
        CallerScope(caller_id="person:1", permission=Permission.VIEW, person_label="Ana")  # type: ignore[call-arg]


def test_constructor_requires_permission_explicitly():
    with pytest.raises(TypeError):
        CallerScope(  # type: ignore[call-arg]
            caller_id="person:1", allowed_business_ids=frozenset(), person_label="Ana"
        )


def test_allowed_business_ids_type_is_a_frozenset_not_a_plain_set():
    scope = CallerScope("person:1", frozenset({"b1"}), Permission.VIEW, "Ana")

    assert isinstance(scope.allowed_business_ids, frozenset)
