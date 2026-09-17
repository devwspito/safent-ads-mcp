"""Politica de tiempo de la publicacion (FR-08, contracts/api.md
§on_approve.grace_seconds): UNA sola fuente para los 45 s de gracia --
`ApproveCampaignPackage` los devuelve al dueño, `RunPackagePublication` no
toca nada hasta que pasan, y `UndoPackagePublication` los usa para decidir
si "Deshacer" cancela sin haber escrito nada. Antes vivian como tres
constantes identicas mantenidas a mano (revision de codigo, nit): un
cambio de numero que solo tocara una de las tres las desincronizaria en
silencio."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

__all__ = ["PACKAGE_UNDO_GRACE", "PACKAGE_UNDO_GRACE_SECONDS"]

PACKAGE_UNDO_GRACE: Final[timedelta] = timedelta(seconds=45)
PACKAGE_UNDO_GRACE_SECONDS: Final[int] = int(PACKAGE_UNDO_GRACE.total_seconds())
