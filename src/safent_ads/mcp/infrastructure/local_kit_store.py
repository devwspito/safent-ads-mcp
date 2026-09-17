"""`LocalKitStore` implementa `KitStorePort` (encargo del dueno, 14-sep)
sobre el kit de marketing del negocio montado de solo lectura en disco
(`ADS_KIT_DIR`, ~255 ficheros/150 MB). Mismo criterio de traversal/IDOR que
`creative/infrastructure/local_asset_storage.py` (resolver + confinar a la
raiz, firma HMAC + TTL para binarios), pero SIN importar ese modulo: raiz
de disco y clave de firma propias, nunca compartidas entre almacenes.

Reglas duras (owner's request, con test propio cada una):
- ninguna ruta sale de la raiz del kit (`..`, absolutas, symlinks fuera).
- ningun componente oculto (prefijo `.`) se lista ni se lee, en ningun
  nivel -- sin excepciones (incluye `AGENTS.md`/`README.md`, que SI son
  visibles porque no empiezan por `.`), salvo UNA: `.agents/
  product-marketing.md` (anadido del dueno: "AGENTS.md, README.md,
  .agents/product-marketing.md"). Esa ruta exacta -- y ningun otro fichero
  dentro de `.agents/` -- es visible y legible; cualquier otro punto
  oculto sigue rechazado sin excepcion.
- ningun symlink se sigue, listado o servido -- nunca se resuelve su
  destino, se descarta en el sitio.
- `read_text`/`get_kit_file` solo sirven la extension que declaran
  (listas blancas cerradas), nunca ejecutan nada."""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from safent_ads.mcp.application.kit_port import (
    KitFileEntry,
    KitFileTooLargeError,
    KitFileTypeNotAllowedError,
    KitPathRejectedError,
)
from safent_ads.shared.clock import Clock
from safent_ads.shared.errors import InfrastructureError

TEXT_EXTENSIONS = frozenset(
    {".md", ".txt", ".json", ".csv", ".yaml", ".yml", ".html", ".svg", ".py", ".mjs", ".ts"}
)
BINARY_EXTENSIONS = frozenset(
    {".png", ".jpg", ".jpeg", ".webp", ".pdf", ".zip", ".woff2", ".ttf"}
)

# Unica excepcion a "ningun oculto" (spec.md 004 #25): `.agents/
# product-marketing.md`, nada mas dentro de `.agents/`.
_ALLOWED_HIDDEN_DIR = ".agents"
_ALLOWED_HIDDEN_FILE = "product-marketing.md"
_ALLOWED_HIDDEN_PARTS = ((_ALLOWED_HIDDEN_DIR,), (_ALLOWED_HIDDEN_DIR, _ALLOWED_HIDDEN_FILE))


class LocalKitStore:
    def __init__(self, root_dir: Path, signing_key: bytes, clock: Clock) -> None:
        resolved = root_dir.resolve()
        if not resolved.is_dir():
            raise InfrastructureError(f"ADS_KIT_DIR no es un directorio legible: {resolved}")
        self._root_dir = resolved
        self._signing_key = signing_key
        self._clock = clock

    async def list_files(
        self, path: str, *, query: str | None, max_results: int
    ) -> list[KitFileEntry]:
        base = self._resolve_visible(path)
        needle = query.lower() if query else None
        matches = [
            self._entry_for(candidate)
            for candidate in self._iter_visible_files(base)
            if needle is None or needle in self._relative(candidate).lower()
        ]
        matches.sort(key=lambda entry: entry.path)
        return matches[:max_results]

    async def read_text(self, path: str, *, max_bytes: int) -> str:
        target = self._resolve_readable_file(path, TEXT_EXTENSIONS)
        raw = target.read_bytes()
        self._require_within_size(raw, max_bytes)
        return raw.decode("utf-8")

    async def signed_preview_url(self, path: str, *, ttl_s: int) -> str:
        target = self._resolve_readable_file(path, BINARY_EXTENSIONS)
        relative = self._relative(target)
        expires_at = int(self._clock.now().timestamp()) + ttl_s
        signature = self._sign(relative, expires_at)
        return f"/api/v1/kit-previews/{relative}?exp={expires_at}&sig={signature}"

    async def open_preview(self, key: str, expires_at: int, signature: str) -> bytes:
        self._require_valid_signature(key, expires_at, signature)
        if expires_at < int(self._clock.now().timestamp()):
            raise KitPathRejectedError("enlace caducado")
        target = self._resolve_readable_file(key, BINARY_EXTENSIONS)
        return target.read_bytes()

    def _require_within_size(self, raw: bytes, max_bytes: int) -> None:
        if len(raw) > max_bytes:
            raise KitFileTooLargeError(f"{len(raw)} bytes > {max_bytes}")

    def _require_valid_signature(self, key: str, expires_at: int, signature: str) -> None:
        expected = self._sign(key, expires_at)
        if not hmac.compare_digest(
            expected.encode("ascii"), signature.encode("utf-8", "replace")
        ):
            raise KitPathRejectedError("firma invalida")

    def _resolve_readable_file(self, path: str, allowed_extensions: frozenset[str]) -> Path:
        target = self._resolve_visible(path)
        if not target.is_file():
            raise KitPathRejectedError(f"no es un fichero del kit: {path!r}")
        if target.suffix.lower() not in allowed_extensions:
            raise KitFileTypeNotAllowedError(f"extension no admitida: {target.suffix!r}")
        return target

    def _resolve_visible(self, path: str) -> Path:
        candidate = self._resolve_within_root(path)
        if candidate.is_symlink():
            raise KitPathRejectedError(f"symlink no permitido: {path!r}")
        if not candidate.exists():
            raise KitPathRejectedError(f"ruta inexistente en el kit: {path!r}")
        return candidate

    def _resolve_within_root(self, path: str) -> Path:
        parts = self._safe_parts(path)
        candidate = self._root_dir.joinpath(*parts)
        resolved = candidate.resolve()
        if resolved != self._root_dir and self._root_dir not in resolved.parents:
            raise KitPathRejectedError(f"ruta fuera del kit: {path!r}")
        return candidate

    def _safe_parts(self, path: str) -> tuple[str, ...]:
        # Nunca se decodifica percent-encoding aqui: un "%2e%2e%2f"
        # llega como texto literal (sin separador real), nunca escapa la
        # raiz -- la ruta HTTP (`kit-previews/{key:path}`) ya recibe el
        # `scope["path"]` del servidor ASGI, decodificado una unica vez
        # antes de llegar a esta capa.
        parts = tuple(part for part in path.split("/") if part not in ("", "."))
        if any(part == ".." for part in parts):
            raise KitPathRejectedError(f"ruta fuera del kit: {path!r}")
        if parts not in _ALLOWED_HIDDEN_PARTS and any(part.startswith(".") for part in parts):
            raise KitPathRejectedError(f"ruta oculta no permitida: {path!r}")
        return parts

    def _iter_visible_files(self, base: Path) -> Iterator[Path]:
        if base.is_file():
            yield base
            return
        for directory, subdirs, files in base.walk(follow_symlinks=False):
            subdirs[:] = [name for name in subdirs if self._component_visible(directory, name)]
            for name in files:
                if not self._component_visible(directory, name):
                    continue
                candidate = directory / name
                if candidate.is_symlink():
                    continue
                yield candidate

    def _component_visible(self, directory: Path, name: str) -> bool:
        """Igual regla que `_safe_parts`, aplicada durante el recorrido:
        `.agents/` (unica excepcion) SOLO deja ver `product-marketing.md`
        dentro -- ningun otro nombre de ese directorio, oculto o no."""
        if directory == self._root_dir / _ALLOWED_HIDDEN_DIR:
            return name == _ALLOWED_HIDDEN_FILE
        if name == _ALLOWED_HIDDEN_DIR and directory == self._root_dir:
            return True
        return not name.startswith(".")

    def _relative(self, target: Path) -> str:
        return target.relative_to(self._root_dir).as_posix()

    def _entry_for(self, candidate: Path) -> KitFileEntry:
        stat = candidate.stat()
        return KitFileEntry(
            path=self._relative(candidate),
            size_bytes=stat.st_size,
            extension=candidate.suffix.lower(),
            modified_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
        )

    def _sign(self, key: str, expires_at: int) -> str:
        message = f"{key}:{expires_at}".encode()
        return hmac.new(self._signing_key, message, hashlib.sha256).hexdigest()
