"""Explicit, paused-only first hierarchy vertical. No inference from briefs.

Children inherit an existing campaign budget; a CPC bid is NOT daily spend.
The entire JSON plan remains in the signed diff, without normalization.
"""

import ipaddress
import re
from copy import deepcopy
from decimal import Decimal
from typing import Any, cast
from urllib.parse import urlsplit

from safent_ads.proposals.domain.campaign_creation import CampaignCreationError
from safent_ads.proposals.domain.google_channel_spec import (
    ChannelSpecError,
    FieldRule,
    GoogleBiddingStrategy,
    GoogleChildNodeKind,
    spec_for_child_type,
)
from safent_ads.proposals.domain.google_search_targeting import valid_keywords
from safent_ads.shared.ids import EntityLevel, EntityRef

_CONTROL_CHARACTER_BOUNDARY = 32
_MIN_AGE = 18
_MAX_AGE = 65


class AdChildCreationError(CampaignCreationError):
    def __init__(self) -> None:
        super().__init__("ad_child_creation_plan_invalid")


def _require(condition: bool) -> None:
    if not condition:
        raise AdChildCreationError


def _object(value: object, keys: set[str]) -> dict[str, Any]:
    _require(isinstance(value, dict) and set(value) == keys)
    return cast(dict[str, Any], value)


def _text(value: object, maximum: int) -> None:
    _require(
        isinstance(value, str)
        and bool(value.strip())
        and len(value) <= maximum
        and not any(ord(char) < _CONTROL_CHARACTER_BOUNDARY for char in value)
    )


def _texts(value: object, minimum: int, maximum: int, length: int) -> None:
    _require(isinstance(value, list) and minimum <= len(value) <= maximum)
    value = cast(list[str], value)
    for item in value:
        _text(item, length)
    _require(len(set(value)) == len(value))


def _url(value: object) -> None:
    _text(value, 2048)
    value = cast(str, value)
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        _require(
            parsed.scheme == "https"
            and parsed.port in (None, 443)
            and not parsed.username
            and not parsed.password
            and not parsed.fragment
            and "." in host
            and not host.endswith((".local", ".internal", ".localhost"))
        )
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            _require(bool(re.fullmatch(r"[A-Za-z0-9.-]+", host)))
        else:
            _require(address.is_global)
    except (ValueError, TypeError) as exc:
        raise AdChildCreationError from exc


_ASSET_GROUP_KIND = "ASSET_GROUP"
# T035 finding 3 (threat-model.md §156/§203): the exact wire shape
# `platform_completeness.py::_google_asset_group_wire` projects -- every
# sibling native validates an exact key set, `assets` was the one
# exception. `name` (`AssetGroup.name`, the owner-signed value) lives
# INSIDE `assets` on purpose (see that function's docstring) so the outer
# native-level key set (`{"kind", "final_url", "assets"}`) never changes.
# `logo`/`marketing_image`/`square_image` carry a `{creative_of:<local_ref>}`
# hole until `chokepoint_step_executor.substitute_holes` resolves it to the
# already-uploaded asset's resource name -- never a full `ImageCreativeRef`.
_ASSET_GROUP_ASSET_KEYS = {
    "headlines",
    "long_headlines",
    "descriptions",
    "business_name",
    "name",
    "logo",
    "marketing_image",
    "square_image",
}


def _validate_google_ad_set_native(native: dict[str, Any]) -> None:
    """Dispatches by node type (tasks.md T015), not by "is it Google?": an
    ad group (`GoogleAdGroupNative`) and an asset group
    (`GoogleAssetGroupNative`, Performance Max) are different shapes with
    different admitted fields."""
    if native.get("kind") == _ASSET_GROUP_KIND:
        _validate_google_asset_group_native(native)
        return
    _validate_google_ad_group_native(native)


def _validate_google_asset_group_native(native: dict[str, Any]) -> None:
    # `package_hash` binding lives in the envelope/binding, not in `assets`
    # -- this only fixes the exact key set of the PLAN, untouched otherwise.
    native = _object(native, {"kind", "final_url", "assets"})
    _url(native["final_url"])
    _object(native["assets"], _ASSET_GROUP_ASSET_KEYS)


def _validate_google_ad_group_native(native: dict[str, Any]) -> None:
    node_type = native.get("type")
    _require(isinstance(node_type, str))
    try:
        spec = spec_for_child_type(cast(str, node_type))
    except ChannelSpecError as error:
        raise AdChildCreationError from error
    _require(spec.child_node is GoogleChildNodeKind.AD_GROUP)
    _require_not_forbidden(native, "keywords", spec.keywords)
    _require_not_forbidden(native, "cpc_bid", spec.cpc_bid)
    if "keywords" in native:
        _require(valid_keywords(native["keywords"]))
    allowed = {"name", "type", "bidding_strategy", "targeting_mode"}
    allowed |= {"keywords"} if "keywords" in native else set()
    allowed |= {"cpc_bid"} if "cpc_bid" in native else set()
    native = _object(native, allowed)
    _text(native["name"], 128)
    _require(
        native["bidding_strategy"] == GoogleBiddingStrategy.MANUAL_CPC
        and native["targeting_mode"] == "INHERIT_CAMPAIGN"
    )
    if "cpc_bid" in native:
        _require_cpc_bid(native["cpc_bid"])


def _require_not_forbidden(native: dict[str, Any], key: str, rule: FieldRule) -> None:
    if rule is FieldRule.FORBIDDEN and key in native:
        raise AdChildCreationError


def _require_cpc_bid(value: object) -> None:
    bid = _object(value, {"amount", "currency"})
    _require(
        bid["currency"] == "EUR"
        and isinstance(bid["amount"], str)
        and bool(re.fullmatch(r"[0-9]{1,6}(\.[0-9]{1,2})?", bid["amount"]))
    )
    _require(Decimal(bid["amount"]) > 0)


def validate_child_payload(payload: object, parent: EntityRef | None = None) -> dict[str, Any]:
    wrapper = _object(payload, {"child_plan"})
    plan = _object(
        wrapper["child_plan"], {"schema_version", "platform", "kind", "status", "native"}
    )
    _require(type(plan["schema_version"]) is int and plan["schema_version"] == 1)
    platform, kind = plan["platform"], plan["kind"]
    _require(isinstance(platform, str) and platform in ("google", "meta"))
    _require(isinstance(kind, str) and kind in ("ad_set", "ad"))
    _require(plan["status"] == "PAUSED")
    if parent is not None:
        _require(parent.platform.value == platform)
        _require(parent.level == (EntityLevel.CAMPAIGN if kind == "ad_set" else EntityLevel.AD_SET))
    native = plan["native"]
    if platform == "google" and kind == "ad_set":
        _require(isinstance(native, dict))
        _validate_google_ad_set_native(cast(dict[str, Any], native))
    elif platform == "google":
        native = _object(native, {"type", "headlines", "descriptions", "final_url"})
        _require(native["type"] == "RESPONSIVE_SEARCH_AD")
        _texts(native["headlines"], 3, 15, 30)
        _texts(native["descriptions"], 2, 4, 90)
        _url(native["final_url"])
    elif kind == "ad_set":
        native = _object(
            native,
            {
                "name",
                "budget_mode",
                "billing_event",
                "optimization_goal",
                "destination_type",
                "targeting",
                "dsa_beneficiary",
                "dsa_payor",
            },
        )
        for field in ("name", "dsa_beneficiary", "dsa_payor"):
            _text(native[field], 128)
        _require(
            native["budget_mode"] == "CAMPAIGN"
            and native["billing_event"] == "IMPRESSIONS"
            and native["optimization_goal"] == "LINK_CLICKS"
            and native["destination_type"] == "WEBSITE"
        )
        targeting = _object(
            native["targeting"], {"geo_locations", "age_min", "age_max", "targeting_automation"}
        )
        countries = _object(targeting["geo_locations"], {"countries"})["countries"]
        _texts(countries, 1, 25, 2)
        _require(all(re.fullmatch(r"[A-Z]{2}", country) for country in countries))
        _require(
            type(targeting["age_min"]) is int
            and type(targeting["age_max"]) is int
            and _MIN_AGE <= targeting["age_min"] <= targeting["age_max"] <= _MAX_AGE
        )
        automation = _object(targeting["targeting_automation"], {"advantage_audience"})
        _require(
            type(automation["advantage_audience"]) is int and automation["advantage_audience"] == 0
        )
    else:
        _require(isinstance(native, dict))
        native = cast(dict[str, Any], native)
        if "creative_inline" in native:
            native = _object(native, {"name", "creative_inline"})
            _meta_inline(native["creative_inline"])
        else:
            native = _object(native, {"name", "creative_id"})
            _require(
                isinstance(native["creative_id"], str)
                and bool(re.fullmatch(r"[0-9]{1,32}", native["creative_id"]))
            )
        _text(native["name"], 128)
    return deepcopy(plan)


_IMAGE_HASH_PATTERN = re.compile(r"[0-9a-f]{32,64}")
_META_LINK_DATA_COMMON_KEYS = {"link", "message", "name", "description", "call_to_action"}


def _image_hash(value: object) -> None:
    _require(isinstance(value, str) and bool(_IMAGE_HASH_PATTERN.fullmatch(value)))


def _meta_inline(value: object) -> None:
    creative = _object(value, {"object_story_spec"})
    story = _object(creative["object_story_spec"], {"page_id", "link_data"})
    _require(
        isinstance(story["page_id"], str) and bool(re.fullmatch(r"[0-9]{1,32}", story["page_id"]))
    )
    _require(isinstance(story["link_data"], dict))
    link = _meta_link_data(cast(dict[str, Any], story["link_data"]))
    _url(link["link"])
    for field, maximum in (("message", 2000), ("name", 128), ("description", 256)):
        _text(link[field], maximum)
    call = _object(link["call_to_action"], {"type", "value"})
    _require(call["type"] in ("LEARN_MORE", "SHOP_NOW", "SIGN_UP", "CONTACT_US", "BOOK_TRAVEL"))
    target = _object(call["value"], {"link"})
    _require(target["link"] == link["link"])


def _meta_link_data(link_data: dict[str, Any]) -> dict[str, Any]:
    """003-paquete-de-campana R2.7 (BL-6): un anuncio de PAQUETE nunca
    proyecta `picture` -- `platform_completeness.ad_wire_plan` solo sabe
    emitir `image_hash` (el manejador confirmado que devuelve el bróker al
    subir la creatividad, R7). `picture` sigue vivo para la creacion de
    anuncio SUELTA (`propose_ad_child`, `mcp/presentation/ad_child_args.py`),
    que hoy no tiene forma de obtener un `image_hash` sin pasar por una
    saga de paquete -- retirarlo de ahi es un cambio de contrato aparte,
    fuera de esta entrega. Discriminado por la clave presente, nunca por
    una bandera del llamante: la forma de los datos decide, como el resto
    de este fichero (`if "creative_inline" in native`, `if platform ==
    "google"`)."""
    if "image_hash" in link_data:
        link = _object(link_data, _META_LINK_DATA_COMMON_KEYS | {"image_hash"})
        _image_hash(link["image_hash"])
        return link
    link = _object(link_data, _META_LINK_DATA_COMMON_KEYS | {"picture"})
    _url(link["picture"])
    return link


def child_parameter(parameter: str) -> bool:
    return parameter.startswith(("new_ad_set:", "new_ad:"))


def validate_child_diff(
    parameter: str,
    before: object,
    after: object,
    parent: EntityRef,
    expected_state_hash: str | None,
) -> dict[str, Any]:
    plan = validate_child_payload(after, parent)
    _require(before is None and bool(expected_state_hash))
    _require(parameter.startswith(f"new_{plan['kind']}:"))
    return plan
