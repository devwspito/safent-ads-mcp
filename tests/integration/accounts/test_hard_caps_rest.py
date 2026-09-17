"""`GET/PUT/DELETE /api/v1/accounts/{id}/hard-caps` de extremo a extremo --
spec 008 T032 y T034, invariantes i9, i10, i11, i13 y i14 de la revision
T027.

Sin dobles en el camino: Postgres real para la sesion, la confirmacion de
accion y el registro de decisiones; y un `ads-broker` REAL escuchando en un
socket unix de verdad, con su `config/caps.yaml`, su sobre y su directorio
de estado 0700. Si la politica del broker se pudiera esquivar desde la
superficie REST, se veria aqui -- que es justo lo que estos tests existen
para impedir."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import Mock

import httpx
import pyotp
import pytest
from sqlalchemy import text

from safent_ads.broker.infrastructure.caps_config import load_caps_snapshot
from safent_ads.broker.infrastructure.caps_state import (
    CapsStateStore,
    assert_state_directory_is_private,
)
from safent_ads.broker.infrastructure.effective_caps import EffectiveCapsResolver
from safent_ads.broker.infrastructure.hard_caps_service import HardCapsService, PanelCapsWriting
from safent_ads.broker.presentation.dispatcher import BrokerRuntime
from safent_ads.broker.presentation.socket_server import serve
from safent_ads.composition.app import create_app
from tests.integration.iam.test_login_lockout_persists import (
    _CORRECT_PASSWORD,
    _api_settings,
    _client_with_csrf,
)
from tests.integration.iam.test_login_lockout_persists import (
    seeded_owner as seeded_owner,  # noqa: PLC0414 - reexportacion de fixture de pytest
)

pytestmark = pytest.mark.integration

# La base de Postgres es compartida entre tests y `platform_accounts` tiene
# UNIQUE sobre `account_ref`: cada test estrena sus propias cuentas, con la
# forma canonica real de cada plataforma (10 digitos en Google, `act_` en
# Meta) para que la canonicalizacion del broker se ejerza de verdad.
_UNKNOWN_ACCOUNT = "act_000000000111"

# Mas de 64 caracteres: el esquema del socket del broker lo rechaza antes de
# mirar ninguna politica, y responde `DENIED`/`invalid_schema`.
_OVERLONG_ACCOUNT = "act_" + "9" * 90


def _fresh_accounts() -> tuple[str, str]:
    suffix = secrets.randbelow(10**9)
    return f"1{suffix:09d}", f"act_{secrets.randbelow(10**10):010d}"


def _caps_yaml(file_account: str) -> str:
    return f"""
defaults:
  max_step_pct: 30
  max_changes_per_day: 2
  autonomy_enabled: false

accounts:
  "{file_account}":
    daily_cap_minor: 4000
    monthly_cap_minor: 80000
    floor_minor: 200
    ceiling_minor: 15000
"""

_ENVELOPE = """
panel_managed:
  currency: EUR
  max_daily_cap_minor: 5000
  max_monthly_cap_minor: 100000
  max_ceiling_minor: 20000
  min_floor_minor: 500
  max_accounts: 2
  max_cap_changes_per_day: 20
"""


def _body(daily: int = 1000, monthly: int = 20000, ceiling: int = 9000, **extra: object) -> dict:
    return {
        "daily_cap_minor": daily,
        "monthly_cap_minor": monthly,
        "ceiling_minor": ceiling,
        "currency": "EUR",
    } | extra


def _path(account: str) -> str:
    return f"/api/v1/accounts/{account}/hard-caps"


@dataclass(frozen=True, slots=True)
class _Harness:
    app: object
    client: httpx.AsyncClient
    totp_secret: str
    business: uuid.UUID
    file_account: str
    panel_account: str


def _reauth_headers(totp_secret: str) -> dict[str, str]:
    return {"X-Reauth-Token": pyotp.TOTP(totp_secret).now()}


async def _confirmed(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    *,
    first_headers: dict[str, str] | None = None,
    **kwargs: object,
) -> httpx.Response:
    """El baile de dos peticiones: 428 con la prueba, y la misma peticion
    otra vez con `X-Action-Confirmation`.

    El `X-Reauth-Token` va SOLO en la primera: `require_reauth` quema el
    codigo TOTP por `(owner_id, action_hash, time_step)`, y la segunda
    peticion es la MISMA accion -- la evidencia que acaba de escribir el
    primer intento ya satisface la frescura, sin cabecera."""
    first = await client.request(method, path, headers=first_headers or {}, **kwargs)  # type: ignore[arg-type]
    if first.status_code != 428:
        return first
    token = first.json()["error"]["details"]["confirmation_token"]
    return await client.request(  # type: ignore[arg-type]
        method, path, headers={"X-Action-Confirmation": token}, **kwargs
    )


async def _start_broker(
    tmp_path: Path, file_account: str, *, with_envelope: bool, seed_state: object | None = None
) -> tuple[asyncio.Server, Path]:
    caps_file = tmp_path / "caps.yaml"
    caps_file.write_text(_caps_yaml(file_account) + (_ENVELOPE if with_envelope else ""))
    snapshot = load_caps_snapshot(caps_file)
    store = None
    writing = None
    if with_envelope:
        state_dir = tmp_path / "caps-state"
        assert_state_directory_is_private(state_dir)
        if seed_state is not None:
            # ANTES de construir el almacen: lo que el broker encuentre al
            # arrancar es lo que decide si el estado es legible.
            (state_dir / "panel-caps.json").write_text(json.dumps(seed_state))
        store = CapsStateStore(state_dir)
        assert snapshot.caps.panel_managed is not None
        writing = PanelCapsWriting(snapshot.caps.panel_managed, store)
    runtime = BrokerRuntime(
        adapters=Mock(),
        oauth_flow=Mock(),
        app_credentials=Mock(),
        hard_caps_status=snapshot.status,
        hard_caps=HardCapsService(
            EffectiveCapsResolver(snapshot.caps, store), snapshot.status, writing=writing
        ),
    )
    socket_path = tmp_path / "broker.sock"
    server = await serve(socket_path, runtime, frozenset({os.getuid()}))
    return server, socket_path


async def _release_owner_rows(app: object, owner_id: uuid.UUID) -> None:
    """`require_reauth` y `require_action_confirmation` dejan filas que
    apuntan a `owners`; sin retirarlas, el teardown de `seeded_owner` no
    puede borrar al dueno (mismo cuidado que en
    `tests/integration/mcp_oauth/test_grants_router.py`)."""
    async with app.state.container.session_factory() as session:  # type: ignore[attr-defined]
        await session.execute(
            text("DELETE FROM totp_reauth_confirmations WHERE owner_id = :owner"),
            {"owner": owner_id},
        )
        await session.execute(
            text("DELETE FROM owner_action_confirmations WHERE owner_id = :owner"),
            {"owner": owner_id},
        )
        await session.commit()


async def _seed_account(app: object, business: uuid.UUID, external_id: str) -> None:
    async with app.state.container.session_factory() as session:  # type: ignore[attr-defined]
        await session.execute(
            text(
                "INSERT INTO platform_accounts "
                "(business_id, platform, external_account_id, currency, timezone, "
                " api_tier, status) "
                "VALUES (:business, :platform, :external, 'EUR', 'Europe/Madrid', "
                " 'standard', 'ACTIVE')"
            ),
            {
                "business": business,
                "platform": "meta" if external_id.startswith("act_") else "google",
                "external": external_id,
            },
        )
        await session.commit()


async def _harness(
    database_url: str,
    tmp_path: Path,
    *,
    with_envelope: bool = True,
    seed_state: Callable[[str, str], object] | None = None,
):
    file_account, panel_account = _fresh_accounts()
    server, socket_path = await _start_broker(
        tmp_path,
        file_account,
        with_envelope=with_envelope,
        seed_state=None if seed_state is None else seed_state(file_account, panel_account),
    )
    settings = _api_settings(database_url).model_copy(
        update={"broker_socket_path": socket_path}
    )
    app = create_app(settings)
    business = uuid.uuid4()
    async with app.state.container.session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO businesses (id,slug,name,timezone,reference_currency) "
                "VALUES (:id,:slug,'Test','Europe/Madrid','EUR')"
            ),
            {"id": business, "slug": f"hardcaps-{business.hex}"},
        )
        await session.commit()
    for account in (file_account, panel_account):
        await _seed_account(app, business, account)
    return server, app, business, file_account, panel_account


@pytest.fixture
async def harness(database_url, seeded_owner, tmp_path) -> AsyncIterator[_Harness]:
    server, app, business, file_account, panel_account = await _harness(database_url, tmp_path)
    async with server, _client_with_csrf(app) as client:
        response = await client.post(
            "/api/v1/auth/login",
            json={"email": seeded_owner.email, "password": _CORRECT_PASSWORD},
        )
        assert response.status_code == 204, response.text
        yield _Harness(
            app, client, seeded_owner.totp_secret, business, file_account, panel_account
        )
    await _release_owner_rows(app, seeded_owner.owner_id)
    await app.state.container.aclose()


def _unreadable_state(file_account: str, panel_account: str) -> dict:
    """`schema_version` desconocida, dos cuentas dentro y presupuesto de
    cambios ya gastado: lo que dejaria un rollback de imagen o una edicion a
    mano del fichero del bróker."""
    entry = {
        "daily_cap_minor": 1000,
        "monthly_cap_minor": 20000,
        "ceiling_minor": 9000,
        "currency": "EUR",
        "updated_at": "2026-09-16T12:00:00+00:00",
        "updated_by": "owner-1",
    }
    return {
        "schema_version": 99,
        "accounts": {file_account: entry, panel_account: dict(entry)},
        "changes_by_day": {"2026-09-16": 4},
    }


@pytest.fixture
async def harness_with_an_unreadable_state(
    database_url, seeded_owner, tmp_path
) -> AsyncIterator[_Harness]:
    server, app, business, file_account, panel_account = await _harness(
        database_url, tmp_path, seed_state=_unreadable_state
    )
    async with server, _client_with_csrf(app) as client:
        response = await client.post(
            "/api/v1/auth/login",
            json={"email": seeded_owner.email, "password": _CORRECT_PASSWORD},
        )
        assert response.status_code == 204, response.text
        yield _Harness(
            app, client, seeded_owner.totp_secret, business, file_account, panel_account
        )
    await _release_owner_rows(app, seeded_owner.owner_id)
    await app.state.container.aclose()


@pytest.fixture
async def harness_without_envelope(
    database_url, seeded_owner, tmp_path
) -> AsyncIterator[_Harness]:
    server, app, business, file_account, panel_account = await _harness(
        database_url, tmp_path, with_envelope=False
    )
    async with server, _client_with_csrf(app) as client:
        response = await client.post(
            "/api/v1/auth/login",
            json={"email": seeded_owner.email, "password": _CORRECT_PASSWORD},
        )
        assert response.status_code == 204, response.text
        yield _Harness(
            app, client, seeded_owner.totp_secret, business, file_account, panel_account
        )
    await _release_owner_rows(app, seeded_owner.owner_id)
    await app.state.container.aclose()


# --- Sin sobre: todo PUT es 409 -----------------------------------------


async def test_without_an_envelope_every_put_is_409(harness_without_envelope: _Harness) -> None:
    response = await _confirmed(
        harness_without_envelope.client,
        "PUT",
        _path(harness_without_envelope.file_account),
        json=_body(),
        first_headers=_reauth_headers(harness_without_envelope.totp_secret),
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ENVELOPE_NOT_DECLARED"
    assert "config/caps.yaml" in response.json()["error"]["message"]


async def test_without_an_envelope_the_view_still_shows_the_file_cap(
    harness_without_envelope: _Harness,
) -> None:
    response = await harness_without_envelope.client.get(
        _path(harness_without_envelope.file_account)
    )

    assert response.status_code == 200
    assert response.json()["source"] == "file"
    assert response.json()["envelope"] is None
    assert response.json()["effective"]["daily_cap_minor"] == 4000


# --- Dentro del sobre acepta, fuera rechaza ------------------------------


async def test_a_cap_inside_the_envelope_is_applied(harness: _Harness) -> None:
    response = await _confirmed(
        harness.client,
        "PUT",
        _path(harness.panel_account),
        json=_body(),
        first_headers=_reauth_headers(harness.totp_secret),
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["source"] == "panel"
    assert payload["writable"] is True
    assert payload["currency"] == "EUR"
    assert payload["effective"]["daily_cap_minor"] == 1000
    assert payload["effective"]["floor_minor"] == 500
    assert payload["envelope"]["accounts_used"] == 1


async def test_a_cap_outside_the_envelope_is_409(harness: _Harness) -> None:
    response = await _confirmed(
        harness.client,
        "PUT",
        _path(harness.panel_account),
        json=_body(daily=9000, monthly=90000, ceiling=19000),
        first_headers=_reauth_headers(harness.totp_secret),
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ENVELOPE_EXCEEDED"


# --- Re-identificacion: solo al subir ------------------------------------


async def test_lowering_a_cap_needs_no_fresh_identification(harness: _Harness) -> None:
    await _confirmed(
        harness.client,
        "PUT",
        _path(harness.panel_account),
        json=_body(daily=3000, monthly=60000, ceiling=12000),
        first_headers=_reauth_headers(harness.totp_secret),
    )

    # Por debajo del anterior en los tres campos, y por encima del suelo que
    # declara `panel_managed.min_floor_minor`: un techo por debajo del suelo
    # dejaria la cuenta denegando todo y se rechaza aparte.
    lowered = await _confirmed(
        harness.client,
        "PUT",
        _path(harness.panel_account),
        json=_body(daily=600, monthly=1200, ceiling=800),
    )

    assert lowered.status_code == 200, lowered.text
    assert lowered.json()["effective"]["daily_cap_minor"] == 600


async def test_raising_a_cap_without_fresh_identification_is_401(harness: _Harness) -> None:
    await _confirmed(
        harness.client,
        "PUT",
        _path(harness.panel_account),
        json=_body(daily=100, monthly=200, ceiling=400),
        first_headers=_reauth_headers(harness.totp_secret),
    )

    raised = await harness.client.put(_path(harness.panel_account), json=_body(daily=2000))

    assert raised.status_code == 401
    assert raised.json()["error"]["code"] == "REAUTH_REQUIRED"
    assert raised.json()["error"]["details"]["methods"] == ["totp"]


async def test_the_first_cap_of_an_account_is_a_rise_and_needs_identification(
    harness: _Harness,
) -> None:
    """`source=none -> panel`: la cuenta pasa de denegar el 100 % de las
    escrituras a poder escribir. Es la mayor relajacion posible."""
    response = await harness.client.put(_path(harness.panel_account), json=_body())

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "REAUTH_REQUIRED"


async def test_a_totp_confirmation_for_one_rise_does_not_satisfy_a_bigger_one(
    harness: _Harness,
) -> None:
    """i13, mitad de la frescura: la evidencia TOTP vive por
    `(owner_id, action_hash)` y el `action_hash` incluye los tres importes.
    Una subida a X, ya completada, no deja fresca una subida a Y > X."""
    applied = await _confirmed(
        harness.client,
        "PUT",
        _path(harness.panel_account),
        json=_body(daily=1000),
        first_headers=_reauth_headers(harness.totp_secret),
    )
    assert applied.status_code == 200, applied.text

    bigger = await harness.client.put(_path(harness.panel_account), json=_body(daily=4000))

    assert bigger.status_code == 401
    assert bigger.json()["error"]["code"] == "REAUTH_REQUIRED"


async def test_a_confirmation_proof_for_one_amount_does_not_authorize_another(
    harness: _Harness,
) -> None:
    """i13, mitad de la confirmacion: la prueba de un solo uso esta ligada
    al cuerpo EXACTO y al `action_hash`. Se comprueba con dos BAJADAS, que
    no exigen frescura, para que lo unico que decida sea el enlace."""
    await _confirmed(
        harness.client,
        "PUT",
        _path(harness.panel_account),
        json=_body(daily=3000, monthly=60000, ceiling=12000),
        first_headers=_reauth_headers(harness.totp_secret),
    )
    first = await harness.client.put(_path(harness.panel_account), json=_body(daily=1000))
    assert first.status_code == 428
    proof_for_1000 = first.json()["error"]["details"]["confirmation_token"]

    reused = await harness.client.put(
        _path(harness.panel_account),
        json=_body(daily=900, monthly=18000, ceiling=8000),
        headers={"X-Action-Confirmation": proof_for_1000},
    )

    assert reused.status_code == 409
    assert reused.json()["error"]["code"] != "ENVELOPE_EXCEEDED"


# --- DELETE ---------------------------------------------------------------


async def test_delete_with_a_bigger_file_entry_needs_fresh_identification(
    harness: _Harness,
) -> None:
    await _confirmed(
        harness.client,
        "PUT",
        _path(harness.file_account),
        json=_body(daily=1000, monthly=20000, ceiling=9000),
        first_headers=_reauth_headers(harness.totp_secret),
    )

    withdrawn = await harness.client.delete(_path(harness.file_account))

    assert withdrawn.status_code == 401
    assert withdrawn.json()["error"]["code"] == "REAUTH_REQUIRED"


async def test_delete_without_a_file_entry_leaves_the_account_unwritable(
    harness: _Harness,
) -> None:
    await _confirmed(
        harness.client,
        "PUT",
        _path(harness.panel_account),
        json=_body(),
        first_headers=_reauth_headers(harness.totp_secret),
    )

    withdrawn = await _confirmed(harness.client, "DELETE", _path(harness.panel_account))

    assert withdrawn.status_code == 200, withdrawn.text
    assert withdrawn.json()["writable"] is False
    assert withdrawn.json()["source"] == "none"
    assert withdrawn.json()["effective"] is None


# --- i9/i10: sesion de panel y solo sesion de panel -----------------------


async def test_a_bearer_token_without_a_panel_cookie_is_401(harness: _Harness) -> None:
    transport = httpx.ASGITransport(app=harness.app)  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=transport, base_url="https://test") as bearer_client:
        response = await bearer_client.get(
            _path(harness.file_account), headers={"Authorization": "Bearer test-mcp-token-abc123"}
        )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


# --- CSRF, confirmacion y un solo uso ------------------------------------


async def test_a_put_without_the_csrf_header_is_403(harness: _Harness) -> None:
    response = await harness.client.put(
        _path(harness.panel_account), json=_body(), headers={"X-CSRF-Token": ""}
    )

    assert response.status_code == 403


async def test_a_put_without_the_confirmation_header_is_428(harness: _Harness) -> None:
    response = await harness.client.put(
        _path(harness.panel_account), json=_body(), headers=_reauth_headers(harness.totp_secret)
    )

    assert response.status_code == 428
    assert response.json()["error"]["code"] == "CONFIRMATION_REQUIRED"
    assert response.json()["error"]["details"]["confirmation_token"]


async def test_the_same_confirmation_token_is_refused_the_second_time(
    harness: _Harness,
) -> None:
    first = await harness.client.put(
        _path(harness.panel_account), json=_body(), headers=_reauth_headers(harness.totp_secret)
    )
    proof = first.json()["error"]["details"]["confirmation_token"]
    # Sin `X-Reauth-Token`: el codigo TOTP ya se quemo y la evidencia que
    # dejo satisface la frescura de ESTA misma accion.
    headers = {"X-Action-Confirmation": proof}

    applied = await harness.client.put(_path(harness.panel_account), json=_body(), headers=headers)
    replayed = await harness.client.put(_path(harness.panel_account), json=_body(), headers=headers)

    assert applied.status_code == 200, applied.text
    assert replayed.status_code == 409
    assert replayed.json()["error"]["code"] == "CONFIRMATION_USED"


# --- 404, nunca 403 -------------------------------------------------------


async def test_an_account_the_session_cannot_reach_is_404(harness: _Harness) -> None:
    response = await harness.client.get(_path(_UNKNOWN_ACCOUNT))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


async def test_a_put_on_an_unreachable_account_is_404_before_any_confirmation(
    harness: _Harness,
) -> None:
    response = await harness.client.put(
        _path(_UNKNOWN_ACCOUNT), json=_body(), headers=_reauth_headers(harness.totp_secret)
    )

    assert response.status_code == 404


# --- i14 y i8 en la superficie REST --------------------------------------


@pytest.mark.parametrize(
    "extra", ["floor_minor", "max_step_pct", "max_changes_per_day", "autonomy_enabled"]
)
async def test_i14_a_field_the_panel_never_sets_is_400_not_ignored(
    harness: _Harness, extra: str
) -> None:
    response = await harness.client.put(_path(harness.panel_account), json=_body(**{extra: 1}))

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_CAPS"
    assert extra in response.json()["error"]["message"]


@pytest.mark.parametrize("value", [-1, 1.5, True, "1000"])
async def test_a_malformed_amount_is_400(harness: _Harness, value: object) -> None:
    response = await harness.client.put(_path(harness.panel_account), json=_body(daily=value))

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_CAPS"


async def test_a_currency_other_than_the_envelopes_is_409_or_400(harness: _Harness) -> None:
    response = await _confirmed(
        harness.client,
        "PUT",
        _path(harness.panel_account),
        json=_body(currency="USD"),
        first_headers=_reauth_headers(harness.totp_secret),
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_CAPS"


# --- Auditoria del lado del panel ----------------------------------------


async def test_an_applied_change_is_written_to_the_decision_log(harness: _Harness) -> None:
    await _confirmed(
        harness.client,
        "PUT",
        _path(harness.panel_account),
        json=_body(),
        first_headers=_reauth_headers(harness.totp_secret),
    )

    async with harness.app.state.container.session_factory() as session:  # type: ignore[attr-defined]
        rows = (
            await session.execute(
                text(
                    "SELECT event_type, payload FROM decision_log "
                    " WHERE business_id = :business AND event_type = 'account_hard_caps_set'"
                ),
                {"business": harness.business},
            )
        ).all()

    assert len(rows) == 1
    assert rows[0][1]["platform_account_id"] == harness.panel_account


async def test_a_rejected_change_leaves_no_applied_decision(harness: _Harness) -> None:
    await _confirmed(
        harness.client,
        "PUT",
        _path(harness.panel_account),
        json=_body(daily=9000, monthly=90000, ceiling=19000),
        first_headers=_reauth_headers(harness.totp_secret),
    )

    async with harness.app.state.container.session_factory() as session:  # type: ignore[attr-defined]
        count = (
            await session.execute(
                text(
                    "SELECT count(*) FROM decision_log "
                    " WHERE business_id = :business AND event_type = 'account_hard_caps_set'"
                ),
                {"business": harness.business},
            )
        ).scalar_one()

    assert count == 0


# --- I-1: estado ilegible, ninguna escritura -----------------------------


async def test_i1_an_unreadable_state_refuses_every_write_with_503(
    harness_with_an_unreadable_state: _Harness, tmp_path: Path
) -> None:
    """El bróker no puede escribir partiendo de un estado que no supo leer:
    lo haria sobre los topes de las demas cuentas y sobre el presupuesto de
    cambios ya gastado. Fail-closed hasta la pantalla: 503 y nada tocado."""
    harness = harness_with_an_unreadable_state
    state_path = tmp_path / "caps-state" / "panel-caps.json"
    before = state_path.read_bytes()

    stored = await _confirmed(
        harness.client,
        "PUT",
        _path(harness.panel_account),
        json=_body(),
        first_headers=_reauth_headers(harness.totp_secret),
    )
    withdrawn = await _confirmed(
        harness.client,
        "DELETE",
        _path(harness.file_account),
        first_headers=_reauth_headers(harness.totp_secret),
    )

    assert stored.status_code == 503, stored.text
    assert stored.json()["error"]["code"] == "CAPS_STATE_UNWRITABLE"
    assert withdrawn.status_code == 503, withdrawn.text
    assert withdrawn.json()["error"]["code"] == "CAPS_STATE_UNWRITABLE"
    assert state_path.read_bytes() == before


async def test_i1_an_unreadable_state_still_shows_the_file_cap(
    harness_with_an_unreadable_state: _Harness,
) -> None:
    harness = harness_with_an_unreadable_state

    response = await harness.client.get(_path(harness.file_account))

    assert response.status_code == 200, response.text
    assert response.json()["source"] == "file"
    assert response.json()["panel_state_available"] is False
    assert response.json()["effective"]["daily_cap_minor"] == 4000


# --- Un id que el bróker rechaza por esquema es 404, no 503 --------------


async def test_an_id_the_broker_refuses_by_schema_is_404_not_503(harness: _Harness) -> None:
    """`DENIED`/`invalid_schema`: el bróker esta vivo y ese id no puede
    nombrar ninguna cuenta. Culparlo de estar caido escondería el motivo."""
    response = await harness.client.get(_path(_OVERLONG_ACCOUNT))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


# --- `from_panel`: lo guardado, junto a lo que esta en vigor -------------


async def test_a_clamped_field_reports_both_what_was_saved_and_what_applies(
    harness: _Harness,
) -> None:
    """El fichero recorta el tope del panel: la vista lleva los dos, para
    que la pantalla diga «guardado X, en vigor Y» en vez de dejar al dueno
    preguntandose cual de los dos esta viendo."""
    response = await _confirmed(
        harness.client,
        "PUT",
        _path(harness.file_account),
        json=_body(daily=5000, monthly=90000, ceiling=19000),
        first_headers=_reauth_headers(harness.totp_secret),
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["source"] == "file_and_panel"
    assert payload["clamped_by"] == [
        "daily_cap_minor",
        "monthly_cap_minor",
        "ceiling_minor",
    ]
    assert payload["from_panel"] == {
        "daily_cap_minor": 5000,
        "monthly_cap_minor": 90000,
        "ceiling_minor": 19000,
    }
    assert payload["from_file"] == {
        "daily_cap_minor": 4000,
        "monthly_cap_minor": 80000,
        "ceiling_minor": 15000,
    }
    assert payload["effective"]["daily_cap_minor"] == 4000


async def test_an_account_with_no_panel_entry_reports_a_null_from_panel(
    harness: _Harness,
) -> None:
    response = await harness.client.get(_path(harness.file_account))

    assert response.json()["from_panel"] is None
    assert response.json()["from_file"]["daily_cap_minor"] == 4000


# --- El nonce de la confirmacion viaja como `request_id` ----------------


async def test_the_confirmation_nonce_travels_to_the_broker_as_request_id(
    harness: _Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    """Es lo que enlaza la fila de `owner_action_confirmations` con la traza
    del bróker -- la que un compromiso de `ads-api` no puede borrar.

    Se lee del registro REAL ya renderizado, no de un doble: el bróker de
    este test escribe por el mismo camino que en produccion."""
    applied = await _confirmed(
        harness.client,
        "PUT",
        _path(harness.panel_account),
        json=_body(),
        first_headers=_reauth_headers(harness.totp_secret),
    )

    assert applied.status_code == 200, applied.text
    request_id = _audited_request_id(capsys.readouterr().out, "broker_account_caps_set")
    async with harness.app.state.container.session_factory() as session:  # type: ignore[attr-defined]
        rows = (
            await session.execute(
                text("SELECT nonce FROM owner_action_confirmations WHERE nonce = :nonce"),
                {"nonce": request_id},
            )
        ).all()

    assert len(rows) == 1


def _audited_request_id(rendered_logs: str, event: str) -> str:
    """El registro del bróker sale como JSON, una traza por linea."""
    for line in rendered_logs.splitlines():
        if f'"event": "{event}"' not in line:
            continue
        request_id = json.loads(line)["request_id"]
        assert isinstance(request_id, str)
        return request_id
    raise AssertionError(f"ninguna traza {event} en el registro del broker")
