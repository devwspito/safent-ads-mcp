#!/usr/bin/env bash
# ops/restore.sh — T125. Sustituye la logica del target `restore` del
# Makefile (una sola fuente de verdad; `Makefile:restore` solo llama aqui):
#
#   ops/restore.sh --file backups/ads-backup-<UTC>.dump [--force]
#
# Idempotente: sobre una base vacia (o recien vaciada por `--force`,
# abajo) `pg_restore` reconstruye SIEMPRE el mismo esquema+datos a partir
# del mismo `.dump`, sin importar cuantas veces se relance.
#
# Rechaza por defecto restaurar sobre una base que YA tiene tablas
# (`information_schema`/`pg_catalog` no cuentan): sin `--force` es
# facilisimo pisar sin querer una base con datos reales. El unico camino
# para saltarselo es pedirlo explicitamente.
#
# Por que `DROP SCHEMA public CASCADE` + `pg_restore` SIN `--clean`, en vez
# de `pg_restore --clean --if-exists` (lo que hacia `Makefile:restore`
# antes de T125): probado de verdad (T125 "real proof") contra el esquema
# real -- `metrics_daily` (0004) es una tabla PARTICIONADA, y el
# `DROP CONSTRAINT` que `pg_dump --clean` genera para la PK de cada
# particion hija falla siempre ("cannot drop inherited constraint...",
# limitacion conocida de `pg_dump`/`pg_restore` con particionado
# declarativo: esa PK pertenece al indice particionado del padre, no se
# puede soltar suelta). `pg_restore` sigue adelante e ignora el error, pero
# devuelve un exit code distinto de cero pese a que los datos SI quedan
# bien restaurados -- un falso negativo peor que el problema que evita.
# Vaciar el esquema entero ANTES de restaurar rodea el problema de raiz:
# no hay nada que "limpiar" durante la restauracion, todo es `CREATE`.
#
# Variables de entorno (mismo criterio que `ops/backup.sh`):
#   ADS_BACKUP_DB_EXEC -- por defecto `docker compose exec -T ads-db`.

set -euo pipefail

usage() {
  echo "uso: ops/restore.sh --file backups/ads-backup-<UTC>.dump [--force]" >&2
  exit 1
}

FILE=""
FORCE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --file)
      FILE="${2:-}"
      shift 2
      ;;
    --force)
      FORCE=1
      shift
      ;;
    *)
      usage
      ;;
  esac
done

[ -n "$FILE" ] || usage
[ -f "$FILE" ] || { echo "no existe: $FILE" >&2; exit 1; }

read -ra ADS_DB_EXEC <<< "${ADS_BACKUP_DB_EXEC:-docker compose exec -T ads-db}"

echo "==> comprobando si la base ya tiene tablas en 'public'"
# La consulta viaja por stdin (`psql -tA` sin `-c`), no como argumento del
# `sh -c` remoto: evita anidar comillas simples dentro de comillas simples
# (`$POSTGRES_USER`/`$POSTGRES_DB` SI deben quedar sin expandir aqui --
# los resuelve el shell de DENTRO del contenedor, con su propio entorno).
existing_relations="$(
  printf "SELECT count(*) FROM pg_catalog.pg_tables WHERE schemaname = 'public';\n" \
    | "${ADS_DB_EXEC[@]}" sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tA'
)"
existing_relations="$(printf '%s' "$existing_relations" | tr -d '[:space:]')"

if [ "$existing_relations" != "0" ]; then
  if [ "$FORCE" -ne 1 ]; then
    echo "la base ya tiene ${existing_relations} tabla(s) en 'public' -- usa --force" \
      "para sobrescribir" >&2
    exit 1
  fi
  echo "==> --force: vaciando 'public' antes de restaurar"
  printf 'DROP SCHEMA public CASCADE;\nCREATE SCHEMA public;\n' \
    | "${ADS_DB_EXEC[@]}" sh -c 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
fi

echo "==> pg_restore <- ${FILE}"
"${ADS_DB_EXEC[@]}" sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < "$FILE"

echo "restauracion completa"
