"""`PackageStepBinding` -- lo que viaja firmado con cada paso de
publicacion (data-model.md "Revision 2" §R2.3; tasks.md T102/T103).

**Todo campo salvo `parent_entity_ref` es derivable del sobre firmado**
(`PackageApprovalEnvelope`) mas el `step_index` que se esta ejecutando. Ni
el chokepoint ni el broker aceptan nunca un binding "tal cual": lo derivan
del sobre con `derive_step_binding` y comparan con el recibido -- un
binding que no se pueda derivar se deniega antes de cualquier escritura.

Alcance de esta entrega (T102, absorbido en T015): el tipo de valor y su
derivacion pura (`derive_step_binding`), con su prueba de determinismo. Las
**siete reglas de admision** que VERIFICAN un binding contra un recibo
confirmado del broker (R2.4, tabla R1-R7 de `data-model.md`) son T103,
fuera de este commit: dependen de `WriteLedgerStore`/`Authorization`
(contextos `execution`/`proposals`, fuera de mi propiedad de fichero) y de
la re-revision de seguridad T113 antes de mergear ningun camino de
escritura real."""

from __future__ import annotations

from dataclasses import dataclass

from safent_ads.packages.domain.approval_envelope import (
    PackageApprovalEnvelope,
    StepKind,
    StepTemplate,
)
from safent_ads.packages.domain.errors import PackageDomainError
from safent_ads.shared.ids import EntityRef


class StepBindingDerivationError(PackageDomainError):
    """El sobre firmado no permite derivar un binding para ese paso: indice
    fuera de rango, o el padre declarado no aparece exactamente una vez
    antes de el en el plan firmado."""


@dataclass(frozen=True, slots=True)
class PackageStepBinding:
    package_id: str
    package_hash: str
    publication_id: str
    envelope_hash: str
    step_index: int
    step_kind: StepKind
    local_ref: str
    parent_local_ref: str | None
    parent_step_index: int | None
    parent_entity_ref: EntityRef | None
    # T123/AL-4 (data-model.md R2.8, generalizado de la activacion a todo
    # paso con padre): el `confirmed_state_hash` del recibo del paso padre,
    # nunca de `ad_entities` -- el padre lo creo la MISMA saga y no vive
    # alli todavia. Igual que `parent_entity_ref`, lo trae quien ejecuta,
    # leido de SU PROPIO recibo confirmado (R5); no es derivable del sobre.
    parent_receipt_state_hash: str | None
    account_ref: EntityRef
    payload_template_hash: str
    creative_sources: tuple[str, ...]
    expected_done_steps: int | None

    def to_canonical(self) -> dict[str, object]:
        return {
            "package_id": self.package_id,
            "package_hash": self.package_hash,
            "publication_id": self.publication_id,
            "envelope_hash": self.envelope_hash,
            "step_index": self.step_index,
            "step_kind": self.step_kind.value,
            "local_ref": self.local_ref,
            "parent_local_ref": self.parent_local_ref,
            "parent_step_index": self.parent_step_index,
            "parent_entity_ref": _optional_ref(self.parent_entity_ref),
            "parent_receipt_state_hash": self.parent_receipt_state_hash,
            "account_ref": str(self.account_ref),
            "payload_template_hash": self.payload_template_hash,
            "creative_sources": list(self.creative_sources),
            "expected_done_steps": self.expected_done_steps,
        }


def _optional_ref(ref: EntityRef | None) -> str | None:
    return str(ref) if ref is not None else None


def derive_step_binding(
    envelope: PackageApprovalEnvelope,
    envelope_hash: str,
    step_index: int,
    parent_entity_ref: EntityRef | None,
    parent_confirmed_state_hash: str | None = None,
) -> PackageStepBinding:
    """Deriva el binding del paso `step_index` **solo** del sobre firmado,
    mas el `entity_ref` ya confirmado del padre y su `confirmed_state_hash`
    (ninguno de los dos vive en el sobre: los trae quien ejecuta, leidos de
    su propio recibo -- regla R5, T123/AL-4). Pura: mismo sobre + mismo
    indice + mismo padre -> mismo binding, siempre (prueba de determinismo
    en `test_step_binding.py`)."""
    if not (0 <= step_index < len(envelope.step_plan)):
        raise StepBindingDerivationError(f"step_index fuera de rango: {step_index}")
    step = envelope.step_plan[step_index]
    parent_step_index = _find_parent_step_index(envelope.step_plan, step)
    return PackageStepBinding(
        package_id=str(envelope.package_id),
        package_hash=envelope.package_hash,
        publication_id=envelope.publication_id,
        envelope_hash=envelope_hash,
        step_index=step.step_index,
        step_kind=step.step_kind,
        local_ref=step.local_ref,
        parent_local_ref=step.parent_local_ref,
        parent_step_index=parent_step_index,
        parent_entity_ref=parent_entity_ref,
        parent_receipt_state_hash=parent_confirmed_state_hash,
        account_ref=envelope.account_ref,
        payload_template_hash=step.payload_template_hash,
        creative_sources=step.depends_on,
        expected_done_steps=step.expected_done_steps,
    )


def parent_step_index_of(envelope: PackageApprovalEnvelope, step_index: int) -> int | None:
    """Forma publica de `_find_parent_step_index` (R2.4, regla R4): la
    necesita `RunPackagePublication` para leer el recibo del padre ANTES
    de poder derivar el binding del paso (que exige el `entity_ref` ya
    resuelto del padre como parametro, no lo resuelve el mismo)."""
    if not (0 <= step_index < len(envelope.step_plan)):
        raise StepBindingDerivationError(f"step_index fuera de rango: {step_index}")
    return _find_parent_step_index(envelope.step_plan, envelope.step_plan[step_index])


def _find_parent_step_index(step_plan: tuple[StepTemplate, ...], step: StepTemplate) -> int | None:
    if step.parent_local_ref is None:
        return None
    candidates = [
        candidate.step_index
        for candidate in step_plan
        if candidate.local_ref == step.parent_local_ref and candidate.step_index < step.step_index
    ]
    if len(candidates) != 1:
        raise StepBindingDerivationError(
            f"padre ambiguo o inexistente para {step.local_ref!r}: {candidates}"
        )
    return candidates[0]
