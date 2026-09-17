from uuid import uuid4

import pytest

from safent_ads.accounts.domain.refs import AccountRef
from safent_ads.shared.ids import EntityLevel, EntityRef, PlatformCode


@pytest.mark.parametrize("platform", list(PlatformCode))
def test_remote_id_collisions_are_separate_connection_identities(platform: PlatformCode) -> None:
    business, connection, other = uuid4(), uuid4(), uuid4()
    left = AccountRef(platform, "123", business, connection)
    right = AccountRef(platform, "123", business, other)
    assert left != right and str(left) != str(right)
    assert AccountRef.parse(str(left)) == left
    assert AccountRef.parse(str(left)).external_account_id == "123"
    for level in EntityLevel:
        entity = EntityRef(platform, level, "customers/123/campaigns/456", business, connection)
        assert EntityRef.parse(str(entity)) == entity
        assert entity != EntityRef(platform, level, entity.external_id, business, other)


def test_scope_is_not_optional_in_half_a_reference() -> None:
    with pytest.raises(ValueError):
        EntityRef(PlatformCode.GOOGLE, EntityLevel.ACCOUNT, "123", uuid4())
