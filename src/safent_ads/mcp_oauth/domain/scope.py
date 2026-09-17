"""`Scope`/`ScopeSet` (data-model.md, contracts/oauth.md §1): los dos
alcances que el AS puede conceder. `ads:propose` sigue exigiendo
aprobacion humana en el dispatcher de `mcp/` -- `mcp_oauth` no relaja esa
regla, solo la habilita a nivel de token (tasks.md T012, fuera de esta
fase)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from safent_ads.mcp_oauth.domain.errors import EmptyScopeSetError, UnknownScopeError


class Scope(StrEnum):
    READ = "ads:read"
    PROPOSE = "ads:propose"


@dataclass(frozen=True, slots=True)
class ScopeSet:
    """Conjunto no vacio de `Scope`. `__str__` serializa ordenado para que
    la cadena `scope` de las respuestas OAuth sea estable entre llamadas."""

    scopes: frozenset[Scope]

    def __post_init__(self) -> None:
        if not self.scopes:
            raise EmptyScopeSetError("un alcance vacio no autoriza nada")

    @classmethod
    def parse(cls, raw: str) -> ScopeSet:
        tokens = raw.split()
        if not tokens:
            raise EmptyScopeSetError("scope vacio")
        try:
            return cls(frozenset(Scope(token) for token in tokens))
        except ValueError as exc:
            raise UnknownScopeError(f"scope desconocido en {raw!r}") from exc

    def __str__(self) -> str:
        return " ".join(sorted(scope.value for scope in self.scopes))

    def contains(self, scope: Scope) -> bool:
        return scope in self.scopes

    def is_subset_of(self, other: ScopeSet) -> bool:
        return self.scopes <= other.scopes
