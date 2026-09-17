"""`ApiError`: unico tipo de excepcion HTTP que cruza a la respuesta JSON,
con la forma de error de contracts/rest-api.md:
`{ "error": {"code", "message", "details"} }`. Nunca traza ni secreto
(plan.md §8). `composition/api.py` registra el `exception_handler` que
serializa esto; `audit/presentation` tambien lo importa desde aqui en vez
de duplicarlo, porque ya depende de `iam` para autenticacion y evita el
ciclo de importar `composition` desde una capa de presentacion."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException


class ApiError(HTTPException):
    def __init__(
        self, *, status_code: int, code: str, message: str, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(
            status_code=status_code,
            detail={"code": code, "message": message, "details": details or {}},
        )
