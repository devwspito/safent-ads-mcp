"""`HkdfIdentitySalt` (T220): determinista por negocio, distinta entre
negocios, sin persistir ningun secreto propio (deriva de un maestro via
HKDF, `shared/crypto/hkdf.py`)."""

from __future__ import annotations

from safent_ads.crm.infrastructure.identity_salt import HkdfIdentitySalt
from safent_ads.shared.ids import BusinessId

_MASTER_SECRET = b"0" * 32


def test_same_business_always_gets_the_same_salt() -> None:
    provider = HkdfIdentitySalt(_MASTER_SECRET)
    business_id = BusinessId.new()

    assert provider.for_business(business_id) == provider.for_business(business_id)


def test_different_businesses_get_different_salts() -> None:
    provider = HkdfIdentitySalt(_MASTER_SECRET)

    salt_a = provider.for_business(BusinessId.new())
    salt_b = provider.for_business(BusinessId.new())

    assert salt_a != salt_b


def test_different_master_secrets_give_different_salts() -> None:
    business_id = BusinessId.new()

    salt_a = HkdfIdentitySalt(_MASTER_SECRET).for_business(business_id)
    salt_b = HkdfIdentitySalt(b"1" * 32).for_business(business_id)

    assert salt_a != salt_b
