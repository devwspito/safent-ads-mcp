"""Excepciones de infraestructura de `proposals`."""

from __future__ import annotations

from safent_ads.shared.errors import InfrastructureError


class DuplicateAuthorizationError(InfrastructureError):
    """Ya existe una decision viva para esa propuesta y ese `diff_hash`
    (indice unico parcial de `0008_proposals`). Aprobar dos veces el mismo
    cambio no crea dos autorizaciones: reaprobar exige rotar el diff."""


class UnknownRuleCodeError(InfrastructureError):
    """Una `rule_authorization` cita una regla que no esta en el catalogo.
    El esquema exige `rule_id` para ese tipo: sin regla no hay autonomia que
    respalde la firma (FR-11)."""


class AuthorizationProposalNotFoundError(InfrastructureError):
    """`approvals.proposal_id` viola su FK a `proposals.id`: la propuesta
    que esta autorizacion referencia todavia no existe (o nunca existio).
    Distinto de `DuplicateAuthorizationError` -- un llamador que confunda
    ambos errores arreglaria el sintoma equivocado (BUG corregido: antes
    `_translate` reclasificaba toda violacion no reconocida como
    duplicado, ocultando el orden de guardado real: `UndoExecution.
    _restore_immediately` guardaba la `Authorization` antes que su
    `Proposal` compensatoria)."""


class CorruptAuthorizationError(InfrastructureError):
    """La firma almacenada no es hexadecimal: la fila no se puede convertir
    en una `Authorization` verificable. Denegar antes que ejecutar con una
    firma que nadie puede comprobar (C-3)."""
