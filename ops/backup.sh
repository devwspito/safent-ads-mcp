#!/usr/bin/env bash
# ops/backup.sh — T125. Sustituye la logica del target `backup` del
# Makefile (una sola fuente de verdad; `Makefile:backup` solo llama aqui):
#
#   ops/backup.sh
#
# Escribe en `backups/` (fuera de git, runbook.md §7):
#   ads-backup-<UTC>.dump                 -- pg_dump --format=custom
#   ads-backup-<UTC>-credential-store.tar.gz -- almacen cifrado del broker
#   ads-backup-<UTC>-caps.yaml             -- topes duros (config/caps.yaml)
#   ads-backup-<UTC>-caps-state.tar.gz     -- topes fijados desde el panel
#                                             (ADS_BROKER_CAPS_STATE_DIR), si
#                                             el despliegue los declara
#   ads-backup-<UTC>-manifest.txt          -- SOLO NOMBRES de las variables
#                                              de secrets/*.env, NUNCA valores
#
# Nada de esto reemplaza rotar/reconectar tras restaurar en otro host:
# `ADS_CREDENTIAL_MASTER_KEY` no viaja en el manifiesto (es un secreto, no
# un nombre), asi que el almacen cifrado solo es legible con la MISMA clave
# maestra que tenia el origen (runbook.md §7 "Rotacion").
#
# Variables de entorno para pruebas/entornos alternativos (nunca para
# produccion real, que usa los valores por defecto):
#   ADS_BACKUP_DB_EXEC          -- como llegar al Postgres real. Por
#                                  defecto `docker compose exec -T ads-db`
#                                  porque ese servicio NO publica puerto al
#                                  host (compose.yaml) -- exec dentro del
#                                  contenedor es el UNICO camino. Un ensayo
#                                  de restauracion contra un Postgres
#                                  descartable pasa aqui p. ej.
#                                  `podman exec -i ads-backup-proof-1`.
#   ADS_BACKUP_BROKER_EXEC      -- idem para `ads-broker` (credential-store).
#                                  Por defecto `docker compose exec -T
#                                  ads-broker`.
#   ADS_BACKUP_DIR              -- por defecto `backups`.
#   ADS_BACKUP_SKIP_CREDENTIAL_STORE=1 -- omite el volumen del broker (p.
#                                  ej. una instalacion que todavia no
#                                  conecto ninguna cuenta, o un ensayo que
#                                  solo prueba la base de datos).
#   ADS_BACKUP_SKIP_CAPS=1       -- omite la copia de `config/caps.yaml`.
#   ADS_BACKUP_SKIP_CAPS_STATE=1 -- omite el estado de topes del panel.
#   ADS_BROKER_CAPS_STATE_DIR   -- por defecto, el valor declarado en
#                                  `secrets/broker.env`. Sin declarar, no
#                                  hay topes del panel que copiar.

set -euo pipefail

ADS_BACKUP_DIR="${ADS_BACKUP_DIR:-backups}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT_PREFIX="${ADS_BACKUP_DIR}/ads-backup-${STAMP}"

# `read -ra` en vez de `IFS=' '` a mano: acepta un exec con mas de dos
# palabras (`docker compose exec -T ads-db`) sin que cada palabra necesite
# su propia variable.
read -ra ADS_DB_EXEC <<< "${ADS_BACKUP_DB_EXEC:-docker compose exec -T ads-db}"
read -ra ADS_BROKER_EXEC <<< "${ADS_BACKUP_BROKER_EXEC:-docker compose exec -T ads-broker}"

# Lee una CLAVE=valor sin ejecutar el fichero (nunca `source`: un fichero de
# secretos no es un script). Sin fichero o sin clave, devuelve vacio.
env_value() {
  [ -f "$1" ] || return 0
  sed -n "s/^$2=//p" "$1" | tail -n 1
}

mkdir -p "$ADS_BACKUP_DIR"

echo "==> pg_dump (formato custom) -> ${OUT_PREFIX}.dump"
"${ADS_DB_EXEC[@]}" sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom' \
  > "${OUT_PREFIX}.dump"

if [ "${ADS_BACKUP_SKIP_CREDENTIAL_STORE:-0}" = "1" ]; then
  echo "==> almacen cifrado de credenciales: omitido (ADS_BACKUP_SKIP_CREDENTIAL_STORE=1)"
else
  echo "==> almacen cifrado de credenciales -> ${OUT_PREFIX}-credential-store.tar.gz"
  # `tar` dentro del propio contenedor de ads-broker (imagen `python:3.12-
  # slim-bookworm`, Containerfile): reutiliza lo que ya esta corriendo en
  # vez de anadir una imagen nueva (alpine, busybox...) solo para el
  # backup. El volumen sigue siendo el mismo `credential-store`
  # (compose.yaml) que ads-broker ya tiene montado en /var/lib/ads-broker.
  "${ADS_BROKER_EXEC[@]}" tar czf - -C /var/lib/ads-broker . \
    > "${OUT_PREFIX}-credential-store.tar.gz"
fi

if [ "${ADS_BACKUP_SKIP_CAPS:-0}" = "1" ] || [ ! -f config/caps.yaml ]; then
  echo "==> config/caps.yaml: omitido (ausente o ADS_BACKUP_SKIP_CAPS=1)"
else
  echo "==> config/caps.yaml -> ${OUT_PREFIX}-caps.yaml"
  cp config/caps.yaml "${OUT_PREFIX}-caps.yaml"
fi

# 008 T020 (data-model.md §Migration plan, punto 4): el estado de topes que
# el panel fija (fase D) vive en el directorio 0700 del broker, no en
# Postgres ni en `config/caps.yaml`. Un respaldo que se lo deja fuera
# restaura una instancia que deniega todo o, peor, una que olvido por que
# denegaba. Se copia aparte aunque el directorio caiga dentro del volumen
# del credential-store: duplicar unos kilobytes es mas barato que perder
# los topes si ese volumen se omite.
CAPS_STATE_DIR="${ADS_BROKER_CAPS_STATE_DIR:-$(env_value secrets/broker.env ADS_BROKER_CAPS_STATE_DIR)}"
if [ "${ADS_BACKUP_SKIP_CAPS_STATE:-0}" = "1" ] || [ -z "$CAPS_STATE_DIR" ]; then
  echo "==> topes del panel: omitido (ADS_BROKER_CAPS_STATE_DIR sin declarar o ADS_BACKUP_SKIP_CAPS_STATE=1)"
elif ! "${ADS_BROKER_EXEC[@]}" true 2>/dev/null; then
  # "El broker no contesta" y "todavia no hay topes del panel" se parecian
  # demasiado: ambos eran un `test -d` que falla. El primero es una copia
  # incompleta y tiene que doler; para omitirlo hay que decirlo.
  echo "ERROR: ads-broker no responde: no se pudo copiar ${CAPS_STATE_DIR}." >&2
  echo "       Levantalo, o repite con ADS_BACKUP_SKIP_CAPS_STATE=1 si lo omites a proposito." >&2
  exit 1
elif ! "${ADS_BROKER_EXEC[@]}" test -d "$CAPS_STATE_DIR"; then
  echo "==> topes del panel: ${CAPS_STATE_DIR} todavia no existe (ningun tope fijado desde el panel)"
else
  echo "==> topes del panel -> ${OUT_PREFIX}-caps-state.tar.gz"
  "${ADS_BROKER_EXEC[@]}" tar czf - -C "$CAPS_STATE_DIR" . \
    > "${OUT_PREFIX}-caps-state.tar.gz"
fi

echo "==> manifiesto (solo NOMBRES de secretos, nunca valores) -> ${OUT_PREFIX}-manifest.txt"
{
  echo "# ads-backup manifest -- ${STAMP}"
  echo "# T125: solo NOMBRES de variables de secrets/*.env; ningun valor."
  echo "# Restaurar en otro host exige rellenar estas variables a mano"
  echo "# (o desde el gestor de secretos), nunca copiarlas de aqui."
  for f in secrets/api.env secrets/broker.env; do
    if [ -f "$f" ]; then
      echo "## ${f}"
      grep -E '^[A-Z][A-Z0-9_]*=' "$f" | cut -d= -f1
    fi
  done
} > "${OUT_PREFIX}-manifest.txt"

echo "backup completo: ${OUT_PREFIX}.dump (+ manifest y, si aplica, credential-store, caps.yaml y topes del panel)"
