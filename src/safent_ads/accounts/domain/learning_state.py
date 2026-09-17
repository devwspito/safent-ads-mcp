"""`LearningState`: fase de aprendizaje del algoritmo de la plataforma sobre
una `AdEntity` (Meta ~50 eventos de optimizacion/7 dias, Google Smart
Bidding ~7 dias — research/ads-platforms-and-mcps.md §5). Una entidad en
`LEARNING` nunca es accionable (`signals.gates.LearningGate`, fuera de este
lane)."""

from __future__ import annotations

from enum import StrEnum


class LearningState(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    LEARNING = "learning"
    LEARNED = "learned"
