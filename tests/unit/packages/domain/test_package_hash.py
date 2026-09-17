"""`package_hash.py` -- huella v2 del arbol declarado (T013, data-model.md
"Revision 2" §R2.1, threat-model.md BL-1).

Reutiliza `canonical_json_bytes`/`to_jsonable` de `proposals.domain.diff_hash`:
sin ellos, cambiar un caracter no cambiaria el hash de forma fiable, y
reordenar un mapa si lo cambiaria. Las pruebas con el nombre exacto que
pide `data-model.md §R2.1` viven aqui."""

from __future__ import annotations

import uuid
from dataclasses import replace

import pytest

from safent_ads.creative.domain.identifiers import AssetId
from safent_ads.packages.domain.identifiers import OfferingId
from safent_ads.packages.domain.package_hash import (
    PackageHash,
    PackageHashFormatError,
    compute_package_hash,
    package_tree_payload,
)
from safent_ads.proposals.domain.money import Money
from safent_ads.shared.ids import EntityLevel, EntityRef, PlatformCode

from .conftest import (
    business_id,
    image_creative,
    meta_ad,
    meta_ad_set,
    meta_campaign,
    meta_publish_as,
    package_rationale,
)


def _account_ref(business_uuid: uuid.UUID, external_id: str = "act_123") -> EntityRef:
    return EntityRef(
        PlatformCode.META, EntityLevel.ACCOUNT, external_id, business_uuid, uuid.uuid4()
    )


def _payload(**overrides: object) -> dict[str, object]:
    business = overrides.pop("business_id", None) or business_id()
    account = overrides.pop("account_ref", None) or _account_ref(business.value)
    defaults: dict[str, object] = {
        "business_id": business,
        "platform": PlatformCode.META,
        "account_ref": account,
        "publish_as": meta_publish_as(),
        "offering_id": OfferingId("offering-1"),
        "campaign": meta_campaign(),
        "ad_sets": (meta_ad_set(),),
        "daily_budget": Money.of("20.00"),
        "rationale": package_rationale(),
        "research": None,
    }
    defaults.update(overrides)
    return package_tree_payload(**defaults)  # type: ignore[arg-type]


class TestDeterminism:
    def test_same_tree_same_hash(self) -> None:
        business = business_id()
        account = _account_ref(business.value)
        asset_id = AssetId.new()
        ad = meta_ad(creative=image_creative(asset_id=asset_id, checksum="a" * 64))
        ad_sets = (meta_ad_set(ads=(ad,)),)

        first = compute_package_hash(
            _payload(business_id=business, account_ref=account, ad_sets=ad_sets)
        )
        second = compute_package_hash(
            _payload(business_id=business, account_ref=account, ad_sets=ad_sets)
        )

        assert first == second

    def test_hash_is_64_hex_chars(self) -> None:
        digest = compute_package_hash(_payload())

        assert len(digest.value) == 64
        assert all(c in "0123456789abcdef" for c in digest.value)

    def test_malformed_hash_value_is_rejected(self) -> None:
        with pytest.raises(PackageHashFormatError):
            PackageHash("not-a-hash")


class TestSensitivityToContent:
    def test_changing_one_character_of_a_headline_changes_the_hash(self) -> None:
        ad = meta_ad()
        changed_ad = replace(ad, copy=replace(ad.copy, headline=ad.copy.headline + "!"))

        first = compute_package_hash(_payload(ad_sets=(meta_ad_set(ads=(ad,)),)))
        second = compute_package_hash(_payload(ad_sets=(meta_ad_set(ads=(changed_ad,)),)))

        assert first != second

    def test_changing_one_character_of_an_asset_id_changes_the_hash(self) -> None:
        first_asset = AssetId.parse("01ARZ3NDEKTSV4RRFFQ69G5FAV")
        second_asset = AssetId.parse("01ARZ3NDEKTSV4RRFFQ69G5FAW")  # last char differs
        ad_a = meta_ad(creative=image_creative(asset_id=first_asset, checksum="a" * 64))
        ad_b = meta_ad(creative=image_creative(asset_id=second_asset, checksum="a" * 64))

        first = compute_package_hash(_payload(ad_sets=(meta_ad_set(ads=(ad_a,)),)))
        second = compute_package_hash(_payload(ad_sets=(meta_ad_set(ads=(ad_b,)),)))

        assert first != second

    def test_changing_one_character_of_the_asset_checksum_changes_the_hash(self) -> None:
        ad_a = meta_ad(checksum="a" * 64)
        ad_b = meta_ad(checksum="b" + "a" * 63)

        first = compute_package_hash(_payload(ad_sets=(meta_ad_set(ads=(ad_a,)),)))
        second = compute_package_hash(_payload(ad_sets=(meta_ad_set(ads=(ad_b,)),)))

        assert first != second

    def test_reordenar_claves_no_cambia_la_huella(self) -> None:
        payload = _payload()
        plan = payload["plan"]
        assert isinstance(plan, dict)
        reordered = dict(reversed(list(payload.items())))
        reordered["plan"] = dict(reversed(list(plan.items())))

        first = compute_package_hash(payload)
        second = compute_package_hash(reordered)

        assert first == second


class TestAccountBindingIsSigned:
    """threat-model.md BL-1: `PackageHash` cubre el enlace de cuenta
    (`account_ref` con `connection_id`) -- sustituyendo a la regla de v1
    ("nunca contiene identificadores de plataforma")."""

    def test_cambiar_account_ref_cambia_la_huella(self) -> None:
        business = business_id()
        first_account = _account_ref(business.value, "act_123")
        second_account = _account_ref(business.value, "act_456")

        first = compute_package_hash(_payload(business_id=business, account_ref=first_account))
        second = compute_package_hash(_payload(business_id=business, account_ref=second_account))

        assert first != second

    def test_cambiar_connection_id_cambia_la_huella(self) -> None:
        business = business_id()
        first_account = _account_ref(business.value, "act_123")
        second_account = _account_ref(business.value, "act_123")  # distinta connection_id (uuid4)

        first = compute_package_hash(_payload(business_id=business, account_ref=first_account))
        second = compute_package_hash(_payload(business_id=business, account_ref=second_account))

        assert first != second

    def test_cambiar_page_id_cambia_la_huella(self) -> None:
        first = compute_package_hash(_payload(publish_as=meta_publish_as(page_id="1")))
        second = compute_package_hash(_payload(publish_as=meta_publish_as(page_id="2")))

        assert first != second

    def test_google_package_has_no_publish_as_in_the_hash(self) -> None:
        payload = _payload(publish_as=None, platform=PlatformCode.GOOGLE)

        assert payload["publish_as"] is None

    def test_business_id_is_signed(self) -> None:
        account = _account_ref(business_id().value)
        first = compute_package_hash(_payload(business_id=business_id(), account_ref=account))
        second = compute_package_hash(_payload(business_id=business_id(), account_ref=account))

        assert first != second

    def test_offering_id_is_signed(self) -> None:
        first = compute_package_hash(_payload(offering_id=OfferingId("offering-1")))
        second = compute_package_hash(_payload(offering_id=OfferingId("offering-2")))

        assert first != second

    def test_daily_budget_is_signed_but_total_cap_is_not_part_of_the_payload(self) -> None:
        payload = _payload()
        budget = payload["budget"]

        assert isinstance(budget, dict)
        assert set(budget.keys()) == {"daily", "duration_days"}

    def test_rationale_carries_the_campaigns_success_and_kill_criterion(self) -> None:
        payload = _payload()
        rationale = payload["rationale"]

        assert isinstance(rationale, dict)
        assert rationale["success_criterion"] == meta_campaign().success_criterion
