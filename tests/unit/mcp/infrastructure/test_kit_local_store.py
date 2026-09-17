"""`LocalKitStore` (encargo del dueno, 14-sep): traversal/IDOR, ocultos,
symlinks, listas blancas de extension, tope de tamano y URL firmada con
TTL -- mismo criterio que `tests/unit/creative/infrastructure/
test_local_asset_storage.py`, raiz e implementacion propias."""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime
from pathlib import Path

import pytest

from safent_ads.mcp.application.kit_port import (
    KitFileTooLargeError,
    KitFileTypeNotAllowedError,
    KitPathRejectedError,
)
from safent_ads.mcp.infrastructure.local_kit_store import LocalKitStore
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.errors import InfrastructureError

_SIGNING_KEY = b"k" * 32
_FIXED_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _store(root: Path, *, clock: FixedClock | None = None) -> LocalKitStore:
    return LocalKitStore(root, signing_key=_SIGNING_KEY, clock=clock or FixedClock(_FIXED_NOW))


def _seed_kit(root: Path) -> None:
    (root / "01_MARCA").mkdir()
    (root / "01_MARCA" / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 20)
    (root / "AGENTS.md").write_text("guia del kit")
    (root / "README.md").write_text("readme")


def test_constructor_rejects_a_missing_kit_dir(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    with pytest.raises(InfrastructureError):
        LocalKitStore(missing, signing_key=_SIGNING_KEY, clock=FixedClock(_FIXED_NOW))


async def test_list_files_is_recursive_and_sorted(tmp_path: Path) -> None:
    _seed_kit(tmp_path)
    store = _store(tmp_path)

    entries = await store.list_files("", query=None, max_results=500)

    assert [e.path for e in entries] == ["01_MARCA/logo.png", "AGENTS.md", "README.md"]


async def test_list_files_query_filters_by_substring_case_insensitively(tmp_path: Path) -> None:
    _seed_kit(tmp_path)
    store = _store(tmp_path)

    entries = await store.list_files("", query="marca", max_results=500)

    assert [e.path for e in entries] == ["01_MARCA/logo.png"]


async def test_list_files_respects_max_results(tmp_path: Path) -> None:
    _seed_kit(tmp_path)
    store = _store(tmp_path)

    entries = await store.list_files("", query=None, max_results=1)

    assert len(entries) == 1


async def test_list_files_scoped_to_a_subdirectory(tmp_path: Path) -> None:
    _seed_kit(tmp_path)
    store = _store(tmp_path)

    entries = await store.list_files("01_MARCA", query=None, max_results=500)

    assert [e.path for e in entries] == ["01_MARCA/logo.png"]


async def test_read_text_returns_utf8_content(tmp_path: Path) -> None:
    _seed_kit(tmp_path)
    store = _store(tmp_path)

    content = await store.read_text("AGENTS.md", max_bytes=1000)

    assert content == "guia del kit"


async def test_read_text_rejects_a_disallowed_extension(tmp_path: Path) -> None:
    _seed_kit(tmp_path)
    store = _store(tmp_path)

    with pytest.raises(KitFileTypeNotAllowedError):
        await store.read_text("01_MARCA/logo.png", max_bytes=1000)


async def test_read_text_rejects_content_over_max_bytes(tmp_path: Path) -> None:
    (tmp_path / "big.md").write_text("x" * 1000)
    store = _store(tmp_path)

    with pytest.raises(KitFileTooLargeError):
        await store.read_text("big.md", max_bytes=10)


async def test_read_text_rejects_a_nonexistent_path(tmp_path: Path) -> None:
    _seed_kit(tmp_path)
    store = _store(tmp_path)

    with pytest.raises(KitPathRejectedError):
        await store.read_text("01_MARCA/does-not-exist.md", max_bytes=1000)


# --- traversal / IDOR -----------------------------------------------------


async def test_read_text_rejects_dotdot_traversal(tmp_path: Path) -> None:
    _seed_kit(tmp_path)
    store = _store(tmp_path)

    with pytest.raises(KitPathRejectedError):
        await store.read_text("../outside.md", max_bytes=1000)


async def test_read_text_rejects_an_absolute_path_without_leaking_the_real_file(
    tmp_path: Path,
) -> None:
    _seed_kit(tmp_path)
    store = _store(tmp_path)

    with pytest.raises(KitPathRejectedError):
        await store.read_text("/etc/passwd", max_bytes=1000)


async def test_read_text_rejects_a_url_encoded_traversal_lookalike_without_decoding_it(
    tmp_path: Path,
) -> None:
    """`%2e%2e%2f` nunca se decodifica aqui: llega como texto literal, no
    como separador -- nunca escapa la raiz, solo no encuentra el fichero."""
    _seed_kit(tmp_path)
    store = _store(tmp_path)

    with pytest.raises(KitPathRejectedError):
        await store.read_text("%2e%2e%2fescape.md", max_bytes=1000)


async def test_symlinked_directory_escaping_the_root_is_neither_listed_nor_readable(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    outside = tmp_path_factory.mktemp("kit-outside-secret")
    (outside / "secret.md").write_text("fuera del kit")
    _seed_kit(tmp_path)
    (tmp_path / "escape").symlink_to(outside)
    store = _store(tmp_path)

    entries = await store.list_files("", query=None, max_results=500)
    assert "escape/secret.md" not in {e.path for e in entries}
    with pytest.raises(KitPathRejectedError):
        await store.read_text("escape/secret.md", max_bytes=1000)


async def test_symlinked_file_inside_the_root_is_neither_listed_nor_readable(
    tmp_path: Path,
) -> None:
    _seed_kit(tmp_path)
    (tmp_path / "alias.md").symlink_to(tmp_path / "AGENTS.md")
    store = _store(tmp_path)

    entries = await store.list_files("", query=None, max_results=500)
    assert "alias.md" not in {e.path for e in entries}
    with pytest.raises(KitPathRejectedError):
        await store.read_text("alias.md", max_bytes=1000)


# --- ocultos ---------------------------------------------------------------


async def test_hidden_files_are_excluded_from_listing(tmp_path: Path) -> None:
    _seed_kit(tmp_path)
    (tmp_path / ".DS_Store").write_text("basura del Finder")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("no es del kit")
    store = _store(tmp_path)

    entries = await store.list_files("", query=None, max_results=500)

    paths = {e.path for e in entries}
    assert ".DS_Store" not in paths
    assert ".git/config" not in paths


async def test_hidden_files_are_rejected_even_when_asked_for_directly(tmp_path: Path) -> None:
    _seed_kit(tmp_path)
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("no es del kit")
    store = _store(tmp_path)

    with pytest.raises(KitPathRejectedError):
        await store.read_text(".git/config", max_bytes=1000)


async def test_dotagents_product_marketing_is_listed_and_readable(tmp_path: Path) -> None:
    """Unica excepcion al bloqueo de ocultos (anadido del dueno):
    `.agents/product-marketing.md`."""
    _seed_kit(tmp_path)
    (tmp_path / ".agents").mkdir()
    (tmp_path / ".agents" / "product-marketing.md").write_text("guia de producto")
    store = _store(tmp_path)

    entries = await store.list_files("", query=None, max_results=500)
    assert ".agents/product-marketing.md" in {e.path for e in entries}

    content = await store.read_text(".agents/product-marketing.md", max_bytes=1000)
    assert content == "guia de producto"


async def test_no_other_file_inside_dotagents_is_exposed(tmp_path: Path) -> None:
    """La excepcion es UNICAMENTE `.agents/product-marketing.md`: cualquier
    otro fichero en `.agents/` sigue oculto, listado o leido directamente."""
    _seed_kit(tmp_path)
    (tmp_path / ".agents").mkdir()
    (tmp_path / ".agents" / "product-marketing.md").write_text("guia de producto")
    (tmp_path / ".agents" / "internal-notes.md").write_text("no deberia verse")
    store = _store(tmp_path)

    entries = await store.list_files("", query=None, max_results=500)
    paths = {e.path for e in entries}
    assert ".agents/internal-notes.md" not in paths
    assert ".agents/product-marketing.md" in paths

    with pytest.raises(KitPathRejectedError):
        await store.read_text(".agents/internal-notes.md", max_bytes=1000)


# --- URL firmada -------------------------------------------------------


async def test_signed_preview_url_carries_hmac_and_expiry(tmp_path: Path) -> None:
    _seed_kit(tmp_path)
    store = _store(tmp_path)

    url = await store.signed_preview_url("01_MARCA/logo.png", ttl_s=600)

    assert url.startswith("/api/v1/kit-previews/01_MARCA/logo.png")
    assert "exp=" in url
    assert "sig=" in url


async def test_signed_preview_url_rejects_a_disallowed_extension(tmp_path: Path) -> None:
    _seed_kit(tmp_path)
    store = _store(tmp_path)

    with pytest.raises(KitFileTypeNotAllowedError):
        await store.signed_preview_url("AGENTS.md", ttl_s=600)


async def test_open_preview_returns_bytes_for_a_valid_unexpired_signature(tmp_path: Path) -> None:
    _seed_kit(tmp_path)
    store = _store(tmp_path)
    url = await store.signed_preview_url("01_MARCA/logo.png", ttl_s=600)
    key, exp, sig = _parse_preview_url(url)

    payload = await store.open_preview(key, exp, sig)

    assert payload == (tmp_path / "01_MARCA" / "logo.png").read_bytes()


async def test_open_preview_rejects_a_tampered_signature(tmp_path: Path) -> None:
    _seed_kit(tmp_path)
    store = _store(tmp_path)
    url = await store.signed_preview_url("01_MARCA/logo.png", ttl_s=600)
    key, exp, _sig = _parse_preview_url(url)

    with pytest.raises(KitPathRejectedError):
        await store.open_preview(key, exp, "0" * 64)


async def test_open_preview_rejects_an_expired_link(tmp_path: Path) -> None:
    _seed_kit(tmp_path)
    clock = FixedClock(_FIXED_NOW)
    store = _store(tmp_path, clock=clock)
    url = await store.signed_preview_url("01_MARCA/logo.png", ttl_s=600)
    key, exp, sig = _parse_preview_url(url)
    clock.advance_to(datetime(2026, 1, 1, 0, 20, tzinfo=UTC))

    with pytest.raises(KitPathRejectedError):
        await store.open_preview(key, exp, sig)


async def test_open_preview_rejects_a_traversal_key_even_with_a_matching_signature(
    tmp_path: Path,
) -> None:
    _seed_kit(tmp_path)
    store = _store(tmp_path)
    expires_at = int(_FIXED_NOW.timestamp()) + 600
    traversal_key = "../outside-the-kit.png"
    signature = _sign(traversal_key, expires_at)

    with pytest.raises(KitPathRejectedError):
        await store.open_preview(traversal_key, expires_at, signature)


def _parse_preview_url(url: str) -> tuple[str, int, str]:
    path_and_query = url.removeprefix("/api/v1/kit-previews/")
    key, query = path_and_query.split("?", 1)
    params = dict(pair.split("=", 1) for pair in query.split("&"))
    return key, int(params["exp"]), params["sig"]


def _sign(key: str, expires_at: int) -> str:
    return hmac.new(_SIGNING_KEY, f"{key}:{expires_at}".encode(), hashlib.sha256).hexdigest()
