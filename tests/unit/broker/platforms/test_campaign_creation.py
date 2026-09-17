"""Native creation uses real signing/caps/SQLite with a fake provider only."""

from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from safent_ads.accounts.application.ports import IdempotencyKey, WriteIntent, WriteOperation
from safent_ads.broker.domain.ledger_scope import LedgerScopeError
from safent_ads.broker.infrastructure.caps_config import parse_caps_config
from safent_ads.broker.infrastructure.write_ledger_store import WriteLedgerStore
from safent_ads.broker.platforms.campaign_creation import create_paused_campaign
from safent_ads.broker.platforms.google_conversion_goal_reader import (
    ConversionGoalVerificationError,
)
from safent_ads.broker.platforms.write_pipeline import WriteAuthorizationPipeline
from safent_ads.proposals.domain.diff_hash import compute_diff_hash
from safent_ads.shared.ids import EntityRef
from tests.unit.broker.ledger_scope_fakes import fake_scope
from tests.unit.broker.platforms.test_write_pipeline import (
    _NOW,
    _authorization,
    _intent,
    _pipeline,
    _signer_and_verifier,
)
from tests.unit.execution.test_campaign_creation_budget import creation_payload


@pytest.mark.parametrize("platform,account", [("google", "123"), ("meta", "act_123")])
@pytest.mark.parametrize("uncertain", [False, True])
async def test_one_native_create_reserved_once_and_replayed_readonly(
    tmp_path: Path, platform: str, account: str, uncertain: bool
):
    signer, verifier = _signer_and_verifier()
    caps = parse_caps_config(f"""defaults:
  max_step_pct: 10
  max_changes_per_day: 10
  autonomy_enabled: false
accounts:
  '{account}':
    daily_cap_minor: 3000
    monthly_cap_minor: 90000
    floor_minor: 100
    ceiling_minor: 3000
""")
    ledger = WriteLedgerStore(tmp_path / "create.sqlite")
    pipeline = WriteAuthorizationPipeline(verifier, caps, ledger, scope_resolver=fake_scope)
    payload = creation_payload(platform)
    ref = EntityRef.parse(f"{platform}:account:{uuid4()}:{uuid4()}:{account}")
    intent = WriteIntent(
        ref,
        WriteOperation.CREATE_CAMPAIGN,
        "new_campaign:explicit",
        None,
        payload,
        compute_diff_hash(ref, "new_campaign:explicit", None, payload),
        "",
        str(ref.business_id),
    )
    authorization = _authorization(signer, diff_hash=intent.diff_hash)
    calls = []

    def native(account_id, plan):
        calls.append((account_id, plan))
        if uncertain:
            raise TimeoutError("remote may have created budget/campaign")
        return {
            "campaign_resource": f"{account_id}/456",
            "status": "PAUSED",
            "daily_budget_minor": 2000,
        }

    kwargs = dict(
        pipeline=pipeline,
        intent=intent,
        authorization=authorization,
        idempotency_key=IdempotencyKey("creation-1"),
        now=_NOW,
        currency=lambda _: "EUR",
        create=native,
        consume_rate=lambda _operations: True,
    )
    first = await create_paused_campaign(**kwargs)
    assert first.outcome == ("UNKNOWN" if uncertain else "SUCCEEDED")
    # Another connection to that physical account must not clear its first reservation.
    other_ref = replace(ref, connection_id=uuid4())
    second = replace(
        intent,
        entity_ref=other_ref,
        diff_hash=compute_diff_hash(other_ref, intent.parametro, None, payload),
    )
    blocked = await create_paused_campaign(
        **{
            **kwargs,
            "intent": second,
            "authorization": _authorization(signer, diff_hash=second.diff_hash),
            "idempotency_key": IdempotencyKey("creation-2"),
        }
    )
    assert blocked.error_code == "daily_cap_exceeded"
    read = pipeline.read_receipt("creation-1", intent, authorization)
    assert read == first
    assert len(calls) == 1
    assert ledger.month_to_date_delta(fake_scope(intent, account), _NOW.date()) == (
        0 if uncertain else 2000
    )
    assert ledger.pending_totals(fake_scope(intent, account))[1] == (2000 if uncertain else 0)


async def test_missing_plan_never_calls_provider(tmp_path: Path):
    signer, verifier = _signer_and_verifier()
    intent = _intent(
        before=None,
        after={"daily_budget_amount": "20000"},
        expected_state_hash="",
        parametro="new_campaign:brief",
        operation=WriteOperation.CREATE_CAMPAIGN,
    )

    def forbidden(*args):
        raise AssertionError("must reject before provider call")

    result = await create_paused_campaign(
        pipeline=_pipeline(tmp_path, verifier=verifier),
        intent=intent,
        authorization=_authorization(signer, diff_hash=intent.diff_hash),
        idempotency_key=IdempotencyKey("brief"),
        now=_NOW,
        currency=forbidden,
        create=forbidden,
        consume_rate=lambda _operations: True,
    )
    assert result.error_code == "campaign_creation_plan_required"


async def test_revocation_after_currency_read_rechecked_before_any_creation(tmp_path: Path):
    signer, verifier = _signer_and_verifier()
    pipeline = _pipeline(tmp_path, verifier=verifier)
    active = True

    def scope(intent, account):
        if not active:
            raise LedgerScopeError("revoked")
        return fake_scope(intent, account)

    pipeline._scope_resolver = scope
    payload = creation_payload()
    ref = EntityRef.parse(f"google:account:{uuid4()}:{uuid4()}:1234567890")
    intent = WriteIntent(
        ref,
        WriteOperation.CREATE_CAMPAIGN,
        "new_campaign:revoked",
        None,
        payload,
        compute_diff_hash(ref, "new_campaign:revoked", None, payload),
        "",
        str(ref.business_id),
    )

    def currency(_):
        nonlocal active
        active = False
        return "EUR"

    def forbidden(*args):
        raise AssertionError("revoked connection must not mutate or fall back")

    result = await create_paused_campaign(
        pipeline=pipeline,
        intent=intent,
        authorization=_authorization(signer, diff_hash=intent.diff_hash),
        idempotency_key=IdempotencyKey("revoked"),
        now=_NOW,
        currency=currency,
        create=forbidden,
        consume_rate=lambda _operations: True,
    )
    assert result.error_code == "ledger_scope_unverified"


def _pmax_payload(resource_names: list[str]) -> dict:
    return {
        "creation_plan": {
            "schema_version": 1,
            "platform": "google",
            "name": "Maximo rendimiento",
            "status": "PAUSED",
            "daily_budget": {"amount": "20.00", "currency": "EUR"},
            "native": {
                "advertising_channel_type": "PERFORMANCE_MAX",
                "bidding_strategy": {"kind": "MAXIMIZE_CONVERSIONS"},
                "contains_eu_political_advertising": (
                    "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING"
                ),
                "conversion_goals": [{"resource_name": name} for name in resource_names],
                "url_expansion_opt_out": True,
                "text_asset_automation_enabled": False,
            },
        }
    }


async def test_meta_de_conversion_de_otra_cuenta_deniega_sin_escribir(tmp_path: Path):
    """threat-model.md S-1/T032: sin las tres condiciones (misma cuenta,
    ENABLED, 1..10 sin duplicados) el bróker deniega ANTES de reservar la
    clave de idempotencia y sin llamar a `create` ni a `consume_rate`."""
    signer, verifier = _signer_and_verifier()
    pipeline = _pipeline(tmp_path, verifier=verifier)
    payload = _pmax_payload(["customers/9998887777/conversionActions/456"])
    ref = EntityRef.parse(f"google:account:{uuid4()}:{uuid4()}:1234567890")
    intent = WriteIntent(
        ref,
        WriteOperation.CREATE_CAMPAIGN,
        "new_campaign:pmax",
        None,
        payload,
        compute_diff_hash(ref, "new_campaign:pmax", None, payload),
        "",
        str(ref.business_id),
    )

    def forbidden(*args):
        raise AssertionError("must deny before consuming rate or calling the SDK")

    def deny(account, resource_names):
        raise ConversionGoalVerificationError

    result = await create_paused_campaign(
        pipeline=pipeline,
        intent=intent,
        authorization=_authorization(signer, diff_hash=intent.diff_hash),
        idempotency_key=IdempotencyKey("pmax-denied"),
        now=_NOW,
        currency=lambda _: "EUR",
        create=forbidden,
        consume_rate=forbidden,
        verify_conversion_goals=deny,
    )

    assert result.outcome == "DENIED"
    assert result.error_code == "campaign_creation_conversion_goal_unverified"


async def test_literal_forzado_manipulado_deniega_sin_tocar_el_proveedor(tmp_path: Path):
    """BL-1 (threat-model.md T-1/T-2): `creation_budget` -- primera accion
    de `create_paused_campaign`, antes de resolver la cuenta, la moneda o
    consumir la tasa -- relee el nativo firmado contra
    `GoogleChannelSpec.forced_literals` en CADA intento de escritura, no
    solo al proponer. Un literal forzado manipulado deniega sin tocar el
    proveedor ni reservar la clave de idempotencia."""
    signer, verifier = _signer_and_verifier()
    pipeline = _pipeline(tmp_path, verifier=verifier)
    payload = _pmax_payload(["customers/1234567890/conversionActions/456"])
    payload["creation_plan"]["native"]["url_expansion_opt_out"] = False
    ref = EntityRef.parse(f"google:account:{uuid4()}:{uuid4()}:1234567890")
    intent = WriteIntent(
        ref,
        WriteOperation.CREATE_CAMPAIGN,
        "new_campaign:pmax",
        None,
        payload,
        compute_diff_hash(ref, "new_campaign:pmax", None, payload),
        "",
        str(ref.business_id),
    )

    def forbidden(*args):
        raise AssertionError("must deny before touching the provider")

    result = await create_paused_campaign(
        pipeline=pipeline,
        intent=intent,
        authorization=_authorization(signer, diff_hash=intent.diff_hash),
        idempotency_key=IdempotencyKey("pmax-literal-denied"),
        now=_NOW,
        currency=forbidden,
        create=forbidden,
        consume_rate=forbidden,
    )

    assert result.outcome == "DENIED"
    assert result.error_code == "campaign_creation_native_invalid"


async def test_meta_de_conversion_verificada_deja_pasar_la_creacion(tmp_path: Path):
    signer, verifier = _signer_and_verifier()
    pipeline = _pipeline(tmp_path, verifier=verifier)
    payload = _pmax_payload(["customers/1234567890/conversionActions/456"])
    ref = EntityRef.parse(f"google:account:{uuid4()}:{uuid4()}:1234567890")
    intent = WriteIntent(
        ref,
        WriteOperation.CREATE_CAMPAIGN,
        "new_campaign:pmax",
        None,
        payload,
        compute_diff_hash(ref, "new_campaign:pmax", None, payload),
        "",
        str(ref.business_id),
    )
    verified = []

    def accept(account, resource_names):
        verified.append((account, tuple(resource_names)))

    def native(account_id, plan):
        return {
            "campaign_resource": f"{account_id}/456",
            "status": "PAUSED",
            "daily_budget_minor": 2000,
        }

    result = await create_paused_campaign(
        pipeline=pipeline,
        intent=intent,
        authorization=_authorization(signer, diff_hash=intent.diff_hash),
        idempotency_key=IdempotencyKey("pmax-ok"),
        now=_NOW,
        currency=lambda _: "EUR",
        create=native,
        consume_rate=lambda _operations: True,
        verify_conversion_goals=accept,
    )

    assert result.outcome == "SUCCEEDED"
    assert verified == [("1234567890", ("customers/1234567890/conversionActions/456",))]


async def test_google_campaign_creation_consumes_budget_plus_campaign_plus_each_goal(
    tmp_path: Path,
):
    """T035 finding 2 (threat-model.md D-2/AL-5): `create_paused_campaign`
    batches `campaign_budget` + `campaign` in ONE mutate plus one
    `campaign_conversion_goal` operation per verified goal -- never the
    implicit `1` `try_consume()` defaults to."""
    signer, verifier = _signer_and_verifier()
    pipeline = _pipeline(tmp_path, verifier=verifier)
    payload = _pmax_payload(
        [
            "customers/1234567890/conversionActions/456",
            "customers/1234567890/conversionActions/789",
        ]
    )
    ref = EntityRef.parse(f"google:account:{uuid4()}:{uuid4()}:1234567890")
    intent = WriteIntent(
        ref,
        WriteOperation.CREATE_CAMPAIGN,
        "new_campaign:pmax",
        None,
        payload,
        compute_diff_hash(ref, "new_campaign:pmax", None, payload),
        "",
        str(ref.business_id),
    )

    def native(account_id, plan):
        return {
            "campaign_resource": f"{account_id}/456",
            "status": "PAUSED",
            "daily_budget_minor": 2000,
        }

    consumed: list[int] = []

    result = await create_paused_campaign(
        pipeline=pipeline,
        intent=intent,
        authorization=_authorization(signer, diff_hash=intent.diff_hash),
        idempotency_key=IdempotencyKey("pmax-ops-count"),
        now=_NOW,
        currency=lambda _: "EUR",
        create=native,
        consume_rate=lambda operations: consumed.append(operations) or True,
        verify_conversion_goals=lambda *_: None,
    )

    assert result.outcome == "SUCCEEDED"
    assert consumed == [4]  # budget + campaign + 2 conversion goals


async def test_meta_campaign_creation_consumes_a_single_operation(tmp_path: Path):
    signer, verifier = _signer_and_verifier()
    caps = parse_caps_config("""defaults:
  max_step_pct: 10
  max_changes_per_day: 10
  autonomy_enabled: false
accounts:
  'act_123':
    daily_cap_minor: 3000
    monthly_cap_minor: 90000
    floor_minor: 100
    ceiling_minor: 3000
""")
    ledger = WriteLedgerStore(tmp_path / "meta-ops-count.sqlite")
    pipeline = WriteAuthorizationPipeline(verifier, caps, ledger, scope_resolver=fake_scope)
    payload = creation_payload("meta")
    ref = EntityRef.parse(f"meta:account:{uuid4()}:{uuid4()}:act_123")
    intent = WriteIntent(
        ref,
        WriteOperation.CREATE_CAMPAIGN,
        "new_campaign:explicit",
        None,
        payload,
        compute_diff_hash(ref, "new_campaign:explicit", None, payload),
        "",
        str(ref.business_id),
    )

    def native(account_id, plan):
        return {
            "campaign_resource": f"{account_id}/456",
            "status": "PAUSED",
            "daily_budget_minor": 2000,
        }

    consumed: list[int] = []

    result = await create_paused_campaign(
        pipeline=pipeline,
        intent=intent,
        authorization=_authorization(signer, diff_hash=intent.diff_hash),
        idempotency_key=IdempotencyKey("meta-ops-count"),
        now=_NOW,
        currency=lambda _: "EUR",
        create=native,
        consume_rate=lambda operations: consumed.append(operations) or True,
    )

    assert result.outcome == "SUCCEEDED"
    assert consumed == [1]
