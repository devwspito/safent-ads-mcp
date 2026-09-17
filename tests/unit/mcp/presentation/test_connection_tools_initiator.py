"""2026-09-16: in single-owner mode (`ADS_SINGLE_OWNER_MODE`) the owner's static
token was refused by `connect_platform_account` with "solo una persona con
puesto puede conectar una cuenta" -- the rule only knew seat holders. The
connection is now attributed to the installation's sole owner; anything else
still fails closed."""

from __future__ import annotations

import uuid

import pytest

from safent_ads.mcp.application.caller_scope import OWNER_CALLER_ID, CallerScope, Permission
from safent_ads.mcp.application.errors import ToolValidationError
from safent_ads.mcp.presentation.connection_tools import _initiator_owner_id

_BUSINESS = "11111111-1111-1111-1111-111111111111"


class _Lookup:
    def __init__(self, owner_id: uuid.UUID | None) -> None:
        self._owner_id = owner_id
        self.calls = 0

    async def sole_owner_id(self) -> uuid.UUID | None:
        self.calls += 1
        return self._owner_id


def _scope(caller_id: str, permission: Permission = Permission.APPROVE) -> CallerScope:
    return CallerScope(caller_id, frozenset({_BUSINESS}), permission, "x")


async def test_a_seat_holder_is_attributed_by_its_person_id() -> None:
    person = uuid.uuid4()
    lookup = _Lookup(uuid.uuid4())
    assert await _initiator_owner_id(_scope(f"person:{person}"), lookup) == person
    assert lookup.calls == 0


async def test_the_single_owner_is_attributed_to_the_sole_owner_row() -> None:
    owner = uuid.uuid4()
    assert await _initiator_owner_id(_scope(OWNER_CALLER_ID), _Lookup(owner)) == owner


async def test_the_single_owner_fails_closed_without_exactly_one_owner() -> None:
    with pytest.raises(ToolValidationError, match="unico dueño"):
        await _initiator_owner_id(_scope(OWNER_CALLER_ID), _Lookup(None))


@pytest.mark.parametrize(
    "scope",
    [_scope(OWNER_CALLER_ID, Permission.VIEW), _scope("service:cerebro"), _scope("anything")],
)
async def test_other_callers_are_refused(scope: CallerScope) -> None:
    lookup = _Lookup(uuid.uuid4())
    with pytest.raises(ToolValidationError, match="puesto o el dueño"):
        await _initiator_owner_id(scope, lookup)
    assert lookup.calls == 0
