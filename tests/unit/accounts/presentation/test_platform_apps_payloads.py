"""Validación de forma/longitud de `/platform-apps` (contracts/rest-api.md
§Conexiones, owner decision app-credentials-ui): esta es la ÚNICA capa que
valida de verdad -- el bróker solo comprueba campos no vacíos."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from safent_ads.accounts.application.platform_apps_ports import PlatformAppStatus
from safent_ads.accounts.presentation.platform_apps_payloads import (
    PlatformAppStatusResponse,
    SetGoogleAppCredentialsRequest,
    SetMetaAppCredentialsRequest,
)
from safent_ads.shared.ids import PlatformCode

_VALID_GOOGLE_BODY = {
    "client_id": "abc123.apps.googleusercontent.com",
    "client_secret": "client-secret",
    "login_customer_id": "1234567890",
}


def test_valid_google_credentials_parse() -> None:
    parsed = SetGoogleAppCredentialsRequest.model_validate(_VALID_GOOGLE_BODY)

    assert parsed.client_id == "abc123.apps.googleusercontent.com"
    assert parsed.login_customer_id == "1234567890"


def test_google_login_customer_id_is_optional() -> None:
    body = dict(_VALID_GOOGLE_BODY)
    del body["login_customer_id"]

    parsed = SetGoogleAppCredentialsRequest.model_validate(body)

    assert parsed.login_customer_id is None


def test_retired_google_developer_token_is_rejected_not_stored() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        SetGoogleAppCredentialsRequest.model_validate(
            {**_VALID_GOOGLE_BODY, "developer_token": "obsolete-test-value"}
        )
    assert "developer_token" not in SetGoogleAppCredentialsRequest.model_fields
    assert "developer_token_masked" not in PlatformAppStatusResponse.model_fields


@pytest.mark.parametrize(
    "client_id",
    ["not-a-client-id", "abc123.apps.googleusercontent.com.evil.com", "", "abc123"],
)
def test_google_client_id_must_match_the_expected_shape(client_id: str) -> None:
    body = {**_VALID_GOOGLE_BODY, "client_id": client_id}

    with pytest.raises(ValidationError):
        SetGoogleAppCredentialsRequest.model_validate(body)


def test_google_formatted_mcc_is_canonical_in_validated_payload() -> None:
    parsed = SetGoogleAppCredentialsRequest.model_validate(
        {**_VALID_GOOGLE_BODY, "login_customer_id": "123-456-7890"}
    )
    assert parsed.login_customer_id == "1234567890"
    assert parsed.model_dump()["login_customer_id"] == "1234567890"


@pytest.mark.parametrize(
    "login_customer_id",
    [
        "12-34-56-7890",
        "abcdefghij",
        "not-numeric",
        "123 456 7890",
        " 123-456-7890",
        "123-456-7890 ",
        "1234567890\n",
        "123-456-7890\n",
        "123\t4567890",
        "123\u00a04567890",
        "１２３４５６７８９０",
        "123–456–7890",
        "123--456-7890",
        "123-4567890",
        "123456-7890",
    ],
)
def test_google_login_customer_id_must_be_numeric(login_customer_id: str) -> None:
    body = {**_VALID_GOOGLE_BODY, "login_customer_id": login_customer_id}

    with pytest.raises(ValidationError):
        SetGoogleAppCredentialsRequest.model_validate(body)


@pytest.mark.parametrize("field", ["client_id", "client_secret"])
def test_google_required_fields_reject_empty_strings(field: str) -> None:
    body = {**_VALID_GOOGLE_BODY, field: ""}

    with pytest.raises(ValidationError):
        SetGoogleAppCredentialsRequest.model_validate(body)


def test_google_rejects_unknown_fields() -> None:
    body = {**_VALID_GOOGLE_BODY, "unexpected": "value"}

    with pytest.raises(ValidationError):
        SetGoogleAppCredentialsRequest.model_validate(body)


def test_valid_meta_credentials_parse() -> None:
    parsed = SetMetaAppCredentialsRequest.model_validate(
        {"app_id": "9876543210", "app_secret": "meta-secret"}
    )

    assert parsed.app_id == "9876543210"
    assert parsed.app_secret == "meta-secret"  # noqa: S105 - valor de prueba, no secreto real


@pytest.mark.parametrize("field", ["app_id", "app_secret"])
def test_meta_required_fields_reject_empty_strings(field: str) -> None:
    body = {"app_id": "9876543210", "app_secret": "meta-secret", field: ""}

    with pytest.raises(ValidationError):
        SetMetaAppCredentialsRequest.model_validate(body)


def test_status_response_never_carries_a_secret_field() -> None:
    status = PlatformAppStatus(
        platform=PlatformCode.GOOGLE,
        configured=True,
        client_id_masked="****.com",
        login_customer_id_masked="****7890",
        updated_at=datetime(2026, 9, 10, tzinfo=UTC),
    )

    response = PlatformAppStatusResponse.from_status(status, redirect_uri="https://x/callback")

    assert "client_secret" not in response.model_dump()
    assert "app_secret" not in response.model_dump()
    assert response.client_id_masked == "****.com"
