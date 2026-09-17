# Contribuir

Gracias por pasarte. Antes de nada, lo que evita malentendidos: este proyecto se
publica **sin soporte ni SLA**. Nadie está de guardia, no hay tiempos de
respuesta comprometidos y un issue puede quedarse abierto. El código es tuyo
para usarlo, forkearlo y arreglarlo.

## Entorno

```bash
make sync                        # uv sync --dev
```

Para levantar una instancia completa (Docker, base de datos, panel), sigue
«Primer arranque» del `README.md`.

## Antes de abrir un PR

Todo esto tiene que pasar en tu máquina. Son los mismos comandos que corre CI.

```bash
make lint                        # ruff
make type                        # mypy
make test                        # pytest (unidad, con umbral de cobertura)
make test-integration            # pytest (integración, necesita Docker)
uv run pytest tests/unit/test_no_client_strings.py
```

Y el panel, si lo tocas:

```bash
cd panel && npm ci --ignore-scripts
npm run lint && npm run typecheck && npm run test && npm run build
```

La guarda de cadenas de cliente (`tests/unit/test_no_client_strings.py`) ya va
dentro de `make test` y también se puede lanzar sobre un árbol cualquiera:
`CLIENT_STRINGS_ROOT=<dir> uv run python tests/unit/test_no_client_strings.py`.
Este es un producto estándar: ningún nombre de un despliegue concreto —marca,
dominio, proyecto de nube— entra en el código, ni siquiera en un comentario.

## Reglas del código

- Un cambio no trivial empieza por un issue que acuerde el QUÉ, no por un PR suelto. El diseño vigente es `ARCHITECTURE.md`: si tu cambio lo contradice, el PR actualiza también esa página.
- El dominio no depende de framework, base de datos ni HTTP.
- Ningún secreto en argv, logs, mensajes de error ni capturas.
- Fail-closed: si falta un control, se deniega.
- Todo arreglo trae su test de regresión.

## DCO

No hay CLA. Usamos el [Developer Certificate of Origin](https://developercertificate.org/):
firma cada commit con

```bash
git commit -s
```

que añade tu línea `Signed-off-by: Nombre <correo>`. Un PR sin firmar no se
mergea.

## Seguridad

Los fallos de seguridad no van en un issue público. Ver `SECURITY.md`.

## Licencia

Al contribuir aceptas que tu aportación se publica bajo Apache-2.0 (`LICENSE`).
