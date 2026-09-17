"""Exposicion de `/metrics` (T127): un `start_http_server` de
`prometheus_client` por proceso, atado SIEMPRE a `127.0.0.1` -- nunca
`0.0.0.0`, nunca publicado en `compose.yaml` (`ports:`) ni anadido a
`compose.companion.yaml`.

Decision de diseno -- por que un servidor propio en loopback por proceso,
y no `multiprocess` mode: `ads-api`/`ads-worker`/`ads-broker` son TRES
PROCESOS de Python distintos (`compose.yaml`), no varios workers de la
MISMA app (el caso que `multiprocess` mode resuelve, p. ej. varios
workers de gunicorn detras de un solo `/metrics`). Cada proceso ya tiene
su propio registro en memoria, correcto sin agregar nada: `multiprocess`
mode exigiria un directorio compartido de ficheros `.db` y una variable
`PROMETHEUS_MULTIPROC_DIR` que aqui no aporta nada, solo complejidad.

Decision de diseno -- por que loopback y no una ruta `/metrics` en la app
FastAPI de `ads-api`: en modo companion (`ADS_COMPANION_MODE=true`,
`compose.companion.yaml`) esa app escucha en `10.201.0.10:8443`, la IP
fija de la red `safent-companions` que comparte con el runtime de Safent
-- montar `/metrics` ahi la haria alcanzable desde esa red, sin
autenticacion (Prometheus no manda bearer). Un servidor HTTP en claro
COMPLETAMENTE APARTE, en el mismo proceso pero en `127.0.0.1`, nunca
sale de la interfaz de loopback del propio contenedor: ni `ads-net` ni
`safent-companions` lo alcanzan en ningun modo. El coste es que un
Prometheus externo no puede hacer scrape remoto sin un sidecar que
comparta el netns del contenedor (`podman exec .../curl 127.0.0.1:PUERTO/
metrics`, documentado en runbook.md SS7) -- aceptado a proposito: "simplest
acceptable" (encargo T127) prioriza cero superficie nueva sobre
comodidad de scrape remoto."""

from __future__ import annotations

from threading import Thread
from wsgiref.simple_server import WSGIServer

from prometheus_client import start_http_server

from safent_ads.observability.metrics import REGISTRY

LOOPBACK_ADDR = "127.0.0.1"


def start_metrics_server(port: int) -> tuple[WSGIServer, Thread]:
    """`port=0` deja que el sistema operativo elija un puerto libre (uso en
    tests); el puerto real queda en `server.server_address[1]`."""
    return start_http_server(port, addr=LOOPBACK_ADDR, registry=REGISTRY)


__all__ = ["LOOPBACK_ADDR", "start_metrics_server"]
