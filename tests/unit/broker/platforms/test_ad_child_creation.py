"""Real validators, v25 protobufs, signature/SQLite receipt; provider network forbidden."""

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from google.ads.googleads.client import GoogleAdsClient
from google.auth.credentials import AnonymousCredentials

from safent_ads.accounts.application.ports import WriteIntent, WriteOperation
from safent_ads.broker.domain.operation_semantics import matches_signed_transition
from safent_ads.broker.infrastructure.caps_config import parse_caps_config
from safent_ads.broker.infrastructure.write_ledger_store import WriteLedgerStore
from safent_ads.broker.platforms.ad_child_creation import (
    _google_asset_group_operation_count,
    _operation_count,
    create_paused_child,
)
from safent_ads.broker.platforms.errors import CredentialNotConnectedError
from safent_ads.broker.platforms.native_ad_child import (
    google_create,
    google_prepare,
    meta_create,
    meta_prepare,
)
from safent_ads.broker.platforms.write_pipeline import WriteAuthorizationPipeline
from safent_ads.execution.domain.guardrails import money_pair_from_diff
from safent_ads.iam.presentation.errors import ApiError
from safent_ads.proposals.domain.ad_child_creation import (
    AdChildCreationError,
    validate_child_payload,
)
from safent_ads.proposals.domain.diff_hash import compute_diff_hash
from safent_ads.proposals.domain.google_channel_spec import GoogleAdvertisingChannelType
from safent_ads.proposals.domain.proposal import ProposedDiff
from safent_ads.proposals.presentation.rest import _edited_value
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import EntityRef
from tests.integration.composition.test_write_path_end_to_end import _caps_yaml
from tests.unit.broker.ledger_scope_fakes import fake_scope
from tests.unit.broker.platforms.test_live_meta_graph_client import _client
from tests.unit.broker.platforms.test_write_pipeline import (
    _NOW,
    _authorization,
    _signer_and_verifier,
)


def child_plan(platform="google", kind="ad_set"):
    natives = {
        ("google", "ad_set"): {
            "name": "Grupo aprobado",
            "type": "SEARCH_STANDARD",
            "bidding_strategy": "MANUAL_CPC",
            "cpc_bid": {"amount": "1.25", "currency": "EUR"},
            "targeting_mode": "INHERIT_CAMPAIGN",
        },
        ("google", "ad"): {
            "type": "RESPONSIVE_SEARCH_AD",
            "headlines": ["Título uno", "Título dos", "Título tres"],
            "descriptions": ["Descripción uno", "Descripción dos"],
            "final_url": "https://example.com/landing",
        },
        ("meta", "ad_set"): {
            "name": "Conjunto aprobado",
            "budget_mode": "CAMPAIGN",
            "billing_event": "IMPRESSIONS",
            "optimization_goal": "LINK_CLICKS",
            "destination_type": "WEBSITE",
            "dsa_beneficiary": "Empresa anunciante",
            "dsa_payor": "Empresa pagadora",
            "targeting": {
                "geo_locations": {"countries": ["ES", "FR"]},
                "age_min": 25,
                "age_max": 55,
                "targeting_automation": {"advantage_audience": 0},
            },
        },
        ("meta", "ad"): {"name": "Anuncio aprobado", "creative_id": "987"},
    }
    return {
        "schema_version": 1,
        "platform": platform,
        "kind": kind,
        "status": "PAUSED",
        "native": natives[platform, kind],
    }


@pytest.mark.parametrize("platform", ["google", "meta"])
@pytest.mark.parametrize("kind", ["ad_set", "ad"])
def test_plan_is_exact_and_copy_isolated(platform, kind):
    plan = child_plan(platform, kind)
    copy = validate_child_payload({"child_plan": plan})
    assert copy == plan
    plan["native"].clear()
    assert copy["native"]


def test_numeric_panel_edit_cannot_destroy_child_plan_and_invalid_plan_cannot_be_signed():
    ref = EntityRef.parse("google:campaign:customers/123/campaigns/456")
    payload = {"child_plan": child_plan()}
    diff = ProposedDiff.build(ref, "new_ad_set:exact", None, payload)
    assert money_pair_from_diff(diff)[1].amount == 0
    with pytest.raises(ApiError) as error:
        _edited_value(
            SimpleNamespace(diff=diff),
            {"valor_propuesto": 10},
            frozenset({GoogleAdvertisingChannelType.SEARCH}),
        )
    assert error.value.detail["code"] == "CHILD_PLAN_EDIT_UNSUPPORTED"
    with pytest.raises(AdChildCreationError):
        money_pair_from_diff(diff.with_new_value(10))


@pytest.mark.parametrize("field", ["schema_version", "platform", "kind", "status", "native"])
@pytest.mark.parametrize("value", [None, True, [], {}, "unsupported"])
def test_total_validator_rejects_malformed_json(field, value):
    plan = child_plan()
    plan[field] = value
    with pytest.raises(AdChildCreationError):
        validate_child_payload({"child_plan": plan})


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "https://127.0.0.1/x",
        "https://user:secret@example.com",
        "https://localhost/",
        "https://example.com:bad/",
        "file:///tmp/x",
    ],
)
def test_final_url_never_becomes_arbitrary_transport(url):
    plan = child_plan(kind="ad")
    plan["native"]["final_url"] = url
    with pytest.raises(AdChildCreationError):
        validate_child_payload({"child_plan": plan})


@pytest.mark.parametrize(
    "mismatch",
    [
        None,
        "campaign.advertising_channel_type",
        "campaign.bidding_strategy_type",
        "customer.currency_code",
        "ad_group.type",
        "ad_group.resource_name",
    ],
)
def test_google_preflight_requires_exact_search_manual_cpc_parent(mismatch):
    row = {
        "ad_group.resource_name": "customers/123/adGroups/456",
        "campaign.advertising_channel_type": "SEARCH",
        "campaign.bidding_strategy_type": "MANUAL_CPC",
        "customer.currency_code": "EUR",
        "ad_group.type": "SEARCH_STANDARD",
    }
    if mismatch:
        row[mismatch] = "unsupported"
    client = SimpleNamespace(search_stream=lambda *_: iter([row]))
    if mismatch:
        with pytest.raises(ValueError):
            google_prepare(client, "customers/123/adGroups/456", child_plan(kind="ad"))
    else:
        google_prepare(client, "customers/123/adGroups/456", child_plan(kind="ad"))


def _asset_group_child_plan() -> dict:
    return {
        "schema_version": 1,
        "platform": "google",
        "kind": "ad_set",
        "status": "PAUSED",
        "native": {
            "kind": "ASSET_GROUP",
            "final_url": "https://example.com/landing",
            "assets": {},
        },
    }


def test_padre_con_canal_distinto_al_firmado_deniega():
    """T033/T-5: el grupo de recursos firmado es de Maximo Rendimiento
    (`spec_for_child_type("ASSET_GROUP")` exige `PERFORMANCE_MAX`), pero
    la campana viva del padre es de Busqueda -- se deniega, sin comparar
    contra la constante `"SEARCH"`."""
    row = {
        "campaign.resource_name": "customers/123/campaigns/456",
        "campaign.advertising_channel_type": "SEARCH",
        "campaign.bidding_strategy_type": "MANUAL_CPC",
        "customer.currency_code": "EUR",
    }
    client = SimpleNamespace(search_stream=lambda *_: iter([row]))

    with pytest.raises(ValueError, match="ad_child_parent_unsupported"):
        google_prepare(client, "customers/123/campaigns/456", _asset_group_child_plan())


def test_padre_con_puja_distinta_a_la_firmada_deniega():
    """T033/T-5: la fila de Maximo Rendimiento solo admite pujas por
    conversiones -- una campana con `MANUAL_CPC` (aunque el canal fuera
    el correcto) no es un padre valido para el grupo de recursos."""
    row = {
        "campaign.resource_name": "customers/123/campaigns/456",
        "campaign.advertising_channel_type": "PERFORMANCE_MAX",
        "campaign.bidding_strategy_type": "MANUAL_CPC",
        "customer.currency_code": "EUR",
    }
    client = SimpleNamespace(search_stream=lambda *_: iter([row]))

    with pytest.raises(ValueError, match="ad_child_parent_unsupported"):
        google_prepare(client, "customers/123/campaigns/456", _asset_group_child_plan())


@pytest.mark.parametrize(
    "mismatch", [None, "account", "creative", "objective", "daily_budget", "special_ad_categories"]
)
def test_meta_preflight_checks_existing_creative_owner_and_campaign_budget(mismatch, monkeypatch):
    client = _client()

    def call(method, path, *, params):
        assert method == "GET"
        bodies = {
            "act_123": {"currency": "EUR"},
            "456": {
                "campaign_id": "333",
                "optimization_goal": "LINK_CLICKS",
                "destination_type": "WEBSITE",
                "account_id": "123",
            },
            "333": {
                "objective": "OUTCOME_TRAFFIC",
                "buying_type": "AUCTION",
                "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
                "daily_budget": "2000",
                "lifetime_budget": "0",
                "special_ad_categories": [],
                "account_id": "123",
            },
            "987": {"id": "987", "account_id": "123"},
        }
        if mismatch == "account":
            bodies["987"]["account_id"] = "999"
        elif mismatch == "creative":
            bodies["987"]["id"] = "999"
        elif mismatch:
            bodies["333"][mismatch] = ["HOUSING"] if mismatch == "special_ad_categories" else "0"
        return SimpleNamespace(json=lambda: bodies[path[0]])

    monkeypatch.setattr(client, "_api_for", lambda _: SimpleNamespace(call=call))
    if mismatch:
        with pytest.raises((ValueError, CredentialNotConnectedError)):
            meta_prepare(client, "act_123/456", child_plan("meta", "ad"))
    else:
        meta_prepare(client, "act_123/456", child_plan("meta", "ad"))


@pytest.mark.parametrize("kind", ["ad_set", "ad"])
@pytest.mark.parametrize(
    "failure", [None, "partial", "active", "different_parent", "different_content"]
)
def test_google_v25_native_paused_child_exact_confirmation(kind, failure, monkeypatch):
    sdk = GoogleAdsClient(credentials=AnonymousCredentials(), version="v25", use_proto_plus=True)
    plan = child_plan(kind=kind)
    parent = "customers/123/campaigns/456" if kind == "ad_set" else "customers/123/adGroups/456"
    calls = []

    def mutate(**kwargs):
        calls.append(kwargs)
        assert kwargs["retry"] is None and kwargs["partial_failure"] is False
        assert kwargs["response_content_type"] == "MUTABLE_RESOURCE"
        (operation,) = kwargs["mutate_operations"]
        response = sdk.get_type("MutateGoogleAdsResponse")
        if failure == "partial":
            response.partial_failure_error.code = 13
            return response
        result = sdk.get_type("MutateOperationResponse")
        if kind == "ad_set":
            child = operation.ad_group_operation.create
            assert child.cpc_bid_micros == 1_250_000
            result.ad_group_result.resource_name = "customers/123/adGroups/789"
            result.ad_group_result.ad_group = child
            actual = result.ad_group_result.ad_group
            if failure == "different_parent":
                actual.campaign = "customers/123/campaigns/999"
            if failure == "different_content":
                actual.cpc_bid_micros = 9_000_000
        else:
            child = operation.ad_group_ad_operation.create
            result.ad_group_ad_result.resource_name = "customers/123/adGroupAds/456~789"
            result.ad_group_ad_result.ad_group_ad = child
            actual = result.ad_group_ad_result.ad_group_ad
            if failure == "different_parent":
                actual.ad_group = "customers/123/adGroups/999"
            if failure == "different_content":
                actual.ad.final_urls[:] = ["https://wrong.example"]
        assert child.status.name == "PAUSED"
        if failure == "active":
            actual.status = "ENABLED"
        response.mutate_operation_responses.append(result)
        return response

    monkeypatch.setattr(sdk, "get_service", lambda _: SimpleNamespace(mutate=mutate))
    if failure:
        with pytest.raises(ValueError):
            google_create(sdk, parent, plan)
    else:
        assert google_create(sdk, parent, plan)["status"] == "PAUSED"
    assert len(calls) == 1


@pytest.mark.parametrize("kind", ["ad_set", "ad"])
@pytest.mark.parametrize(
    "failure", [None, "timeout", "active", "wrong_account", "different_parent", "different_content"]
)
def test_meta_v26_one_post_exact_owner_paused_and_content(kind, failure):
    client = _client()
    plan = child_plan("meta", kind)
    posted = []

    def call(method, path, *, params):
        if method == "POST":
            posted.append(deepcopy(params))
            assert path == ["act_123", "adsets" if kind == "ad_set" else "ads"]
            assert "daily_budget" not in params and params["status"] == "PAUSED"
            if failure == "timeout":
                raise TimeoutError
            return SimpleNamespace(json=lambda: {"id": "678"})
        actual = {**deepcopy(posted[0]), "account_id": "123"}
        if kind == "ad":
            actual["creative"] = {"id": "987"}
        if failure == "active":
            actual["status"] = "ACTIVE"
        if failure == "wrong_account":
            actual["account_id"] = "999"
        if failure == "different_parent":
            actual["campaign_id" if kind == "ad_set" else "adset_id"] = "999"
        if failure == "different_content":
            if kind == "ad":
                actual["creative"] = {"id": "999"}
            else:
                actual["targeting"]["targeting_automation"]["advantage_audience"] = 1
        return SimpleNamespace(json=lambda: actual)

    if failure:
        with pytest.raises((ValueError, TimeoutError, CredentialNotConnectedError)):
            meta_create(client, SimpleNamespace(call=call), "act_123/456", plan)
    else:
        assert (
            meta_create(client, SimpleNamespace(call=call), "act_123/456", plan)["child_resource"]
            == "act_123/678"
        )
    assert len(posted) == 1


@pytest.mark.parametrize(
    "platform,account,parent",
    [
        ("google", "1234567890", "customers/1234567890/campaigns/456"),
        ("meta", "act_1234567890", "act_1234567890/456"),
    ],
)
@pytest.mark.parametrize("unknown", [False, True])
@pytest.mark.parametrize("expire_during", [None, "preflight", "lock"])
async def test_child_creation_real_receipt_no_remutation_and_signed_semantics(  # noqa: PLR0917 - platform/clock matrix
    tmp_path, monkeypatch, platform, account, parent, unknown, expire_during
):
    signer, verifier = _signer_and_verifier()
    clock = FixedClock(_NOW)
    ledger = WriteLedgerStore(tmp_path / "children.sqlite")
    pipeline = WriteAuthorizationPipeline(
        verifier,
        parse_caps_config(_caps_yaml(account)),
        ledger,
        scope_resolver=fake_scope,
        clock=clock,
    )
    ref = EntityRef.parse(f"{platform}:campaign:{uuid4()}:{uuid4()}:{parent}")
    payload = {"child_plan": child_plan(platform)}
    parameter = "new_ad_set:exact"
    intent = WriteIntent(
        ref,
        WriteOperation.CREATE_AD_SET,
        parameter,
        None,
        payload,
        compute_diff_hash(ref, parameter, None, payload),
        "state",
        str(ref.business_id),
    )
    auth = _authorization(signer, diff_hash=intent.diff_hash)
    if expire_during == "lock":
        original_begin = ledger.begin_transaction

        def delayed_lock():
            original_begin()
            clock.advance_to(auth.expires_at + timedelta(seconds=1))

        monkeypatch.setattr(ledger, "begin_transaction", delayed_lock)
    assert matches_signed_transition(intent)
    # `ATTACH_CREATIVE` se elimino (004 tasks-2.md H-1: dead code, ningun
    # llamador lo produce); cualquier operacion ajena a la creacion de hijos
    # sigue sin encajar el `parametro` de esta prueba.
    assert not matches_signed_transition(replace(intent, operation=WriteOperation.DELETE))
    assert not matches_signed_transition(replace(intent, expected_state_hash=""))
    calls = []

    def create(*args):
        calls.append(args)
        if unknown:
            raise TimeoutError
        return {"child_resource": "confirmed", "status": "PAUSED"}

    def prepare(*_):
        if expire_during == "preflight":
            clock.advance_to(auth.expires_at + timedelta(seconds=1))

    kwargs = dict(
        pipeline=pipeline,
        intent=intent,
        authorization=auth,
        key="child-once",
        account=account,
        now=_NOW,
        prepare=prepare,
        create=create,
        consume_rate=lambda _operations: True,
    )
    first = await create_paused_child(**kwargs)
    if expire_during:
        assert first.outcome == "DENIED"
        assert first.error_code == "authorization_expired"
        assert not calls
        assert pipeline.read_receipt("child-once", intent, auth) is None
        return
    assert first.outcome == ("UNKNOWN" if unknown else "SUCCEEDED")
    assert await create_paused_child(**kwargs) == first
    assert pipeline.read_receipt("child-once", intent, auth) == first


# ---------------------------------------------------------------------------
# T035 finding 2 (threat-model.md D-2/AL-5): real operation count passed to
# `consume_rate`, never the implicit `1`.
# ---------------------------------------------------------------------------


def test_asset_group_operation_count_covers_every_new_text_asset_and_its_link():
    assets = {
        "headlines": ["H1", "H2", "H3"],
        "long_headlines": ["LH1"],
        "descriptions": ["D1", "D2"],
        "business_name": "ClinicaX",
        "name": "Grupo de recursos 1",
        "logo": "customers/123/assets/1",
        "marketing_image": "customers/123/assets/2",
        "square_image": "customers/123/assets/3",
    }
    # (3 headlines + 1 long_headline + 2 descriptions + 1 business_name) *
    # (create + link) = 14, + 3 image links (already-uploaded assets), + 1
    # for the asset group itself.
    assert _google_asset_group_operation_count(assets) == 18


def test_operation_count_is_one_for_a_google_search_ad_group():
    assert _operation_count(child_plan("google", "ad_set")) == 1


def test_operation_count_is_one_for_a_meta_child():
    assert _operation_count(child_plan("meta", "ad_set")) == 1


def _full_asset_group_child_plan() -> dict:
    plan = _asset_group_child_plan()
    plan["native"]["assets"] = {
        "headlines": ["Reserva hoy"],
        "long_headlines": ["Reserva tu cita en clinicax.es"],
        "descriptions": ["Atencion cercana", "Sin listas de espera"],
        "business_name": "ClinicaX",
        "name": "Grupo de recursos 1",
        "logo": "customers/1234567890/assets/1",
        "marketing_image": "customers/1234567890/assets/2",
        "square_image": "customers/1234567890/assets/3",
    }
    return plan


async def test_create_paused_child_consumes_the_real_asset_group_operation_count(
    tmp_path,
):
    signer, verifier = _signer_and_verifier()
    clock = FixedClock(_NOW)
    ledger = WriteLedgerStore(tmp_path / "asset_group.sqlite")
    account = "1234567890"
    pipeline = WriteAuthorizationPipeline(
        verifier,
        parse_caps_config(_caps_yaml(account)),
        ledger,
        scope_resolver=fake_scope,
        clock=clock,
    )
    ref = EntityRef.parse(
        f"google:campaign:{uuid4()}:{uuid4()}:customers/{account}/campaigns/456"
    )
    payload = {"child_plan": _full_asset_group_child_plan()}
    parameter = "new_ad_set:exact"
    intent = WriteIntent(
        ref,
        WriteOperation.CREATE_AD_SET,
        parameter,
        None,
        payload,
        compute_diff_hash(ref, parameter, None, payload),
        "state",
        str(ref.business_id),
    )
    auth = _authorization(signer, diff_hash=intent.diff_hash)
    consumed: list[int] = []

    result = await create_paused_child(
        pipeline=pipeline,
        intent=intent,
        authorization=auth,
        key="asset-group-once",
        account=account,
        now=_NOW,
        prepare=lambda *_: None,
        create=lambda *_: {"child_resource": "confirmed", "status": "PAUSED"},
        consume_rate=lambda operations: consumed.append(operations) or True,
    )

    # (1 headline + 1 long_headline + 2 descriptions + 1 business_name) *
    # (create + link) = 10, + 3 image links, + 1 for the asset group itself.
    assert result.outcome == "SUCCEEDED"
    assert consumed == [14]
