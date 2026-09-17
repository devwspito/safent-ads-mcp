"""`_server_binding` (composition/app.py): companion mode manda TLS en
`0.0.0.0:8443`; por defecto sigue en `127.0.0.1:8410` en claro, exactamente
como hoy (companion preinstalado; su diseño vive en el runtime)."""

from __future__ import annotations

from pathlib import Path

from safent_ads.composition.app import _server_binding
from tests.unit.composition.factories import build_api_settings


def test_plaintext_binding_when_companion_mode_is_off() -> None:
    binding = _server_binding(build_api_settings())

    assert binding.host == "127.0.0.1"
    assert binding.port == 8410
    assert binding.ssl_certfile is None
    assert binding.ssl_keyfile is None


def test_tls_binding_when_companion_mode_is_on(tmp_path: Path) -> None:
    certfile = tmp_path / "leaf.crt"
    certfile.write_text("cert")
    keyfile = tmp_path / "leaf.key"
    keyfile.write_text("key")

    binding = _server_binding(
        build_api_settings(companion_mode=True, tls_certfile=certfile, tls_keyfile=keyfile)
    )

    assert binding.host == "0.0.0.0"  # noqa: S104 - valor esperado, no un bind real
    assert binding.port == 8443
    assert binding.ssl_certfile == str(certfile)
    assert binding.ssl_keyfile == str(keyfile)
