"""`Cause`, `Evidence` y `CauseKey` (data-model.md: atributos de
`PropuestaDeAccion`; FR-17 agrupacion por causa; contracts/mcp-tools.md
`Cause`/`Evidence`).

`signals/` (N3, otra rama) todavia no publica estos VOs; se definen aqui,
localmente a `proposals`, con la misma forma que el contrato MCP para que la
integracion futura sea un simple cambio de import."""

from __future__ import annotations

from dataclasses import dataclass

from safent_ads.shared.errors import DomainError
from safent_ads.shared.ids import EntityRef

_MAX_CAUSE_TEXT_LENGTH = 140


class CauseInvariantError(DomainError):
    """`Cause.text` no respeta el limite de longitud del contrato MCP."""


@dataclass(frozen=True, slots=True)
class Cause:
    """Motivo en lenguaje llano de una propuesta (`contracts/mcp-tools.md`)."""

    text: str
    signal_id: str | None = None
    rule_id: str | None = None

    def __post_init__(self) -> None:
        if not self.text or len(self.text) > _MAX_CAUSE_TEXT_LENGTH:
            raise CauseInvariantError(
                f"Cause.text debe tener 1-{_MAX_CAUSE_TEXT_LENGTH} caracteres, "
                f"tiene {len(self.text)}"
            )


@dataclass(frozen=True, slots=True)
class Evidence:
    """Metrica que sustenta la causa (`contracts/mcp-tools.md::Evidence`)."""

    metric: str
    actual: float
    target: float
    window_preset: str


@dataclass(frozen=True, slots=True)
class CauseKey:
    """Clave de agrupacion FR-17: causa tipada + entidad + regla."""

    entity_ref: EntityRef
    rule_id: str
    cause_type: str

    def as_grouping_key(self) -> str:
        return f"{self.cause_type}:{self.rule_id}:{self.entity_ref}"
