"""Excepciones de infraestructura de `rules`."""

from __future__ import annotations

from safent_ads.shared.errors import InfrastructureError


class UnknownRuleCodeError(InfrastructureError):
    """El codigo de regla no esta en la base. El catalogo se siembra con
    `sync_catalog`; operar sobre un codigo ausente es un error de llamada,
    no un caso a ignorar."""


class IncompleteGuardrailRowError(InfrastructureError):
    """La fila de guardarrail no tiene todos los limites que exige
    `GuardrailPolicy`. Componer una politica a medias seria peor que no
    tenerla: dejaria pasar cambios que nadie autorizo."""


class DuplicateRuleCodeError(InfrastructureError):
    """`create` es alta, no upsert: ya existe una fila calibrada con este
    `code` en el catalogo global (`rules_code_scope_unique`). Tocar una
    regla existente es `PUT /rules/{id}`, no esta."""
