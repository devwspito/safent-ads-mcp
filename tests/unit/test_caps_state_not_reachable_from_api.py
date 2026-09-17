"""i12 (revision T035 de la spec 008): el estado de topes del panel vive en
un volumen que **solo** `ads-broker` monta, y ningun otro servicio sabe
siquiera donde esta.

Es la mitad de despliegue del teorema. Toda la politica del bróker se apoya
en que `ads-api` no pueda tocar el fichero de estado: si un dia alguien le
monta `credential-store` "para depurar", o le pasa
`ADS_BROKER_CAPS_STATE_DIR`, el control desaparece sin que ningun test de
Python se entere -- el codigo seguiria siendo correcto y el despliegue ya
no. Por eso la guarda mira los ficheros de compose, que es donde se decide.

Se comprueban todos los que lleve este repo: el de desarrollo, en la raiz,
y el de cada despliegue bajo `deploy/`. El NOMBRE es exacto y esta escrito
aqui -- un compose que se llame de otra forma hay que anadirlo a mano, a
proposito, que es justo el dia en que un comodin fallaria --; lo que se
descubre es donde esta, porque los directorios de despliegue son del repo
privado y un export los borra (T042): nombrar uno dejaria una referencia
colgante en un fichero que si viaja."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]

_COMPOSE_NAMES = ("compose.yaml",)
_DEPLOYMENTS_DIR = ROOT / "deploy"


def _compose_files() -> tuple[str, ...]:
    en_la_raiz = [name for name in _COMPOSE_NAMES if (ROOT / name).is_file()]
    de_cada_despliegue = sorted(
        path.relative_to(ROOT).as_posix()
        for name in _COMPOSE_NAMES
        for path in _DEPLOYMENTS_DIR.rglob(name)
    )
    return tuple(en_la_raiz + de_cada_despliegue)


COMPOSE_FILES = _compose_files()

# El unico servicio que puede ver el estado de topes, y el volumen donde
# vive (`/var/lib/ads-broker/caps-state`, dentro de `credential-store`).
_BROKER_SERVICE = "ads-broker"
_BROKER_ONLY_VOLUME = "credential-store"

# Donde esta el directorio de estado. Fuera de `ads-broker` no es un secreto
# filtrado, es peor: es la pista de que alguien le ha dado acceso.
_STATE_DIR_VARIABLE = "ADS_BROKER_CAPS_STATE_DIR"

# El fichero de entorno del broker, en las dos rutas que usan los dos
# composes. `ads-api`/`ads-worker` leen el suyo y solo el suyo (C-24).
_BROKER_ENV_FILES = frozenset({"secrets/broker.env", "/etc/safent-ads/broker.env"})
_APP_SERVICES = ("ads-api", "ads-worker")


def _compose(relative: str) -> dict[str, Any]:
    document: dict[str, Any] = yaml.safe_load((ROOT / relative).read_text())
    return document


def _services(document: dict[str, Any]) -> dict[str, Any]:
    services: dict[str, Any] = document.get("services", {})
    return services


def _volume_source(entry: object) -> str | None:
    """Las dos formas que admite compose: la corta (`origen:destino[:ro]`) y
    la larga (`{type: volume, source: ...}`). Un `type: bind` no es este
    volumen y no cuenta."""
    if isinstance(entry, str):
        return entry.split(":")[0]
    if isinstance(entry, dict) and entry.get("type") == "volume":
        source = entry.get("source")
        return source if isinstance(source, str) else None
    return None


def _offenders(document: dict[str, Any]) -> set[str]:
    """Servicios que NO son el bróker y montan su volumen de credenciales."""
    return {
        name
        for name, service in _services(document).items()
        if name != _BROKER_SERVICE
        and any(
            _volume_source(entry) == _BROKER_ONLY_VOLUME
            for entry in service.get("volumes", []) or []
        )
    }


def _environment_names(service: dict[str, Any]) -> set[str]:
    """`environment:` acepta mapa (`VAR: valor`) y lista (`VAR=valor`)."""
    environment = service.get("environment") or {}
    if isinstance(environment, dict):
        return set(environment)
    return {str(item).split("=", 1)[0] for item in environment}


def _env_file_paths(service: dict[str, Any]) -> set[str]:
    """`env_file:` acepta una cadena, una lista de cadenas y una lista de
    `{path: ..., required: ...}`."""
    declared = service.get("env_file") or []
    if isinstance(declared, str):
        declared = [declared]
    return {entry if isinstance(entry, str) else str(entry.get("path")) for entry in declared}


def test_the_guard_watches_the_development_compose() -> None:
    """Sin esta prueba, una lista vacia no colecciona nada y la guarda
    entera pasaria sin mirar un solo fichero."""
    assert "compose.yaml" in COMPOSE_FILES


def test_the_guard_watches_every_deployment_compose() -> None:
    """La contraparte, y la que de verdad importa: `compose.yaml` solo ya
    satisfaria "hay algo que vigilar" mientras el compose de un despliegue
    se renombra o se muda y sale de la lista en silencio. Si hay
    despliegues en este repo, alguno tiene que estar vigilado."""
    if not _DEPLOYMENTS_DIR.is_dir():
        pytest.skip("este arbol no lleva despliegues (el publico, p.ej.)")

    assert any(relative.startswith("deploy/") for relative in COMPOSE_FILES), (
        f"hay {_DEPLOYMENTS_DIR.name}/ pero ningun compose suyo vigilado: "
        f"o se renombro, o se mudo -- anadelo a _COMPOSE_NAMES a mano"
    )


@pytest.mark.parametrize("relative", COMPOSE_FILES)
def test_no_service_but_the_broker_mounts_the_state_volume(relative: str) -> None:
    document = _compose(relative)

    # Control positivo: si el volumen se renombrara, la comprobacion de
    # abajo pasaria sin mirar nada. Aqui se afirma que sigue existiendo
    # donde debe.
    broker_volumes = {
        _volume_source(entry) for entry in _services(document)[_BROKER_SERVICE]["volumes"]
    }
    assert _BROKER_ONLY_VOLUME in broker_volumes

    assert _offenders(document) == set()


@pytest.mark.parametrize("relative", COMPOSE_FILES)
def test_the_guard_would_catch_a_state_mount_in_ads_api(relative: str) -> None:
    """La guarda solo vale si falla cuando tiene que fallar: se siembra el
    fallo sobre una COPIA y se comprueba que lo ve."""
    tampered = copy.deepcopy(_compose(relative))
    tampered["services"]["ads-api"].setdefault("volumes", []).append(f"{_BROKER_ONLY_VOLUME}:/x")

    assert _offenders(tampered) == {"ads-api"}


@pytest.mark.parametrize("relative", COMPOSE_FILES)
def test_no_other_service_is_told_where_the_state_directory_is(relative: str) -> None:
    document = _compose(relative)
    services = _services(document)

    # Control positivo: `ads-api` declara entorno de verdad, asi que la
    # comprobacion de abajo mira una lista con contenido, no una vacia.
    assert _environment_names(services["ads-api"])

    exposed = {
        name
        for name, service in services.items()
        if name != _BROKER_SERVICE and _STATE_DIR_VARIABLE in _environment_names(service)
    }

    assert exposed == set()


@pytest.mark.parametrize("relative", COMPOSE_FILES)
@pytest.mark.parametrize("service_name", _APP_SERVICES)
def test_the_app_services_never_read_the_brokers_env_file(relative: str, service_name: str) -> None:
    """`ADS_BROKER_CAPS_STATE_DIR` se declara en `secrets/broker.env`: dar
    ese fichero a `ads-api` seria la otra forma de contarselo."""
    service = _services(_compose(relative))[service_name]
    declared = _env_file_paths(service)

    # Control positivo: lee UN fichero de entorno, el suyo.
    assert declared

    assert declared & _BROKER_ENV_FILES == set()
