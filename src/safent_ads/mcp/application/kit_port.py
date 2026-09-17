"""Puerto del kit de marketing del negocio (encargo del dueno, 14-sep): una
carpeta de referencia -- marca, mascotas, campanas pasadas, plantillas,
etiquetas, guias, encargos, archivo -- montada de solo lectura por
instalacion via `ADS_KIT_DIR`. Principio del encargo: "el MCP da acceso, el
arnes piensa" -- este puerto solo lee bytes de disco, nunca interpreta ni
resume nada.

Un unico kit por instalacion: ningun metodo recibe `business_id` (el kit no
varia por negocio, `presentation/kit_tools.py` lo exige solo para
autorizacion, mismo criterio que el resto del catalogo).

Errores propios en vez de anadir a `mcp.application.errors` (fuera del
alcance de esta lane): subclasifican `ToolDispatchError` para que
`presentation/mount.py` los traduzca al mismo sobre `{"error": {code,
message}}` que cualquier otra herramienta, sin tocar ese modulo
compartido."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from safent_ads.mcp.application.errors import ToolDispatchError

__all__ = [
    "KitFileEntry",
    "KitFileTooLargeError",
    "KitFileTypeNotAllowedError",
    "KitNotConfiguredError",
    "KitPathRejectedError",
    "KitStorePort",
    "KitTextFile",
]


class KitNotConfiguredError(ToolDispatchError):
    """`ADS_KIT_DIR` no esta fijado en esta instalacion: las tres
    herramientas siguen registradas (visibles a `ver`), pero lo reportan
    limpiamente en vez de fallar en crudo (encargo del dueno: "si unset ->
    tools report «kit no configurado» cleanly")."""

    code = "KIT_NOT_CONFIGURED"


class KitPathRejectedError(ToolDispatchError):
    """`path` sale de la raiz del kit, apunta a un fichero/directorio
    oculto (prefijo `.`), es un symlink, o no existe -- UN unico codigo
    para los cuatro motivos (mismo criterio IDOR-safe que
    `PreviewLinkRejectedError` en `creative/infrastructure/
    local_asset_storage.py`: nunca revelar cual de los motivos aplico)."""

    code = "KIT_PATH_REJECTED"


class KitFileTooLargeError(ToolDispatchError):
    """El fichero de texto pedido supera `max_bytes` (tope duro 262144,
    `presentation/kit_tools.py::GetKitTextArgs`)."""

    code = "KIT_FILE_TOO_LARGE"


class KitFileTypeNotAllowedError(ToolDispatchError):
    """La extension de `path` no esta en la lista blanca de la operacion
    que la pidio (`get_kit_text` exige texto; `get_kit_file` exige
    binario) -- nunca se ejecuta ni interpreta un fichero por su
    extension, solo se compara contra un conjunto cerrado."""

    code = "KIT_FILE_TYPE_NOT_ALLOWED"


@dataclass(frozen=True, slots=True)
class KitFileEntry:
    """Una fila de `list_kit_files`: ruta relativa a la raiz del kit --
    nunca una ruta absoluta del disco del servidor."""

    path: str
    size_bytes: int
    extension: str
    modified_at: datetime


@dataclass(frozen=True, slots=True)
class KitTextFile:
    """Resultado de `get_kit_text`: el contenido decodificado mas su
    tamano real, para que el arnes sepa si tuvo que pedir mas `max_bytes`."""

    path: str
    content: str
    size_bytes: int


class KitStorePort(Protocol):
    """Puro contrato: sin I/O aqui. `mcp/infrastructure/local_kit_store.py`
    es la unica implementacion (disco local de solo lectura, mismo
    criterio de traversal/IDOR que `LocalAssetStorage`, sin compartir
    estado ni clave de firma con ese almacen -- raiz de disco distinta)."""

    async def list_files(
        self, path: str, *, query: str | None, max_results: int
    ) -> list[KitFileEntry]: ...

    async def read_text(self, path: str, *, max_bytes: int) -> str: ...

    async def signed_preview_url(self, path: str, *, ttl_s: int) -> str: ...

    async def open_preview(self, key: str, expires_at: int, signature: str) -> bytes:
        """Usado solo por la ruta HTTP que sirve `signed_preview_url`
        (`presentation/kit_tools.py::build_kit_preview_router`), nunca por
        un handler de herramienta MCP."""
        ...
