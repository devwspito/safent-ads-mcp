"""Vocabulario de huecos simbolicos del sobre de aprobacion (data-model.md
"Revision 2" §R2.2, unico hueco de imagen: `"{creative_of:<local_ref>}"`).

Modulo hoja a proposito, sin importar nada de `packages.domain`:
`approval_envelope.py` importa DE `platform_completeness.py`
(`ad_set_wire_plan`/`ad_wire_plan`/`campaign_wire_plan`), asi que ninguno
de los dos puede importar del otro sin crear un ciclo -- ambos importan de
aqui en su lugar."""

from __future__ import annotations

_IMAGE_LOCAL_REF_CHECKSUM_LENGTH = 12


def image_local_ref(checksum: str) -> str:
    return f"img#{checksum[:_IMAGE_LOCAL_REF_CHECKSUM_LENGTH]}"


def creative_hole(local_ref: str) -> str:
    return f"{{creative_of:{local_ref}}}"
