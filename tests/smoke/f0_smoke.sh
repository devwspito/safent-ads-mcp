#!/usr/bin/env bash
# Humo F0 (tasks.md T018, quickstart.md §4): tres procesos vivos, cadena de
# auditoria verificable, sin via de escritura antes de F2. Cada paso falla
# alto y con motivo -- sin `| tail` ni pipes que oculten un `curl` roto.
#
# Requiere: `docker compose up -d ads-db ads-broker ads-api` ya corriendo
# (Makefile `up`) y `curl`/`python3`/`docker` en el PATH.
#
# Uso:
#   ADS_SMOKE_BASE_URL=http://127.0.0.1:8410 tests/smoke/f0_smoke.sh

set -euo pipefail

BASE_URL="${ADS_SMOKE_BASE_URL:-http://127.0.0.1:8410}"
OWNER_EMAIL="${ADS_SMOKE_OWNER_EMAIL:-smoke@safent.example}"
OWNER_PASSWORD="${ADS_SMOKE_OWNER_PASSWORD:-$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')}"
COOKIE_JAR="$(mktemp)"

trap 'rm -f "$COOKIE_JAR"' EXIT

log() { printf '[f0-smoke] %s\n' "$1" >&2; }
fail() { printf '[f0-smoke] FALLO: %s\n' "$1" >&2; exit 1; }

require_field() {
  # require_field <json> <jq-filter> <descripcion>
  local value
  value="$(printf '%s' "$1" | python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
except json.JSONDecodeError:
    sys.exit(1)
print(data$2 if data$2 is not None else '')
" 2>/dev/null)" || fail "$3: respuesta no es el JSON esperado -> $1"
  printf '%s' "$value"
}

log "0/7 sembrando propietario de prueba ($OWNER_EMAIL)"
SEED_OUTPUT="$(printf '%s' "$OWNER_PASSWORD" | docker compose exec -T ads-api python -m safent_ads.tools.seed_owner \
  --email "$OWNER_EMAIL" --password-stdin)" \
  || fail "seed_owner no pudo ejecutarse dentro de ads-api"

log "1/7 GET /api/v1/health (liveness, sin autenticacion)"
curl -fsS "$BASE_URL/api/v1/health" >/dev/null || fail "health liveness no responde"

log "2/7 GET / -> panel SPA horneado en la imagen (T053, sin autenticacion)"
ROOT_BODY="$(curl -fsS "$BASE_URL/")" || fail "GET / no responde (panel no horneado en la imagen)"
printf '%s' "$ROOT_BODY" | grep -q '<div id="root">' \
  || fail "GET / no devolvio el index.html del panel (falta panel/dist en la imagen)"

log "3/7 obteniendo cookie CSRF"
curl -fsS -c "$COOKIE_JAR" "$BASE_URL/api/v1/health" >/dev/null
CSRF_TOKEN="$(awk '$6 == "ads_csrf" {print $7}' "$COOKIE_JAR")"
[[ -n "$CSRF_TOKEN" ]] || fail "no se recibio la cookie ads_csrf"

log "4/7 POST /api/v1/auth/login -> sesión Community sin OTP"
LOGIN_STATUS="$(curl -fsS -o /dev/null -w '%{http_code}' -c "$COOKIE_JAR" -b "$COOKIE_JAR" \
  -H "X-CSRF-Token: $CSRF_TOKEN" -H 'Content-Type: application/json' \
  -d "{\"email\":\"$OWNER_EMAIL\",\"password\":\"$OWNER_PASSWORD\"}" \
  "$BASE_URL/api/v1/auth/login")" || fail "POST /auth/login fallo"
[[ "$LOGIN_STATUS" == "204" ]] || fail "auth/login no devolvio 204"
log "5/7 verificando cookie de sesión"
grep -q 'ads_session' "$COOKIE_JAR" || fail "no se recibio la cookie de sesion ads_session"

log "6/7 GET /api/v1/health/deep -> db:ok, broker:ok"
DEEP_BODY="$(curl -fsS -b "$COOKIE_JAR" "$BASE_URL/api/v1/health/deep")" \
  || fail "GET /health/deep fallo"
DB_STATUS="$(require_field "$DEEP_BODY" "['db']" "health/deep")"
BROKER_STATUS="$(require_field "$DEEP_BODY" "['broker']" "health/deep")"
[[ "$DB_STATUS" == "ok" ]] || fail "health/deep: db no esta ok ($DEEP_BODY)"
[[ "$BROKER_STATUS" == "ok" ]] || fail "health/deep: broker no esta ok ($DEEP_BODY)"

log "7/7a broker en denegacion: ninguna via de escritura antes de F2"
BROKER_PROBE="$(docker compose exec -T ads-api python -m safent_ads.tools.broker_probe --write)" \
  || fail "broker_probe no pudo ejecutarse dentro de ads-api"
PROBE_ERROR_CODE="$(require_field "$BROKER_PROBE" "['error_code']" "broker_probe")"
[[ "$PROBE_ERROR_CODE" == "DENIED" ]] || fail "broker_probe --write no devolvio DENIED: $BROKER_PROBE"

log "7/7b GET /api/v1/decision-log/verify -> chain_ok true"
VERIFY_BODY="$(curl -fsS -b "$COOKIE_JAR" "$BASE_URL/api/v1/decision-log/verify")" \
  || fail "GET /decision-log/verify fallo"
CHAIN_OK="$(require_field "$VERIFY_BODY" "['chain_ok']" "decision-log/verify")"
[[ "$CHAIN_OK" == "True" ]] || fail "decision-log/verify: chain_ok no es true ($VERIFY_BODY)"

log "7/7c inmutabilidad: UPDATE sobre decision_log debe fallar por trigger"
if docker compose exec -T ads-db psql -U ads -d ads \
    -c "update decision_log set payload='{}' where seq = 1;" >/dev/null 2>&1; then
  fail "UPDATE sobre decision_log tuvo exito: el trigger de inmutabilidad no esta activo"
fi

log "F0 OK: tres procesos vivos, cadena verificable, sin via de escritura"
