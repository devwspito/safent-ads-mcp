"""`composition/api.py` (tasks.md T015, threat-model.md C-23/C-26):
cabeceras de seguridad, CSRF de doble envio y limite de tasa en `/auth/*`."""

from __future__ import annotations

import json

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from safent_ads.composition import api as api_composition
from safent_ads.composition.app import create_app
from safent_ads.composition.settings import ApiSettings


def test_security_headers(api_settings: ApiSettings) -> None:
    app = create_app(api_settings)

    with TestClient(app, base_url="https://testserver") as client:
        response = client.get("/api/v1/health")

    csp = response.headers["content-security-policy"]
    assert "unsafe-inline" not in csp
    assert "unsafe-eval" not in csp
    assert "frame-ancestors 'none'" in csp
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-frame-options"] == "DENY"


def test_csrf_rejected_without_token(api_settings: ApiSettings) -> None:
    app = create_app(api_settings)

    with TestClient(app, base_url="https://testserver") as client:
        response = client.post(
            "/api/v1/auth/login", json={"email": "owner@safent.example", "password": "x"}
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CSRF_REJECTED"


def test_csrf_cookie_is_issued_on_first_request(api_settings: ApiSettings) -> None:
    app = create_app(api_settings)

    with TestClient(app, base_url="https://testserver") as client:
        client.get("/api/v1/health")

        assert client.cookies.get("ads_csrf") is not None


def test_csrf_accepted_when_cookie_matches_header(api_settings: ApiSettings) -> None:
    """El adaptador SQL no llega a resolver el login en este test (no hay
    Postgres real detras): basta con demostrar que el CSRF no es lo que
    bloquea la peticion. `raise_server_exceptions=False` porque el fallo de
    conexion, mas alla de CSRF, es exactamente lo que se espera aqui."""
    app = create_app(api_settings)

    with TestClient(
        app, base_url="https://testserver", raise_server_exceptions=False
    ) as client:
        client.get("/api/v1/health")
        token = client.cookies.get("ads_csrf")

        response = client.post(
            "/api/v1/auth/login",
            json={"email": "owner@safent.example", "password": "x"},
            headers={"X-CSRF-Token": token},
        )

    assert response.status_code != 403


def test_csrf_rejected_when_header_does_not_match_cookie(api_settings: ApiSettings) -> None:
    app = create_app(api_settings)

    with TestClient(app, base_url="https://testserver") as client:
        client.get("/api/v1/health")

        response = client.post(
            "/api/v1/auth/login",
            json={"email": "owner@safent.example", "password": "x"},
            headers={"X-CSRF-Token": "not-the-real-token"},
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CSRF_REJECTED"


# H3/AL-7 (threat-model.md #364, tasks.md T126): `/api/v1/packages/**`
# alcanza una escritura real de plataforma (`undo` revierte una campana ya
# publicada) -- doble envio no basta, `Origin`/`Sec-Fetch-Site` se
# comprueban tambien. El flag `ADS_CAMPAIGN_PACKAGES_ENABLED` no importa
# aqui: `CsrfMiddleware` corre antes de que el enrutador resuelva la ruta.
_PACKAGES_MUTATION_PATH = "/api/v1/packages/pkg-1/approve"


def _csrf_token(client: TestClient) -> str:
    client.get("/api/v1/health")
    token = client.cookies.get("ads_csrf")
    assert token is not None
    return token


def test_packages_mutation_without_origin_header_is_rejected(api_settings: ApiSettings) -> None:
    app = create_app(api_settings)

    with TestClient(app, base_url="https://testserver") as client:
        token = _csrf_token(client)
        response = client.post(_PACKAGES_MUTATION_PATH, headers={"X-CSRF-Token": token})

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CSRF_REJECTED"


def test_packages_mutation_with_cross_site_header_is_rejected(api_settings: ApiSettings) -> None:
    app = create_app(api_settings)

    with TestClient(app, base_url="https://testserver") as client:
        token = _csrf_token(client)
        response = client.post(
            _PACKAGES_MUTATION_PATH,
            headers={
                "X-CSRF-Token": token,
                "Origin": api_settings.public_base_url,
                "Sec-Fetch-Site": "cross-site",
            },
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CSRF_REJECTED"


def test_packages_mutation_with_valid_origin_passes_the_origin_guard(
    api_settings: ApiSettings,
) -> None:
    """La ruta no esta montada (flag apagado por defecto): un 404 real
    demuestra que la peticion paso el guardia de CSRF/Origin y llego al
    enrutador, en vez de quedarse en el 403 de `CsrfMiddleware`."""
    app = create_app(api_settings)

    with TestClient(app, base_url="https://testserver") as client:
        token = _csrf_token(client)
        response = client.post(
            _PACKAGES_MUTATION_PATH,
            headers={
                "X-CSRF-Token": token,
                "Origin": api_settings.public_base_url,
                "Sec-Fetch-Site": "same-origin",
            },
        )

    assert response.status_code == 404


def test_packages_mutation_with_extra_allowed_host_origin_passes_the_guard(
    api_settings: ApiSettings,
) -> None:
    """B-4 (revision de seguridad 0.2.22): el panel servido en el host
    provisional (`ADS_MCP_EXTRA_ALLOWED_HOSTS`) no se auto-rechaza. 404 (no
    403) demuestra que paso el guardia de CSRF/Origin, igual que el test
    homologo con `public_base_url`."""
    extra_host = "ads.example.test"
    settings = api_settings.model_copy(update={"mcp_extra_allowed_hosts": [extra_host]})
    app = create_app(settings)

    with TestClient(app, base_url="https://testserver") as client:
        token = _csrf_token(client)
        response = client.post(
            _PACKAGES_MUTATION_PATH,
            headers={
                "X-CSRF-Token": token,
                "Origin": f"https://{extra_host}",
                "Sec-Fetch-Site": "same-origin",
            },
        )

    assert response.status_code == 404


def test_packages_mutation_with_unknown_origin_is_rejected_even_with_extra_hosts(
    api_settings: ApiSettings,
) -> None:
    """Un origen fuera de la lista sigue en 403 aunque haya hosts extra
    configurados -- la ampliacion no relaja el default-deny."""
    settings = api_settings.model_copy(
        update={"mcp_extra_allowed_hosts": ["ads.example.test"]}
    )
    app = create_app(settings)

    with TestClient(app, base_url="https://testserver") as client:
        token = _csrf_token(client)
        response = client.post(
            _PACKAGES_MUTATION_PATH,
            headers={
                "X-CSRF-Token": token,
                "Origin": "https://evil.example",
                "Sec-Fetch-Site": "same-origin",
            },
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CSRF_REJECTED"


def test_packages_mutation_with_session_cookie_and_no_origin_pair_is_rejected(
    api_settings: ApiSettings,
) -> None:
    """AL-7 (revision de seguridad 0.2.22): `PackagesOriginRequiredMiddleware`
    corre ANTES que `CsrfMiddleware` -- un replay de `ads_session` sin
    Origin ni Sec-Fetch-Site cae en `CSRF_ORIGIN_REQUIRED`, sin necesitar
    siquiera acertar el token `ads_csrf` de doble envio."""
    app = create_app(api_settings)

    with TestClient(
        app, base_url="https://testserver", cookies={"ads_session": "stolen-token"}
    ) as client:
        response = client.post(_PACKAGES_MUTATION_PATH, json={})

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CSRF_ORIGIN_REQUIRED"


def test_packages_mutation_without_a_session_cookie_is_unaffected(
    api_settings: ApiSettings,
) -> None:
    """Sin cookie `ads_session` (bearer/MCP-like), el guardia nuevo no
    aplica -- cae en el CSRF de doble envio de siempre, con su propio
    codigo."""
    app = create_app(api_settings)

    with TestClient(app, base_url="https://testserver") as client:
        response = client.post(_PACKAGES_MUTATION_PATH, json={})

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CSRF_REJECTED"


def test_rate_limit_auth(api_settings: ApiSettings) -> None:
    app = create_app(api_settings)

    with TestClient(
        app, base_url="https://testserver", raise_server_exceptions=False
    ) as client:
        client.get("/api/v1/health")
        token = client.cookies.get("ads_csrf")
        headers = {"X-CSRF-Token": token}
        payload = {"email": "owner@safent.example", "password": "x"}

        statuses = [
            client.post("/api/v1/auth/login", json=payload, headers=headers).status_code
            for _ in range(30)
        ]

    assert 429 in statuses


def test_rate_limited_response_carries_security_headers(api_settings: ApiSettings) -> None:
    """L3: `RateLimitMiddleware` era, en tiempo de ejecucion, la envoltura
    MAS EXTERNA -- el orden real de `Starlette.add_middleware` es el
    inverso al de las llamadas -- asi que un 429 nunca pasaba por
    `SecurityHeadersMiddleware`. Ahora la envoltura real de fuera adentro
    es RequestId -> ForwardedPrefix -> SecurityHeaders -> Csrf ->
    RateLimit, asi que un corte en el limite de tasa sigue llevando las
    cabeceras de seguridad."""
    app = create_app(api_settings)

    response = None
    with TestClient(
        app, base_url="https://testserver", raise_server_exceptions=False
    ) as client:
        for _ in range(30):
            response = client.get(
                "/api/v1/rules/autonomy-gate",
                params={"business_id": "00000000-0000-0000-0000-000000000000"},
            )
            if response.status_code == 429:
                break

    assert response is not None
    assert response.status_code == 429
    assert response.headers["x-content-type-options"] == "nosniff"


def test_csrf_rejected_response_carries_security_headers(api_settings: ApiSettings) -> None:
    """Mismo bug de L3, visto desde el corte de `CsrfMiddleware` (403)."""
    app = create_app(api_settings)

    with TestClient(app, base_url="https://testserver") as client:
        response = client.post(
            "/api/v1/auth/login", json={"email": "owner@safent.example", "password": "x"}
        )

    assert response.status_code == 403
    assert response.headers["x-content-type-options"] == "nosniff"


def test_csrf_rejected_when_request_is_cross_site(api_settings: ApiSettings) -> None:
    """M4: `Sec-Fetch-Site` distinto de `same-origin`/`none` corta la
    peticion ANTES de mirar la cookie/cabecera de doble envio -- una
    pagina de otro origen que consiguiera leer `ads_csrf` (XSS, red
    compartida) no basta si el navegador declara que la peticion es
    cross-site."""
    app = create_app(api_settings)

    with TestClient(
        app, base_url="https://testserver", raise_server_exceptions=False
    ) as client:
        client.get("/api/v1/health")
        token = client.cookies.get("ads_csrf")

        response = client.post(
            "/api/v1/auth/login",
            json={"email": "owner@safent.example", "password": "x"},
            headers={"X-CSRF-Token": token, "Sec-Fetch-Site": "cross-site"},
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CSRF_REJECTED"


def test_csrf_rejected_when_origin_does_not_match_public_base_url(
    api_settings: ApiSettings,
) -> None:
    """M4: `Origin` presente y distinto de `ADS_PUBLIC_BASE_URL` corta la
    peticion aunque el doble envio de cookie sea valido -- respaldo para
    los navegadores que no mandan `Sec-Fetch-Site`."""
    app = create_app(api_settings)

    with TestClient(
        app, base_url="https://testserver", raise_server_exceptions=False
    ) as client:
        client.get("/api/v1/health")
        token = client.cookies.get("ads_csrf")

        response = client.post(
            "/api/v1/auth/login",
            json={"email": "owner@safent.example", "password": "x"},
            headers={"X-CSRF-Token": token, "Origin": "https://evil.example"},
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CSRF_REJECTED"


def test_csrf_accepted_when_origin_matches_public_base_url(api_settings: ApiSettings) -> None:
    """Contraprueba de M4: un `Origin` que SI coincide con
    `ADS_PUBLIC_BASE_URL` no anade ningun rechazo nuevo."""
    app = create_app(api_settings)

    with TestClient(
        app, base_url="https://testserver", raise_server_exceptions=False
    ) as client:
        client.get("/api/v1/health")
        token = client.cookies.get("ads_csrf")

        response = client.post(
            "/api/v1/auth/login",
            json={"email": "owner@safent.example", "password": "x"},
            headers={"X-CSRF-Token": token, "Origin": api_settings.public_base_url},
        )

    assert response.status_code != 403


def test_same_origin_accepts_default_port_normalization() -> None:
    """Nit de la revision de seguridad final (16-sep): `Origin` sin puerto
    explicito y `public_base_url` con el puerto https por defecto (`:443`)
    son el MISMO origen -- una comparacion de cadena exacta (la version
    anterior) los rechazaria por un falso positivo."""
    assert api_composition._same_origin("https://host", "https://host:443") is True


def test_same_origin_rejects_a_different_host() -> None:
    assert api_composition._same_origin("https://evil.example", "https://host") is False


def test_same_origin_rejects_a_different_scheme_with_the_same_host() -> None:
    assert api_composition._same_origin("http://host", "https://host") is False


def test_per_minute_converts_a_monthly_cap_into_a_per_second_refill_rate() -> None:
    """Nit (code review 17-sep): `_per_minute(capacity)` reemplaza el
    `capacity / 60` repetido a mano en cada regla de tasa federada."""
    assert api_composition._per_minute(60) == 1.0
    assert api_composition._per_minute(10) == 10 / 60


def test_rate_limit_autonomy_gate(api_settings: ApiSettings) -> None:
    """T075/F2-F3 TOTP re-auth defecto 3 (CWE-307): antes de este fix,
    `/api/v1/rules/autonomy-gate*` no coincidia con ningun prefijo de
    `_limiters` (solo `/auth`, `/mcp`, `/brand/discover`) y quedaba sin
    limite -- online guessing del codigo TOTP de re-auth era viable. GET,
    no POST: no hace falta CSRF para demostrar que el limitador actua antes
    de que el router resuelva sesion/negocio."""
    app = create_app(api_settings)

    with TestClient(
        app, base_url="https://testserver", raise_server_exceptions=False
    ) as client:
        statuses = [
            client.get(
                "/api/v1/rules/autonomy-gate",
                params={"business_id": "00000000-0000-0000-0000-000000000000"},
            ).status_code
            for _ in range(30)
        ]

    assert 429 in statuses


def test_autonomy_gate_rate_limit_ignores_spoofed_forwarded_for_by_default(
    api_settings: ApiSettings,
) -> None:
    """Code review 17-sep (item 3): `_AUTONOMY_GATE_RATE_LIMIT_PREFIX` se
    quedaba en el `key_of` por defecto (`_rate_limit_key`, que ignora
    `X-Forwarded-For` igual que `_AUTH_RATE_LIMIT_PREFIX` antes de C-76) --
    detras de Caddy el cubo era, sin darse cuenta, uno solo para todo
    Internet. Ahora usa `forwarded_for_rate_limit_key`: sin proxy de
    confianza (defecto `trusted_proxy_hops=0`), rotar la cabecera en cada
    peticion sigue sin evitar el limite."""
    app = create_app(api_settings)

    with TestClient(
        app, base_url="https://testserver", raise_server_exceptions=False
    ) as client:
        statuses = [
            client.get(
                "/api/v1/rules/autonomy-gate",
                params={"business_id": "00000000-0000-0000-0000-000000000000"},
                headers={"X-Forwarded-For": f"10.0.0.{i}"},
            ).status_code
            for i in range(30)
        ]

    assert 429 in statuses


def test_autonomy_gate_rate_limit_is_keyed_per_ip_with_one_trusted_hop(
    api_settings: ApiSettings,
) -> None:
    """Con un proxy de confianza (`ADS_TRUSTED_PROXY_HOPS=1`, Caddy), dos
    IPs reales distintas detras del mismo proxy no comparten cupo -- antes
    de este arreglo, agotar este cubo dejaba sin adivinar el TOTP de
    re-auth a Internet entero, dueno incluido."""
    settings = api_settings.model_copy(update={"trusted_proxy_hops": 1})
    app = create_app(settings)

    with TestClient(
        app, base_url="https://testserver", raise_server_exceptions=False
    ) as client:
        statuses = [
            client.get(
                "/api/v1/rules/autonomy-gate",
                params={"business_id": "00000000-0000-0000-0000-000000000000"},
                headers={"X-Forwarded-For": f"attacker-{i}, 198.51.100.7"},
            ).status_code
            for i in range(30)
        ]
        other_ip = client.get(
            "/api/v1/rules/autonomy-gate",
            params={"business_id": "00000000-0000-0000-0000-000000000000"},
            headers={"X-Forwarded-For": "attacker, 198.51.100.20"},
        ).status_code

    assert 429 in statuses
    assert other_ip != 429


def test_rate_limit_packages_mutations(api_settings: ApiSettings) -> None:
    """T126/AL-7 (revision de seguridad 0.2.22): `/api/v1/packages/**`
    alcanza una escritura real de plataforma (`undo` revierte una campana
    ya publicada) y no tenia presupuesto propio -- 30/min por SESION
    (`ads_session`)."""
    app = create_app(api_settings)

    with TestClient(
        app,
        base_url="https://testserver",
        raise_server_exceptions=False,
        cookies={"ads_session": "session-a"},
    ) as client:
        token = _csrf_token(client)
        headers = {
            "X-CSRF-Token": token,
            "Origin": api_settings.public_base_url,
            "Sec-Fetch-Site": "same-origin",
        }
        statuses = [
            client.post(_PACKAGES_MUTATION_PATH, headers=headers).status_code for _ in range(35)
        ]

    assert 429 in statuses


def test_rate_limit_packages_mutations_is_isolated_per_session(
    api_settings: ApiSettings,
) -> None:
    """Mismo criterio que `/mcp` (sesion A no debe agotar el cupo de la
    sesion B): dos `ads_session` distintas nunca comparten cubeta."""
    app = create_app(api_settings)

    with TestClient(
        app, base_url="https://testserver", raise_server_exceptions=False
    ) as client:
        client.cookies.set("ads_session", "session-a")
        token = _csrf_token(client)
        headers = {
            "X-CSRF-Token": token,
            "Origin": api_settings.public_base_url,
            "Sec-Fetch-Site": "same-origin",
        }
        for _ in range(35):
            client.post(_PACKAGES_MUTATION_PATH, headers=headers)
        exhausted = client.post(_PACKAGES_MUTATION_PATH, headers=headers)

        client.cookies.set("ads_session", "session-b")
        other_session = client.post(_PACKAGES_MUTATION_PATH, headers=headers)

    assert exhausted.status_code == 429
    assert other_session.status_code != 429


def test_rate_limit_packages_does_not_throttle_reads(api_settings: ApiSettings) -> None:
    """El limite nuevo es SOLO para mutaciones -- una lectura del panel
    (GET, poll cada pocos segundos) no debe compartir cupo con `approve`/
    `undo`, ni agotarse por su cuenta."""
    app = create_app(api_settings)

    with TestClient(
        app,
        base_url="https://testserver",
        raise_server_exceptions=False,
        cookies={"ads_session": "session-a"},
    ) as client:
        statuses = [
            client.get("/api/v1/packages/pkg-1").status_code for _ in range(35)
        ]

    assert 429 not in statuses


_CLOUDFLARE_TOKEN_PATH = "/api/v1/integrations/cloudflare/token"  # noqa: S105


def test_rate_limit_cloudflare_token_connect(api_settings: ApiSettings) -> None:
    """Hallazgo bajo (revision de seguridad de la conexion Cloudflare,
    2026-09-15): `POST .../cloudflare/token` valida el token contra la API
    real de Cloudflare ANTES de guardarlo -- sin limite propio es un
    oraculo de validacion de tokens (cada intento revela si un token
    adivinado/robado es valido). 5/min por SESION."""
    app = create_app(api_settings)

    with TestClient(
        app,
        base_url="https://testserver",
        raise_server_exceptions=False,
        cookies={"ads_session": "session-a"},
    ) as client:
        token = _csrf_token(client)
        headers = {
            "X-CSRF-Token": token,
            "Origin": api_settings.public_base_url,
            "Sec-Fetch-Site": "same-origin",
        }
        statuses = [
            client.post(
                _CLOUDFLARE_TOKEN_PATH, headers=headers, json={"token": "sk-fake"}
            ).status_code
            for _ in range(8)
        ]

    assert 429 in statuses


def test_rate_limit_cloudflare_token_is_isolated_per_session(api_settings: ApiSettings) -> None:
    """Mismo criterio que `/api/v1/packages/**`: dos `ads_session`
    distintas nunca comparten cubeta."""
    app = create_app(api_settings)

    with TestClient(
        app, base_url="https://testserver", raise_server_exceptions=False
    ) as client:
        client.cookies.set("ads_session", "session-a")
        token = _csrf_token(client)
        headers = {
            "X-CSRF-Token": token,
            "Origin": api_settings.public_base_url,
            "Sec-Fetch-Site": "same-origin",
        }
        for _ in range(8):
            client.post(_CLOUDFLARE_TOKEN_PATH, headers=headers, json={"token": "sk-fake"})
        exhausted = client.post(_CLOUDFLARE_TOKEN_PATH, headers=headers, json={"token": "sk-fake"})

        client.cookies.set("ads_session", "session-b")
        other_session = client.post(
            _CLOUDFLARE_TOKEN_PATH, headers=headers, json={"token": "sk-fake"}
        )

    assert exhausted.status_code == 429
    assert other_session.status_code != 429


def test_rate_limit_cloudflare_token_does_not_throttle_delete(api_settings: ApiSettings) -> None:
    """El limite nuevo es SOLO para `POST` -- `DELETE` (desconectar) nunca
    toca la red de Cloudflare, no es el oraculo, no debe compartir cupo."""
    app = create_app(api_settings)

    with TestClient(
        app,
        base_url="https://testserver",
        raise_server_exceptions=False,
        cookies={"ads_session": "session-a"},
    ) as client:
        token = _csrf_token(client)
        headers = {
            "X-CSRF-Token": token,
            "Origin": api_settings.public_base_url,
            "Sec-Fetch-Site": "same-origin",
        }
        statuses = [
            client.request(
                "DELETE", _CLOUDFLARE_TOKEN_PATH, headers=headers, json={}
            ).status_code
            for _ in range(8)
        ]

    assert 429 not in statuses


_MCP_ENDPOINT = "/mcp"
_SMALL_MCP_TEST_CAPACITY = 3


def _shrink_mcp_rate_limit_budgets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Los presupuestos reales (600/min por sesion, 60/min sin sesion) son
    demasiado grandes para agotar en un test rapido y determinista --
    `harden_api` lee estas constantes en el momento de construir el
    middleware, asi que basta con encogerlas antes de `create_app`."""
    monkeypatch.setattr(
        api_composition, "_MCP_SESSION_RATE_LIMIT_CAPACITY", _SMALL_MCP_TEST_CAPACITY
    )
    monkeypatch.setattr(
        api_composition, "_MCP_SESSION_LESS_RATE_LIMIT_CAPACITY", _SMALL_MCP_TEST_CAPACITY
    )


def test_mcp_rate_limit_session_exhausted_does_not_affect_another_session(
    api_settings: ApiSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regresion: antes, todo `/mcp` compartia una unica cubeta -- un ciclo
    de agente normal (`initialize` + `tools/list` + una docena de lecturas,
    varios ciclos por hora) se comia el cupo de cualquier otra sesion
    abierta en el mismo proceso `ads-api`. Ahora cada `Mcp-Session-Id`
    tiene su propia cubeta: agotar la de la sesion A no toca la de la
    sesion B."""
    _shrink_mcp_rate_limit_budgets(monkeypatch)
    app = create_app(api_settings)

    with TestClient(
        app, base_url="https://testserver", raise_server_exceptions=False
    ) as client:
        for _ in range(_SMALL_MCP_TEST_CAPACITY):
            client.post(_MCP_ENDPOINT, headers={"Mcp-Session-Id": "session-a"})
        exhausted = client.post(_MCP_ENDPOINT, headers={"Mcp-Session-Id": "session-a"})
        other_session = client.post(_MCP_ENDPOINT, headers={"Mcp-Session-Id": "session-b"})

    assert exhausted.status_code == 429
    assert other_session.status_code != 429


def test_mcp_rate_limit_session_less_budget_protects_initialize_floods(
    api_settings: ApiSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`initialize` todavia no trae `Mcp-Session-Id` -- el SDK se lo da en
    su propia respuesta, el cliente lo repite a partir de la siguiente
    llamada -- asi que se queda con el cupo estrecho sin sesion: una
    inundacion de `initialize` sigue cortandose aunque haya una sesion ya
    abierta y con cupo intacto alrededor."""
    _shrink_mcp_rate_limit_budgets(monkeypatch)
    app = create_app(api_settings)

    with TestClient(
        app, base_url="https://testserver", raise_server_exceptions=False
    ) as client:
        client.post(_MCP_ENDPOINT, headers={"Mcp-Session-Id": "session-a"})
        for _ in range(_SMALL_MCP_TEST_CAPACITY):
            client.post(_MCP_ENDPOINT)
        exhausted = client.post(_MCP_ENDPOINT)

    assert exhausted.status_code == 429


def test_oauth_register_rate_limit_ignores_spoofed_forwarded_for_by_default(
    api_settings: ApiSettings,
) -> None:
    """H1: `ApiSettings.trusted_proxy_hops` es `0` por defecto -- rotar
    `X-Forwarded-For` en cada peticion de registro DCR (`/register`) no
    evita el limite de tasa, porque sin proxy de confianza delante la
    cabecera se ignora por completo."""
    app = create_app(api_settings)

    with TestClient(
        app, base_url="https://testserver", raise_server_exceptions=False
    ) as client:
        statuses = [
            client.post(
                "/register", json={}, headers={"X-Forwarded-For": f"10.0.0.{i}"}
            ).status_code
            for i in range(12)
        ]

    assert 429 in statuses
    assert statuses.index(429) == 10


def test_oauth_register_rate_limit_uses_rightmost_forwarded_for_with_one_hop(
    api_settings: ApiSettings,
) -> None:
    """H1: con `ADS_TRUSTED_PROXY_HOPS=1` (Caddy en la VM / `tailscale
    serve`), el valor que ese proxy anadio -- el mas a la derecha -- es el
    que particiona el limite. Doce peticiones del mismo cliente real
    detras del proxy, cada una intentando falsear un prefijo distinto a su
    izquierda, comparten cubeta igual que con una IP fija."""
    settings = api_settings.model_copy(update={"trusted_proxy_hops": 1})
    app = create_app(settings)

    with TestClient(
        app, base_url="https://testserver", raise_server_exceptions=False
    ) as client:
        statuses = [
            client.post(
                "/register",
                json={},
                headers={"X-Forwarded-For": f"attacker-{i}, 198.51.100.7"},
            ).status_code
            for i in range(12)
        ]

    assert 429 in statuses
    assert statuses.index(429) == 10


def _csrf_headers(client: TestClient) -> dict[str, str]:
    """L3: desde el reordenamiento de middlewares, `CsrfMiddleware` corre
    ANTES que `RateLimitMiddleware` (envoltura de fuera adentro: RequestId
    -> ForwardedPrefix -> SecurityHeaders -> Csrf -> RateLimit) -- toda
    ruta mutante no exenta de CSRF necesita un token valido para siquiera
    llegar al limitador, igual que `test_rate_limit_auth` de mas arriba."""
    client.get("/api/v1/health")
    token = client.cookies.get("ads_csrf")
    return {"X-CSRF-Token": token} if token else {}


def test_discover_route_is_rate_limited_per_business(api_settings: ApiSettings) -> None:
    """F-7: `POST /brand/discover` dispara N peticiones salientes por
    llamada -- 3/h por `business_id`."""
    app = create_app(api_settings)
    business_id = "11111111-1111-1111-1111-111111111111"

    with TestClient(
        app, base_url="https://testserver", raise_server_exceptions=False
    ) as client:
        headers = _csrf_headers(client)
        statuses = [
            client.post(
                f"/api/v1/brand/discover?business_id={business_id}",
                json={"url": "https://example.test/"},
                headers=headers,
            ).status_code
            for _ in range(5)
        ]

    assert statuses.count(429) >= 1


def test_discover_route_rate_limit_is_scoped_per_business(api_settings: ApiSettings) -> None:
    """La clave es `business_id`, no la sesion/IP del llamante: agotar el
    cupo de un negocio no afecta a otro."""
    app = create_app(api_settings)
    business_a = "11111111-1111-1111-1111-111111111111"
    business_b = "22222222-2222-2222-2222-222222222222"

    with TestClient(
        app, base_url="https://testserver", raise_server_exceptions=False
    ) as client:
        headers = _csrf_headers(client)
        for _ in range(3):
            client.post(
                f"/api/v1/brand/discover?business_id={business_a}",
                json={"url": "https://example.test/"},
                headers=headers,
            )
        exhausted = client.post(
            f"/api/v1/brand/discover?business_id={business_a}",
            json={"url": "https://example.test/"},
            headers=headers,
        )
        other_business = client.post(
            f"/api/v1/brand/discover?business_id={business_b}",
            json={"url": "https://example.test/"},
            headers=headers,
        )

    assert exhausted.status_code == 429
    assert other_business.status_code != 429


# ---------------------------------------------------------------------------
# `_rate_limited_response`/`_csrf_rejected_response` (Starlette `Response`
# es mutable -- headers por instancia): antes eran `JSONResponse` unicos a
# nivel de modulo, compartidos por toda peticion Y por toda la bateria de
# tests en el mismo proceso de pytest. Una mutacion de cabecera en una
# peticion (`Retry-After` calculado, CSP con nonce, lo que sea) se filtraria
# a la siguiente. Ahora son fabricas: cada llamada devuelve una instancia
# nueva con el mismo cuerpo/status de siempre.
# ---------------------------------------------------------------------------


def test_rate_limited_response_body_and_status_are_unchanged() -> None:
    response = api_composition._rate_limited_response()

    assert response.status_code == 429
    assert response.headers["retry-after"] == "60"


def test_two_consecutive_rate_limited_responses_are_independent_instances() -> None:
    first = api_composition._rate_limited_response()
    second = api_composition._rate_limited_response()

    assert first is not second


def test_two_consecutive_rate_limited_responses_carry_their_own_retry_after() -> None:
    first = api_composition._rate_limited_response()
    first.headers["retry-after"] = "999"

    second = api_composition._rate_limited_response()

    assert second.headers["retry-after"] == "60"
    assert first.headers["retry-after"] == "999"


def test_csrf_rejected_response_body_and_status_are_unchanged() -> None:
    response = api_composition._csrf_rejected_response()

    assert response.status_code == 403
    assert json.loads(response.body)["error"]["code"] == "CSRF_REJECTED"


def test_two_consecutive_csrf_rejected_responses_do_not_share_header_mutations() -> None:
    first = api_composition._csrf_rejected_response()
    first.headers["x-debug-leak"] = "from-first-request"

    second = api_composition._csrf_rejected_response()

    assert first is not second
    assert "x-debug-leak" not in second.headers


# ---------------------------------------------------------------------------
# H3/AL-7: helpers puros del guardia de `Origin`/`Sec-Fetch-Site` de
# `/api/v1/packages/**` (threat-model.md #364, tasks.md T126).
# ---------------------------------------------------------------------------


def _request_with_headers(headers: dict[str, str]) -> Request:
    encoded = [(name.lower().encode(), value.encode()) for name, value in headers.items()]
    return Request({"type": "http", "headers": encoded})


def test_allowed_origins_include_the_public_base_url_and_localhost() -> None:
    allowed = api_composition._allowed_origins_for("https://ads.example.com")

    assert "https://ads.example.com" in allowed
    assert "https://127.0.0.1" in allowed
    assert "http://127.0.0.1" in allowed


def test_allowed_origins_include_extra_allowed_hosts_as_https() -> None:
    """B-4 (revision de seguridad 0.2.22): el host provisional de
    `ADS_MCP_EXTRA_ALLOWED_HOSTS` (p.ej. `ads.example.test`) tiene
    que colar como origen `https://` -- mismo criterio que
    `mcp/presentation/http.py::_transport_security_for`."""
    allowed = api_composition._allowed_origins_for(
        "https://ads.example.com",
        extra_allowed_hosts=frozenset({"ads.example.test"}),
    )

    assert "https://ads.example.test" in allowed
    assert "https://ads.example.com" in allowed


def test_requires_origin_guard_matches_only_the_packages_prefix() -> None:
    assert api_composition._requires_origin_guard("/api/v1/packages/pkg-1/approve")
    assert not api_composition._requires_origin_guard("/api/v1/auth/login")


def test_origin_guard_rejects_a_missing_origin() -> None:
    request = _request_with_headers({})

    assert api_composition._origin_guard_rejected(request, frozenset({"https://ads.test"}))


def test_origin_guard_rejects_an_origin_outside_the_allow_list() -> None:
    request = _request_with_headers({"origin": "https://evil.example"})

    assert api_composition._origin_guard_rejected(request, frozenset({"https://ads.test"}))


def test_origin_guard_rejects_cross_site_even_with_an_allowed_origin() -> None:
    request = _request_with_headers(
        {"origin": "https://ads.test", "sec-fetch-site": "cross-site"}
    )

    assert api_composition._origin_guard_rejected(request, frozenset({"https://ads.test"}))


def test_origin_guard_accepts_a_same_origin_allowed_request() -> None:
    request = _request_with_headers(
        {"origin": "https://ads.test", "sec-fetch-site": "same-origin"}
    )

    assert not api_composition._origin_guard_rejected(request, frozenset({"https://ads.test"}))


def test_origin_guard_accepts_an_allowed_origin_without_sec_fetch_site() -> None:
    """No todos los clientes legitimos mandan Fetch Metadata (navegadores
    antiguos, algunos clientes no-navegador internos): la ausencia de
    `Sec-Fetch-Site` no rechaza por si sola, solo un `cross-site` explicito."""
    request = _request_with_headers({"origin": "https://ads.test"})

    assert not api_composition._origin_guard_rejected(request, frozenset({"https://ads.test"}))


# ---------------------------------------------------------------------------
# AL-7 (revision de seguridad 0.2.22): replay no-navegador de la cookie de
# sesion (`ads_session`) sin Origin NI Sec-Fetch-Site -- capa independiente
# del doble envio, `PackagesOriginRequiredMiddleware`.
# ---------------------------------------------------------------------------


def test_session_cookie_replay_without_origin_or_sec_fetch_site_is_flagged() -> None:
    request = _request_with_headers({"cookie": "ads_session=stolen-token"})

    assert api_composition._is_session_cookie_replay_without_origin(request)


def test_session_cookie_with_origin_header_is_not_flagged_as_replay() -> None:
    request = _request_with_headers(
        {"cookie": "ads_session=tok", "origin": "https://ads.test"}
    )

    assert not api_composition._is_session_cookie_replay_without_origin(request)


def test_session_cookie_with_sec_fetch_site_header_is_not_flagged_as_replay() -> None:
    request = _request_with_headers(
        {"cookie": "ads_session=tok", "sec-fetch-site": "same-origin"}
    )

    assert not api_composition._is_session_cookie_replay_without_origin(request)


def test_bearer_request_without_a_session_cookie_is_never_flagged_as_replay() -> None:
    """Bearer/MCP (sin cookie `ads_session`) no dispara esta condicion --
    solo mira la cookie de sesion, nunca la ausencia de Origin por si
    sola."""
    request = _request_with_headers({})

    assert not api_composition._is_session_cookie_replay_without_origin(request)
