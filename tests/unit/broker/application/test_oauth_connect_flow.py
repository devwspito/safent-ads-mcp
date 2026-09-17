"""`OAuthConnectFlow`: begin/complete de un solo uso, descubrimiento de
cuentas, y que `ads-api` nunca reciba un token (Google y Meta, mas la via
de pegar un System User token)."""

from __future__ import annotations

import base64
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from safent_ads.accounts.domain.platform_account import ApiTier
from safent_ads.accounts.domain.refs import CredentialRefId
from safent_ads.broker.application.errors import (
    CredentialNotFoundError,
    GoogleProjectAccessDeniedError,
    OAuthProviderDeniedError,
    OAuthSessionExpiredError,
    OAuthSessionNotFoundError,
)
from safent_ads.broker.application.oauth_connect_flow import OAuthConnectFlow
from safent_ads.broker.infrastructure.credential_store import EncryptedCredentialStore
from safent_ads.broker.platforms.google_oauth_adapter import (
    GoogleCustomerMetadata,
    GoogleOAuthAdapter,
    GoogleOAuthAdapterConfig,
    GoogleOAuthError,
    GoogleOAuthTokens,
)
from safent_ads.broker.platforms.meta_oauth_adapter import (
    MetaAdAccount,
    MetaOAuthAdapter,
    MetaOAuthAdapterConfig,
    MetaOAuthTokens,
)
from safent_ads.broker.platforms.oauth_http import GoogleCloudProjectAccessError, OAuthHttpError
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import PlatformCode

_NOW = datetime(2026, 9, 9, tzinfo=UTC)
_KEY_B64 = base64.b64encode(b"0" * 32).decode()
_CALLBACK = "https://x/callback"


async def test_cloud_project_access_error_survives_the_application_boundary(tmp_path: Path) -> None:
    flow = _flow(tmp_path, google=_StubGoogle(error=GoogleCloudProjectAccessError("test access")))
    begin = await flow.begin(provider=PlatformCode.GOOGLE, business_id="b1", redirect_uri=_CALLBACK)
    with pytest.raises(GoogleProjectAccessDeniedError):
        await flow.complete(state=begin.state, code="auth-code")


@pytest.mark.parametrize("provider", [PlatformCode.GOOGLE, PlatformCode.META])
async def test_transport_errors_are_classified_as_oauth_denials(
    tmp_path: Path, provider: PlatformCode
) -> None:
    flow = _flow(
        tmp_path,
        google=_StubGoogle(error=OAuthHttpError("network")),
        meta=_StubMeta(error=OAuthHttpError("network")),
    )
    begin = await flow.begin(provider=provider, business_id="b1", redirect_uri=_CALLBACK)
    with pytest.raises(OAuthProviderDeniedError):
        await flow.complete(state=begin.state, code="auth-code")


class _StubGoogle:
    def __init__(
        self, *, tokens: GoogleOAuthTokens | None = None, error: Exception | None = None
    ) -> None:
        self._tokens = tokens
        self._error = error
        self.exchange_calls: list[dict[str, str]] = []

    def authorization_url(self, *, state: str, code_challenge: str, redirect_uri: str) -> str:
        return f"https://accounts.google.com/auth?state={state}&challenge={code_challenge}&r={redirect_uri}"

    async def exchange_code(
        self, *, code: str, code_verifier: str, redirect_uri: str
    ) -> GoogleOAuthTokens:
        self.exchange_calls.append(
            {"code": code, "code_verifier": code_verifier, "redirect_uri": redirect_uri}
        )
        if self._error:
            raise self._error
        assert self._tokens is not None
        return self._tokens

    async def list_accessible_customers(self, access_token: str) -> list[str]:  # noqa: ARG002
        return ["1234567890"]

    async def fetch_customer_metadata(
        self,
        customer_id: str,
        access_token: str,  # noqa: ARG002
    ) -> GoogleCustomerMetadata:
        return GoogleCustomerMetadata(
            customer_id=customer_id,
            currency="EUR",
            timezone="Europe/Madrid",
            descriptive_name="Cliente",
        )


class _StubMeta:
    def __init__(
        self,
        *,
        long_lived: MetaOAuthTokens | None = None,
        accounts: list[MetaAdAccount] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._long_lived = long_lived
        self._accounts = accounts or []
        self._error = error

    def authorization_url(self, *, state: str, redirect_uri: str) -> str:
        return f"https://facebook.com/auth?state={state}&r={redirect_uri}"

    async def exchange_code_for_long_lived_token(
        self,
        *,
        code: str,  # noqa: ARG002
        redirect_uri: str,  # noqa: ARG002
    ) -> MetaOAuthTokens:
        if self._error:
            raise self._error
        assert self._long_lived is not None
        return self._long_lived

    async def list_ad_accounts(self, access_token: str) -> list[MetaAdAccount]:  # noqa: ARG002
        return self._accounts

    async def validate_system_user_token(self, token: str) -> list[MetaAdAccount]:  # noqa: ARG002
        if self._error:
            raise self._error
        return self._accounts


def _flow(
    tmp_path: Path, *, google: _StubGoogle | None = None, meta: _StubMeta | None = None
) -> OAuthConnectFlow:
    store = EncryptedCredentialStore(tmp_path / "credentials", _KEY_B64)
    return OAuthConnectFlow(
        store,
        google or _StubGoogle(),  # type: ignore[arg-type]
        meta or _StubMeta(),  # type: ignore[arg-type]
        FixedClock(_NOW),
    )


def _meta_account(account_id: str, name: str = "Cuenta") -> MetaAdAccount:
    return MetaAdAccount(
        account_id=account_id, name=name, currency="EUR", timezone_name="Europe/Madrid"
    )


def _google_tokens(**overrides: object) -> GoogleOAuthTokens:
    defaults: dict[str, object] = {
        "refresh_token": "refresh-secret",
        "access_token": "access-secret",
        "expires_at": _NOW,
        "scopes": ("adwords",),
    }
    defaults.update(overrides)
    return GoogleOAuthTokens(**defaults)  # type: ignore[arg-type]


async def test_begin_google_generates_pkce_and_returns_authorization_url(tmp_path: Path) -> None:
    flow = _flow(tmp_path)

    result = await flow.begin(
        provider=PlatformCode.GOOGLE, business_id=str(uuid.uuid4()), redirect_uri=_CALLBACK
    )

    assert result.state in result.authorization_url
    assert "challenge=" in result.authorization_url
    assert result.expires_at == _NOW + timedelta(minutes=10)


async def test_begin_meta_has_no_pkce_challenge(tmp_path: Path) -> None:
    flow = _flow(tmp_path)

    result = await flow.begin(
        provider=PlatformCode.META, business_id=str(uuid.uuid4()), redirect_uri=_CALLBACK
    )

    assert "challenge=" not in result.authorization_url


async def test_complete_unknown_state_raises(tmp_path: Path) -> None:
    flow = _flow(tmp_path)

    with pytest.raises(OAuthSessionNotFoundError):
        await flow.complete(state="never-began", code="irrelevant")


async def test_complete_is_single_use(tmp_path: Path) -> None:
    flow = _flow(tmp_path, google=_StubGoogle(tokens=_google_tokens()))
    begin = await flow.begin(provider=PlatformCode.GOOGLE, business_id="b1", redirect_uri=_CALLBACK)

    await flow.complete(state=begin.state, code="auth-code")

    with pytest.raises(OAuthSessionNotFoundError):
        await flow.complete(state=begin.state, code="auth-code")


async def test_complete_google_discovers_accounts_without_leaking_the_token(
    tmp_path: Path,
) -> None:
    flow = _flow(tmp_path, google=_StubGoogle(tokens=_google_tokens()))
    begin = await flow.begin(provider=PlatformCode.GOOGLE, business_id="b1", redirect_uri=_CALLBACK)

    result = await flow.complete(state=begin.state, code="auth-code")

    assert len(result.accounts) == 1
    account = result.accounts[0]
    assert account.external_account_id == "1234567890"
    assert account.currency == "EUR"
    assert account.api_tier == ApiTier.GOOGLE_EXPLORER
    assert account.platform == PlatformCode.GOOGLE
    assert not hasattr(account, "token")
    persisted = EncryptedCredentialStore(tmp_path / "credentials", _KEY_B64)
    assert (
        persisted.account_credential_ref(
            PlatformCode.GOOGLE,
            account.external_account_id,
            business_id=account.business_id,
            connection_id=account.connection_id,
        )
        == account.credential_ref_id
    )


async def test_complete_google_passes_the_pkce_verifier_from_begin(tmp_path: Path) -> None:
    google = _StubGoogle(tokens=_google_tokens(refresh_token="r", access_token="a"))
    flow = _flow(tmp_path, google=google)
    begin = await flow.begin(provider=PlatformCode.GOOGLE, business_id="b1", redirect_uri=_CALLBACK)

    await flow.complete(state=begin.state, code="auth-code")

    assert google.exchange_calls[0]["code"] == "auth-code"
    assert google.exchange_calls[0]["redirect_uri"] == _CALLBACK
    assert google.exchange_calls[0]["code_verifier"]


@pytest.mark.parametrize("minutes", [10, 11])
async def test_complete_expired_session_raises(tmp_path: Path, minutes: int) -> None:
    store = EncryptedCredentialStore(tmp_path / "credentials", _KEY_B64)
    clock = FixedClock(_NOW)
    flow = OAuthConnectFlow(store, _StubGoogle(), _StubMeta(), clock)  # type: ignore[arg-type]
    begin = await flow.begin(provider=PlatformCode.META, business_id="b1", redirect_uri=_CALLBACK)

    clock.advance_to(_NOW + timedelta(minutes=minutes))

    with pytest.raises(OAuthSessionExpiredError):
        await flow.complete(state=begin.state, code="auth-code")


async def test_complete_meta_discovers_accounts(tmp_path: Path) -> None:
    accounts = [_meta_account("act_1")]
    long_lived = MetaOAuthTokens(access_token="long-lived", expires_at=_NOW + timedelta(days=60))
    flow = _flow(tmp_path, meta=_StubMeta(long_lived=long_lived, accounts=accounts))
    begin = await flow.begin(provider=PlatformCode.META, business_id="b1", redirect_uri=_CALLBACK)

    result = await flow.complete(state=begin.state, code="auth-code")

    assert len(result.accounts) == 1
    assert result.accounts[0].external_account_id == "act_1"
    assert result.accounts[0].api_tier == ApiTier.META_LIMITED
    assert result.accounts[0].platform == PlatformCode.META


async def test_provider_error_is_wrapped(tmp_path: Path) -> None:
    flow = _flow(tmp_path, google=_StubGoogle(error=GoogleOAuthError("boom")))
    begin = await flow.begin(provider=PlatformCode.GOOGLE, business_id="b1", redirect_uri=_CALLBACK)

    with pytest.raises(OAuthProviderDeniedError):
        await flow.complete(state=begin.state, code="auth-code")


async def test_register_meta_system_user_token_discovers_accounts(tmp_path: Path) -> None:
    accounts = [_meta_account("act_2", name="Cuenta 2")]
    flow = _flow(tmp_path, meta=_StubMeta(accounts=accounts))

    result = await flow.register_meta_system_user_token(token="pasted-system-user-token")

    assert len(result.accounts) == 1
    assert result.accounts[0].external_account_id == "act_2"


async def test_status_of_unknown_credential_raises(tmp_path: Path) -> None:
    flow = _flow(tmp_path)

    with pytest.raises(CredentialNotFoundError):
        await flow.status(CredentialRefId(uuid.uuid4()))


async def test_revoke_then_status_reports_revoked(tmp_path: Path) -> None:
    accounts = [_meta_account("act_3", name="Cuenta 3")]
    flow = _flow(tmp_path, meta=_StubMeta(accounts=accounts))
    result = await flow.register_meta_system_user_token(token="a-token")
    credential_ref_id = result.accounts[0].credential_ref_id

    await flow.revoke(credential_ref_id)
    status = await flow.status(credential_ref_id)

    assert status.status == "revoked"


def _google_config() -> GoogleOAuthAdapterConfig:
    return GoogleOAuthAdapterConfig(client_id="c", client_secret="s", login_customer_id=None)


def _meta_config() -> MetaOAuthAdapterConfig:
    return MetaOAuthAdapterConfig(app_id="a", app_secret="s")


class _UnusedHttpClient:
    """`GoogleOAuthAdapter`/`MetaOAuthAdapter` reales solo se usan aqui
    para probar la frontera de tipos del constructor de `OAuthConnectFlow`
    — ningun test llama de verdad al cliente HTTP."""

    async def post_form(
        self,
        url: str,  # noqa: ARG002
        *,
        data: dict[str, str],  # noqa: ARG002
    ) -> dict[str, object]:
        raise AssertionError

    async def get_json(
        self,
        url: str,  # noqa: ARG002
        *,
        headers: object = None,  # noqa: ARG002
        params: object = None,  # noqa: ARG002
    ) -> dict[str, object]:
        raise AssertionError

    async def post_json(
        self,
        url: str,  # noqa: ARG002
        *,
        json_body: object,  # noqa: ARG002
        headers: object = None,  # noqa: ARG002
    ) -> dict[str, object]:
        raise AssertionError


def test_real_adapters_satisfy_the_flow_constructor(tmp_path: Path) -> None:
    """No un stub: comprueba que `GoogleOAuthAdapter`/`MetaOAuthAdapter`
    reales encajan en el constructor de `OAuthConnectFlow` (frontera de
    tipos, no de comportamiento — el comportamiento ya lo cubren sus
    propios tests con HTTP mockado)."""
    clock = FixedClock(_NOW)
    store = EncryptedCredentialStore(tmp_path / "credentials", _KEY_B64)
    google = GoogleOAuthAdapter(_google_config(), _UnusedHttpClient(), clock)  # type: ignore[arg-type]
    meta = MetaOAuthAdapter(_meta_config(), _UnusedHttpClient(), clock)  # type: ignore[arg-type]

    OAuthConnectFlow(store, google, meta, clock)
