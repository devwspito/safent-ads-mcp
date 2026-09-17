#!/usr/bin/env bash
# Humo F2/US2 (tasks.md T074, quickstart.md §6): la pila de `docker
# compose` de verdad, atacada por REST con curl (login+TOTP del
# propietario sembrado) y una propuesta creada por una TOOL MCP real sobre
# `streamable-http` con bearer (`python -m safent_ads.tools.seed_owner`
# mas un cliente MCP minimo -- de punta a punta con `curl` a mano no es
# viable: el transporte es JSON-RPC 2.0/SSE, no REST; ningun cliente MCP
# real lo hace con `curl` tampoco, ver quickstart.md §5.7 "npx -y
# mcp-remote"), aprobada por REST, y observada hasta su desenlace.
#
# Igual disciplina que f0_smoke.sh: cada paso falla alto y con motivo, sin
# `| tail` ni pipes que oculten un fallo.
#
# Requiere: `docker compose up -d ads-db ads-broker ads-api ads-worker` ya
# corriendo (Makefile `up`), con `secrets/broker.env`/`.env` rellenos
# (quickstart.md §1) y `curl`/`python3`/`docker`/`jq`-less (usa python3
# para JSON) en el PATH.
#
# LIMITE DOCUMENTADO (no es un fallo del script): sin credenciales REALES
# de una cuenta de prueba de Google Ads/Meta en `secrets/broker.env`, el
# bróker no puede escribir de verdad -- este humo observa y REPORTA el
# desenlace real que devuelva la pila (EXECUTED con credenciales reales;
# FAILED/BLOCKED con las de ejemplo), nunca lo fuerza. `GET
# /api/v1/executions/{id}` (contracts/rest-api.md:193, citado por
# quickstart.md §6.5/§7.3) tampoco esta cableado en `composition/
# execution_rest.py` (ver el `xfail` que lo prueba en
# tests/e2e/test_us2_defensive_autonomy.py) -- este humo cae a `GET
# /api/v1/proposals/{id}` (`state`) para observar el desenlace, documentado
# como sustituto explicito, no silencioso.
#
# Uso:
#   ADS_SMOKE_BASE_URL=http://127.0.0.1:8410 tests/smoke/us2_smoke.sh

set -euo pipefail

BASE_URL="${ADS_SMOKE_BASE_URL:-http://127.0.0.1:8410}"
OWNER_EMAIL="${ADS_SMOKE_OWNER_EMAIL:-us2-smoke@safent.example}"
OWNER_PASSWORD="${ADS_SMOKE_OWNER_PASSWORD:-$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')}"
MCP_TOKEN="${ADS_MCP_TOKEN:?ADS_MCP_TOKEN requerido (el mismo bearer que ads-api espera en /mcp)}"
COOKIE_JAR="$(mktemp)"
SMOKE_SUFFIX="$(python3 -c 'import uuid; print(uuid.uuid4().hex[:10])')"
SMOKE_ENTITY_REF="google:campaign:us2-smoke-${SMOKE_SUFFIX}"

trap 'rm -f "$COOKIE_JAR"' EXIT

log() { printf '[us2-smoke] %s\n' "$1" >&2; }
fail() { printf '[us2-smoke] FALLO: %s\n' "$1" >&2; exit 1; }

require_field() {
  # require_field <json> <python-expr-sobre-"data"> <descripcion>
  local value
  value="$(printf '%s' "$1" | python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
except json.JSONDecodeError:
    sys.exit(1)
result = $2
print(result if result is not None else '')
" 2>/dev/null)" || fail "$3: respuesta no es el JSON esperado -> $1"
  printf '%s' "$value"
}

log "0a/9 sembrando propietario de prueba ($OWNER_EMAIL)"
SEED_OUTPUT="$(printf '%s' "$OWNER_PASSWORD" | docker compose exec -T ads-api python -m safent_ads.tools.seed_owner \
  --email "$OWNER_EMAIL" --password-stdin)" \
  || fail "seed_owner no pudo ejecutarse dentro de ads-api"
TOTP_CODE="$(printf '%s\n' "$SEED_OUTPUT" | grep '^TOTP_CODE_NOW=' | cut -d= -f2)"
[[ -n "$TOTP_CODE" ]] || fail "seed_owner no imprimio TOTP_CODE_NOW"

log "0b/9 sembrando negocio/campana/guardarraíles de prueba ($SMOKE_ENTITY_REF)"
docker compose exec -T ads-db psql -U ads -d ads -v ON_ERROR_STOP=1 -c "
  DO \$\$
  DECLARE
    v_business_id uuid := gen_random_uuid();
    v_credential_id uuid := gen_random_uuid();
    v_account_id uuid := gen_random_uuid();
  BEGIN
    INSERT INTO businesses (id, slug, name, timezone, reference_currency)
      VALUES (v_business_id, 'us2-smoke-${SMOKE_SUFFIX}', 'US2 Smoke', 'Europe/Madrid', 'EUR');
    INSERT INTO credential_refs (id, platform, alias)
      VALUES (v_credential_id, 'google', 'us2-smoke-${SMOKE_SUFFIX}');
    INSERT INTO platform_accounts (id, business_id, platform, external_account_id, currency,
                                   timezone, api_tier, credential_ref_id, status)
      VALUES (v_account_id, v_business_id, 'google', 'act_us2-smoke-${SMOKE_SUFFIX}', 'EUR',
              'Europe/Madrid', 'meta_full', v_credential_id, 'ACTIVE');
    INSERT INTO ad_entities (business_id, platform_account_id, platform, level, external_id,
                             name, status, platform_state_hash, budget_amount_minor,
                             budget_currency, budget_kind)
      VALUES (v_business_id, v_account_id, 'google', 'campaign', 'us2-smoke-${SMOKE_SUFFIX}',
              'Campana de humo US2', 'ACTIVE', repeat('a', 64), 10000, 'EUR', 'daily');
    INSERT INTO guardrails (scope, business_id, currency, daily_cap_minor, monthly_cap_minor,
                            budget_floor_minor, budget_ceiling_minor, max_step_pct,
                            max_changes_per_entity_per_day)
      VALUES ('business', v_business_id, 'EUR', 50000, 900000, 1000, 30000, 50, 5);
    RAISE NOTICE 'SMOKE_BUSINESS_ID=%', v_business_id;
  END \$\$;
" 2>&1 | tee /tmp/us2_smoke_seed.log >/dev/null || fail "no se pudo sembrar negocio/campana/guardarraíles"
BUSINESS_ID="$(grep -oE 'SMOKE_BUSINESS_ID=[0-9a-f-]+' /tmp/us2_smoke_seed.log | cut -d= -f2)"
[[ -n "$BUSINESS_ID" ]] || fail "no se pudo leer SMOKE_BUSINESS_ID de la salida de psql"
log "negocio de humo: $BUSINESS_ID"

log "1/9 GET /api/v1/health (liveness)"
curl -fsS "$BASE_URL/api/v1/health" >/dev/null || fail "health liveness no responde"

log "2/9 cookie CSRF + POST /api/v1/auth/login -> stage totp_required"
curl -fsS -c "$COOKIE_JAR" "$BASE_URL/api/v1/health" >/dev/null
CSRF_TOKEN="$(awk '$6 == "ads_csrf" {print $7}' "$COOKIE_JAR")"
[[ -n "$CSRF_TOKEN" ]] || fail "no se recibio la cookie ads_csrf"
LOGIN_BODY="$(curl -fsS -c "$COOKIE_JAR" -b "$COOKIE_JAR" \
  -H "X-CSRF-Token: $CSRF_TOKEN" -H 'Content-Type: application/json' \
  -d "{\"email\":\"$OWNER_EMAIL\",\"password\":\"$OWNER_PASSWORD\"}" \
  "$BASE_URL/api/v1/auth/login")" || fail "POST /auth/login fallo"
STAGE="$(require_field "$LOGIN_BODY" "data['stage']" "auth/login")"
[[ "$STAGE" == "totp_required" ]] || fail "auth/login no devolvio stage=totp_required: $LOGIN_BODY"
CHALLENGE_ID="$(require_field "$LOGIN_BODY" "data['challenge_id']" "auth/login")"

log "3/9 POST /api/v1/auth/totp -> cookie de sesion"
TOTP_STATUS="$(curl -fsS -o /dev/null -w '%{http_code}' -c "$COOKIE_JAR" -b "$COOKIE_JAR" \
  -H "X-CSRF-Token: $CSRF_TOKEN" -H 'Content-Type: application/json' \
  -d "{\"challenge_id\":\"$CHALLENGE_ID\",\"code\":\"$TOTP_CODE\"}" \
  "$BASE_URL/api/v1/auth/totp")"
[[ "$TOTP_STATUS" == "204" ]] || fail "auth/totp no devolvio 204 (fue $TOTP_STATUS)"
grep -q 'ads_session' "$COOKIE_JAR" || fail "no se recibio la cookie de sesion ads_session"

log "4/9 proponer bajada de presupuesto via TOOL MCP real (propose_budget_change, streamable-http+bearer)"
PROPOSAL_JSON="$(BASE_URL="$BASE_URL" MCP_TOKEN="$MCP_TOKEN" BUSINESS_ID="$BUSINESS_ID" \
  ENTITY_REF="$SMOKE_ENTITY_REF" python3 - <<'PYEOF'
import asyncio
import json
import os
import sys

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def main() -> None:
    base_url = os.environ["BASE_URL"].rstrip("/")
    token = os.environ["MCP_TOKEN"]
    http_client = httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"})
    try:
        async with streamable_http_client(f"{base_url}/mcp", http_client=http_client) as (
            read_stream,
            write_stream,
            _get_session_id,
        ):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(
                    "propose_budget_change",
                    {
                        "business_id": os.environ["BUSINESS_ID"],
                        "entity_ref": os.environ["ENTITY_REF"],
                        "new_daily_budget_amount": "90",
                        "new_daily_budget_currency": "EUR",
                        "cause": {"text": "Humo US2 (tests/smoke/us2_smoke.sh)"},
                        "evidence": [],
                        "urgency": "recommended",
                    },
                )
                if result.isError:
                    print(json.dumps({"error": [c.text for c in result.content]}))
                    return
                payload = result.structuredContent
                if payload is None:
                    payload = json.loads(result.content[0].text)
                print(json.dumps(payload))
    finally:
        await http_client.aclose()


asyncio.run(main())
PYEOF
)" || fail "la llamada MCP a propose_budget_change fallo"
PROPOSAL_ERROR="$(require_field "$PROPOSAL_JSON" "data.get('error')" "propose_budget_change")"
[[ -z "$PROPOSAL_ERROR" ]] || fail "propose_budget_change devolvio error: $PROPOSAL_JSON"
PROPOSAL_ID="$(require_field "$PROPOSAL_JSON" "data['proposal_id']" "propose_budget_change")"
DIFF_HASH="$(require_field "$PROPOSAL_JSON" "data['diff_hash']" "propose_budget_change")"
[[ -n "$PROPOSAL_ID" && -n "$DIFF_HASH" ]] || fail "propose_budget_change no devolvio proposal_id/diff_hash: $PROPOSAL_JSON"
log "propuesta pendiente: $PROPOSAL_ID (diff_hash=${DIFF_HASH:0:12}...)"

log "5/9 GET /api/v1/proposals/{id} -> state=pending (la tool MCP nunca toca la plataforma)"
PROPOSAL_BODY="$(curl -fsS -b "$COOKIE_JAR" \
  "$BASE_URL/api/v1/proposals/$PROPOSAL_ID?business_id=$BUSINESS_ID")" \
  || fail "GET /proposals/{id} fallo"
PROPOSAL_STATE="$(require_field "$PROPOSAL_BODY" "data['summary']['state']" "proposals/{id}")"
[[ "$PROPOSAL_STATE" == "pending" ]] || fail "la propuesta no nacio pendiente: $PROPOSAL_BODY"

log "6/9 POST /api/v1/proposals/{id}/approve -> autorizacion humana firmada"
APPROVE_BODY="$(curl -fsS -c "$COOKIE_JAR" -b "$COOKIE_JAR" \
  -H "X-CSRF-Token: $CSRF_TOKEN" -H 'Content-Type: application/json' \
  -d "{\"diff_hash\":\"$DIFF_HASH\"}" \
  "$BASE_URL/api/v1/proposals/$PROPOSAL_ID/approve")" || fail "POST /proposals/{id}/approve fallo"
GRACE_SECONDS="$(require_field "$APPROVE_BODY" "data['grace_seconds']" "proposals/{id}/approve")"
[[ -n "$GRACE_SECONDS" ]] || fail "approve no devolvio grace_seconds: $APPROVE_BODY"
log "aprobada; ExecutionCycle la recoge tras la gracia servidora (${GRACE_SECONDS}s) + hasta 30s de tick"

log "7/9 GET /api/v1/executions/{id} -- documentado como NO cableado (contracts/rest-api.md:193)"
EXECUTIONS_STATUS="$(curl -s -o /dev/null -w '%{http_code}' -b "$COOKIE_JAR" \
  "$BASE_URL/api/v1/executions/$PROPOSAL_ID" || true)"
if [[ "$EXECUTIONS_STATUS" == "404" ]]; then
  log "confirmado: /api/v1/executions/{id} no existe (404) -- gap ya reportado, no es un fallo de este humo"
elif [[ -n "$EXECUTIONS_STATUS" ]]; then
  log "AVISO: /api/v1/executions/{id} respondio $EXECUTIONS_STATUS -- si ya esta cableado, actualizar este humo y el xfail de test_us2_defensive_autonomy.py"
fi

log "8/9 esperando el desenlace real (poll deterministico sobre GET /proposals/{id}, hasta 90s)"
FINAL_STATE="pending"
for _ in $(seq 1 45); do
  POLL_BODY="$(curl -fsS -b "$COOKIE_JAR" \
    "$BASE_URL/api/v1/proposals/$PROPOSAL_ID?business_id=$BUSINESS_ID")" \
    || fail "GET /proposals/{id} fallo durante el sondeo"
  FINAL_STATE="$(require_field "$POLL_BODY" "data['summary']['state']" "proposals/{id}")"
  case "$FINAL_STATE" in
    executed|failed|invalidated|expired) break ;;
    *) sleep 2 ;;
  esac
done

log "9/9 desenlace observado: state=$FINAL_STATE"
case "$FINAL_STATE" in
  executed)
    log "US2 OK: EXECUTED de punta a punta (credenciales reales de bróker configuradas)"
    ;;
  scheduled|approved)
    fail "la propuesta no llego a un estado terminal en 90s (quedo en $FINAL_STATE) -- ¿ExecutionCycle/ads-worker esta vivo?"
    ;;
  *)
    log "AVISO (no es un fallo de este humo): desenlace terminal '$FINAL_STATE', no EXECUTED -- "
    log "esperado sin credenciales REALES de una cuenta de prueba de Google Ads/Meta en secrets/broker.env"
    log "US2 humo COMPLETO: propuesta MCP -> aprobacion REST -> desenlace real observado ($FINAL_STATE)"
    ;;
esac
