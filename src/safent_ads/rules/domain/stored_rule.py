"""`StoredRule`: una `Rule` del catalogo mas lo que decide la base, no el
fichero.

`rules.yaml` es la fuente de la condicion, la ventana, la accion y la
magnitud; el nivel de autonomia y el interruptor de habilitada viven en la
base porque los cambia el propietario sin desplegar (tasks.md §Notas 4: 'el
catalogo nace NOTIFY con autonomy_enabled=false; pasar a AUTO es decision
del propietario, no un despliegue')."""

from __future__ import annotations

from dataclasses import dataclass

from safent_ads.rules.domain.rule import Rule


@dataclass(frozen=True, kw_only=True, slots=True)
class StoredRule:
    rule: Rule
    is_enabled: bool

    @property
    def code(self) -> str:
        return self.rule.code
