.PHONY: sync lint type test test-integration build first-run up down migrate check-secrets backup restore

sync:
	uv sync --dev

lint:
	uv run ruff check .

type:
	uv run mypy

test:
	# --cov activa `fail_under` de pyproject (T121): una caida de cobertura falla en local y en CI (mismo target).
	uv run pytest -m "not integration" --cov=safent_ads --cov-report=term-missing:skip-covered

test-integration:
	uv run pytest -m integration

build:
	docker build -f Containerfile -t safent-ads:local .

# 008 T019: primer arranque de un comando (contracts/first-run-cli.md).
# `scripts/primer-arranque.sh` es la unica fuente de verdad -- este target
# solo lo invoca, igual que `backup` con `ops/backup.sh`. Argumentos
# opcionales: `make first-run ARGS="--dry-run"`.
first-run:
	./scripts/primer-arranque.sh $(ARGS)

# T066, threat-model.md C-24: antes de `make up`, comprueba que los
# secretos reales existen, tienen permisos 0600 y no quedan marcadores de
# plantilla sin rellenar. `.env` entra en la misma lista (revisión de
# seguridad PR 45): lleva `POSTGRES_PASSWORD` y el DSN con esa contraseña
# dentro (ver el `_DOTENV_HEADER` de `safent_ads.tools.first_run`), y el
# camino manual (`cp .env.example .env`) lo dejaba en el 0644 por omisión
# del sistema de ficheros con un `change-me` sin tocar. `config/caps.yaml`
# no es un secreto pero recibe el mismo trato de "nada por defecto": debe
# existir y no ser escribible por grupo/otros. No hace `chown` por si
# mismo -- requeriria privilegios que este target no debe asumir; solo
# avisa si el propietario no es root.
#
# El marcador se busca SOLO en lineas `CLAVE=valor` sin comentar, nunca en
# comentarios: la cabecera de `secrets/*.env.example` nombra el propio
# marcador para explicar que hace este target (T049, walkthrough en
# limpio), y esa cabecera viaja tal cual a `secrets/*.env` al copiarla --
# con un grep sin filtrar, ese fichero NUNCA pasaba, ni con todo relleno.
#
# `stat -c` es de GNU coreutils -- en BSD/macOS no existe esa opcion y
# sale con error, que el `|| stat -f ...` de abajo recoge (revision de
# seguridad PR 45): `%a`/`%Lp` son permisos en octal, `%U`/`%Su` el
# propietario, en cada dialecto.
check-secrets:
	@status=0; \
	for f in secrets/broker.env secrets/api.env .env; do \
		if [ ! -f "$$f" ]; then \
			echo "FALTA $$f (copiar desde $$f.example y rellenar)" >&2; \
			status=1; \
			continue; \
		fi; \
		perm="$$(stat -c '%a' "$$f" 2>/dev/null || stat -f '%Lp' "$$f")"; \
		if [ "$$perm" != "600" ]; then \
			echo "$$f tiene permisos $$perm, deben ser 600 -> chmod 600 $$f" >&2; \
			status=1; \
		fi; \
		if grep -v '^[[:space:]]*#' "$$f" | grep -q 'change-me'; then \
			echo "$$f todavia tiene placeholders 'change-me' sin rellenar" >&2; \
			status=1; \
		fi; \
	done; \
	if [ ! -f config/caps.yaml ]; then \
		echo "FALTA config/caps.yaml (copiar desde config/caps.example.yaml y rellenar por cuenta)" >&2; \
		status=1; \
	else \
		if find config/caps.yaml -maxdepth 0 \( -perm -020 -o -perm -002 \) | grep -q .; then \
			echo "config/caps.yaml es escribible por grupo/otros -> chmod 644 config/caps.yaml" >&2; \
			status=1; \
		fi; \
		owner="$$(stat -c '%U' config/caps.yaml 2>/dev/null || stat -f '%Su' config/caps.yaml)"; \
		if [ "$$owner" != "root" ]; then \
			echo "AVISO: config/caps.yaml pertenece a $$owner, no root (chown root:root config/caps.yaml en despliegue real)" >&2; \
		fi; \
		if grep -q 'example_platform_account_id' config/caps.yaml; then \
			echo "config/caps.yaml todavia tiene la cuenta de ejemplo sin sustituir" >&2; \
			status=1; \
		fi; \
	fi; \
	if [ "$$status" -eq 0 ]; then echo "check-secrets: OK"; fi; \
	exit $$status

# Con `ADS_IMAGE` puesta (entorno o `.env`, la escribe `make first-run`):
# SIEMPRE se descarga, nunca se compila encima. Sin este paso, `docker
# compose up` con `build:` presente y la imagen ausente en local intenta
# tirar y, si falla (por ejemplo la referencia no existe todavia en el
# registro), CONSTRUYE de fuente en silencio y etiqueta ese build local
# con el nombre de la referencia firmada -- sustituye la imagen verificada
# por una que no lo es, sin avisar (revision de PR 45, T049). `--no-build`
# en el `up` final hace que, si el pull de arriba no dejo la imagen en
# local por lo que sea, esto falle alto y claro en vez de compilar.
up:
	@ads_image="$${ADS_IMAGE:-}"; \
	if [ -z "$$ads_image" ] && [ -f .env ]; then \
		ads_image="$$(sed -n 's/^ADS_IMAGE=//p' .env | tail -1)"; \
	fi; \
	if [ -n "$$ads_image" ]; then \
		echo "→ Imagen publicada ($$ads_image): descargando (nunca se compila encima)…"; \
		docker compose pull ads-migrate ads-api ads-worker ads-broker \
			|| { echo "ERROR: no se pudo descargar $$ads_image; verifica ADS_IMAGE" >&2; exit 1; }; \
		docker compose up -d ads-db; \
		docker compose run --rm ads-migrate; \
		docker compose up -d --no-build ads-broker ads-api ads-worker; \
	else \
		docker compose up -d ads-db; \
		docker compose run --rm ads-migrate; \
		docker compose up -d ads-broker ads-api ads-worker; \
	fi

down:
	docker compose down

migrate:
	docker compose run --rm ads-migrate

# T125: `ops/backup.sh`/`ops/restore.sh` son la unica fuente de verdad --
# estos targets solo los invocan, sin duplicar el `pg_dump`/`pg_restore` de
# dentro del contenedor. Dump/restore de la BD, mas el volumen del almacen
# cifrado de credenciales, `config/caps.yaml` y un manifiesto con los
# NOMBRES (nunca valores) de `secrets/*.env` -- ver runbook.md §7.
backup:
	./ops/backup.sh

# Uso: make restore FILE=backups/ads-backup-20260909T120000Z.dump [FORCE=1]
# `--force` (FORCE=1) es obligatorio si la base ya tiene tablas: sin el,
# `ops/restore.sh` se niega a pisarla. Con `--force` vacia 'public' antes
# de restaurar (T125: `pg_restore --clean` falla con `metrics_daily`,
# tabla particionada -- ver el docstring de ops/restore.sh).
restore:
	@test -n "$(FILE)" || { echo "uso: make restore FILE=backups/ads-backup-....dump [FORCE=1]" >&2; exit 1; }
	./ops/restore.sh --file "$(FILE)" $(if $(FORCE),--force,)
