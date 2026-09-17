"""`EnterpriseSeatCallerScopeResolver` (004 tasks.md A4): traduce
`SeatAdmission` a `CallerScope`. Una admision sin negocio produce alcance
VACIO, nunca total; `caller_id` es estable."""

from __future__ import annotations

from safent_ads.mcp.application.caller_scope import Permission
from safent_ads.mcp.application.seat_authority import SeatAdmission
from safent_ads.mcp.infrastructure.enterprise_seat_caller_scope_resolver import (
    EnterpriseSeatCallerScopeResolver,
)
from safent_ads.mcp.testing.fakes import FakeSeatAuthority

_CREDENTIAL = "sfa_" + "a" * 64


def _admission(*, business_id: str = "biz-1", permission: Permission = Permission.PROPOSE):
    return SeatAdmission(
        org_id="org-1",
        user_id="user-1",
        person_label="Ana",
        seat_id="seat-1",
        business_id=business_id,
        permission=permission,
        expires_at=1_800_000_060,
    )


async def test_resolves_admission_to_a_scoped_single_business():
    authority = FakeSeatAuthority()
    authority.admit(_CREDENTIAL, _admission(business_id="biz-1"))
    resolver = EnterpriseSeatCallerScopeResolver(authority)

    scope = await resolver.resolve(_CREDENTIAL)

    assert scope.caller_id == "person:user-1"
    assert scope.allowed_business_ids == frozenset({"biz-1"})
    assert scope.permission is Permission.PROPOSE
    assert scope.person_label == "Ana"


async def test_admission_without_a_business_produces_an_empty_scope_never_total():
    authority = FakeSeatAuthority()
    authority.admit(_CREDENTIAL, _admission(business_id=""))
    resolver = EnterpriseSeatCallerScopeResolver(authority)

    scope = await resolver.resolve(_CREDENTIAL)

    assert scope.allowed_business_ids == frozenset()
    assert scope.can_access("anything") is False


async def test_caller_id_is_stable_across_resolutions_for_the_same_user():
    authority = FakeSeatAuthority()
    authority.admit(_CREDENTIAL, _admission())
    resolver = EnterpriseSeatCallerScopeResolver(authority)

    first = await resolver.resolve(_CREDENTIAL)
    second = await resolver.resolve(_CREDENTIAL)

    assert first.caller_id == second.caller_id == "person:user-1"
