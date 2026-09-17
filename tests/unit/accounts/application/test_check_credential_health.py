"""`CheckCredentialHealth` (tasks.md T126, threat-model.md C-21): reglas de
transicion/deduplicacion con reloj fijo, y prueba estructural de que la
operacion de solo lectura del broker nunca puede llevar un token."""

from __future__ import annotations

import dataclasses
import uuid
from datetime import datetime, timedelta

import pytest

from safent_ads.accounts.application.check_credential_health import CheckCredentialHealth
from safent_ads.accounts.application.connect_ports import CredentialStatusResult
from safent_ads.accounts.application.errors import BrokerRequestDeniedError
from safent_ads.accounts.domain.platform_account import ApiTier, PlatformAccount
from safent_ads.accounts.domain.platform_credential import (
    CredentialHealth,
    CredentialStatus,
    PlatformCredential,
)
from safent_ads.accounts.domain.refs import AccountRef, CredentialRefId
from safent_ads.accounts.testing.in_memory_repositories import (
    InMemoryAccountRepository,
    InMemoryCredentialRepository,
)
from safent_ads.shared.ids import BusinessId, PlatformCode
from tests.unit.accounts.application.conftest import NOW, FakeOAuthBrokerPort

_ACCOUNT_REF = AccountRef(PlatformCode.GOOGLE, "1234567890")


def _account(business_id: BusinessId, credential_ref_id: CredentialRefId) -> PlatformAccount:
    return PlatformAccount(
        business_id=business_id,
        account_ref=_ACCOUNT_REF,
        currency="EUR",
        timezone="Europe/Madrid",
        api_tier=ApiTier.GOOGLE_EXPLORER,
        credential_ref_id=credential_ref_id,
    )


def _credential(
    business_id: BusinessId, credential_ref_id: CredentialRefId, *, obtained_at: datetime
) -> PlatformCredential:
    return PlatformCredential(
        credential_ref_id=credential_ref_id,
        business_id=business_id,
        platform=PlatformCode.GOOGLE,
        alias=str(credential_ref_id),
        scopes=frozenset({"adwords"}),
        obtained_at=obtained_at,
    )


def test_credential_status_result_structurally_cannot_carry_a_token() -> None:
    """`OAuthBrokerPort.credential_status` (connect_ports.py: "por
    construccion, ninguna forma... tiene un campo capaz de llevar un
    token") -- verificacion estructural, no de comportamiento: ningun
    campo del DTO de respuesta se llama ni contiene nada parecido a un
    secreto."""
    fields = {f.name for f in dataclasses.fields(CredentialStatusResult)}
    assert fields == {"status", "scopes", "expires_at", "last_validated_at"}
    assert not any("token" in name or "secret" in name for name in fields)


async def test_no_transition_when_health_does_not_change() -> None:
    business_id = BusinessId.new()
    credential_ref_id = CredentialRefId(uuid.uuid4())
    accounts = InMemoryAccountRepository([_account(business_id, credential_ref_id)])
    credentials = InMemoryCredentialRepository(
        [_credential(business_id, credential_ref_id, obtained_at=NOW)]
    )
    broker = FakeOAuthBrokerPort(
        status_result=CredentialStatusResult(
            status=CredentialStatus.CONNECTED,
            scopes=frozenset({"adwords"}),
            expires_at=None,
            last_validated_at=None,
        )
    )
    use_case = CheckCredentialHealth(accounts, credentials, broker)

    transitions = await use_case.execute(business_id, now=NOW)

    assert transitions == []
    stored = await credentials.get(credential_ref_id, business_id=business_id)
    assert stored is not None
    assert stored.checked_at == NOW


async def test_transition_to_expiring_soon_is_reported() -> None:
    business_id = BusinessId.new()
    credential_ref_id = CredentialRefId(uuid.uuid4())
    accounts = InMemoryAccountRepository([_account(business_id, credential_ref_id)])
    credentials = InMemoryCredentialRepository(
        [_credential(business_id, credential_ref_id, obtained_at=NOW)]
    )
    broker = FakeOAuthBrokerPort(
        status_result=CredentialStatusResult(
            status=CredentialStatus.CONNECTED,
            scopes=frozenset({"adwords"}),
            expires_at=NOW + timedelta(days=3),
            last_validated_at=None,
        )
    )
    use_case = CheckCredentialHealth(accounts, credentials, broker)

    transitions = await use_case.execute(business_id, now=NOW)

    assert len(transitions) == 1
    transition = transitions[0]
    assert transition.previous_health == CredentialHealth.OK
    assert transition.current_health == CredentialHealth.EXPIRING_SOON
    assert transition.error_code == "TOKEN_EXPIRING_SOON"
    assert transition.account_ref == _ACCOUNT_REF


async def test_transition_to_revoked_is_reported_with_its_error_code() -> None:
    business_id = BusinessId.new()
    credential_ref_id = CredentialRefId(uuid.uuid4())
    accounts = InMemoryAccountRepository([_account(business_id, credential_ref_id)])
    credentials = InMemoryCredentialRepository(
        [_credential(business_id, credential_ref_id, obtained_at=NOW)]
    )
    broker = FakeOAuthBrokerPort(
        status_result=CredentialStatusResult(
            status=CredentialStatus.REVOKED,
            scopes=frozenset({"adwords"}),
            expires_at=None,
            last_validated_at=None,
        )
    )
    use_case = CheckCredentialHealth(accounts, credentials, broker)

    transitions = await use_case.execute(business_id, now=NOW)

    assert len(transitions) == 1
    assert transitions[0].current_health == CredentialHealth.REVOKED
    assert transitions[0].error_code == "CREDENTIAL_REVOKED"


async def test_credential_not_found_in_the_broker_is_treated_as_revoked() -> None:
    business_id = BusinessId.new()
    credential_ref_id = CredentialRefId(uuid.uuid4())
    accounts = InMemoryAccountRepository([_account(business_id, credential_ref_id)])
    credentials = InMemoryCredentialRepository(
        [_credential(business_id, credential_ref_id, obtained_at=NOW)]
    )
    broker = FakeOAuthBrokerPort(deny=BrokerRequestDeniedError("CREDENTIAL_NOT_FOUND"))
    use_case = CheckCredentialHealth(accounts, credentials, broker)

    transitions = await use_case.execute(business_id, now=NOW)

    assert len(transitions) == 1
    assert transitions[0].current_health == CredentialHealth.REVOKED
    assert transitions[0].error_code == "CREDENTIAL_NOT_FOUND"


async def test_other_broker_denials_propagate() -> None:
    business_id = BusinessId.new()
    credential_ref_id = CredentialRefId(uuid.uuid4())
    accounts = InMemoryAccountRepository([_account(business_id, credential_ref_id)])
    credentials = InMemoryCredentialRepository(
        [_credential(business_id, credential_ref_id, obtained_at=NOW)]
    )
    broker = FakeOAuthBrokerPort(deny=BrokerRequestDeniedError("DENIED"))
    use_case = CheckCredentialHealth(accounts, credentials, broker)

    with pytest.raises(BrokerRequestDeniedError):
        await use_case.execute(business_id, now=NOW)


async def test_account_without_a_resolvable_credential_is_skipped() -> None:
    business_id = BusinessId.new()
    credential_ref_id = CredentialRefId(uuid.uuid4())
    accounts = InMemoryAccountRepository([_account(business_id, credential_ref_id)])
    use_case = CheckCredentialHealth(
        accounts, InMemoryCredentialRepository(), FakeOAuthBrokerPort()
    )

    transitions = await use_case.execute(business_id, now=NOW)

    assert transitions == []


async def test_deduplicates_across_consecutive_checks_at_the_same_health() -> None:
    """"Deduplicado por (cuenta, salud) hasta que cambia" -- una segunda
    vuelta con la misma salud reportada por el broker no produce una
    segunda transicion, con reloj fijo (sin avanzar el tiempo)."""
    business_id = BusinessId.new()
    credential_ref_id = CredentialRefId(uuid.uuid4())
    accounts = InMemoryAccountRepository([_account(business_id, credential_ref_id)])
    credentials = InMemoryCredentialRepository(
        [_credential(business_id, credential_ref_id, obtained_at=NOW)]
    )
    broker = FakeOAuthBrokerPort(
        status_result=CredentialStatusResult(
            status=CredentialStatus.REVOKED,
            scopes=frozenset({"adwords"}),
            expires_at=None,
            last_validated_at=None,
        )
    )
    use_case = CheckCredentialHealth(accounts, credentials, broker)

    first_pass = await use_case.execute(business_id, now=NOW)
    second_pass = await use_case.execute(business_id, now=NOW + timedelta(hours=6))

    assert len(first_pass) == 1
    assert second_pass == []
