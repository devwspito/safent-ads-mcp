"""Admision de un `WriteIntent`/`SignedAuthorization` de `kind ==
"package_step"` (`003-paquete-de-campana` contracts/api.md §R2.E,
data-model.md Revision 2 §R2.4: las siete reglas R1-R7).

Puro salvo por `resolve_created_resource` -- inyectado (protocolo
`CreatedResourceLookup`), nunca un import de `broker.infrastructure`: el
mismo criterio que ya separa `write_authorization.py` (puro) de
`write_pipeline.py` (I/O). El llamante resuelve el libro propio del bróker
(`WriteLedgerStore.created_resource`); esta funcion solo decide con las
respuestas que recibe.

El bróker NUNCA importa `packages.domain` (data-model.md "Bounded contexts":
`packages -> {proposals, execution, accounts, shared}`, ninguna flecha de
vuelta) -- `envelope`/`binding` viajan como `Mapping[str, object]` puro
(la forma canonica ya serializada por `packages.domain`) y este modulo los
lee por clave, nunca por atributo de un tipo de ese contexto. Reimplementa
`reopen_holes`/`canonical_json_bytes` en miniatura por la misma razon que
`recompute_diff_hash` reimplementa la comprobacion 3 entera: el bróker nunca
se fia de una proyeccion que no pueda rehacer el mismo, sin preguntarle nada
a `ads-api` (contracts/api.md §R2.E, "el bróker NUNCA le pregunta a ads-api
quien es el padre de un paso")."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Protocol, cast

from safent_ads.accounts.application.ports import SignedAuthorization, WriteIntent
from safent_ads.accounts.domain.json_value import JsonValue
from safent_ads.accounts.domain.refs import AccountRef
from safent_ads.broker.domain.write_authorization import VerifierPort, WriteDenialCode
from safent_ads.proposals.domain.diff_hash import canonical_json_bytes
from safent_ads.shared.ids import EntityRef, EntityRefFormatError

__all__ = [
    "CreatedResourceLookup",
    "admit_package_step",
    "admit_package_upload",
    "build_package_step_idempotency_key",
]

_PACKAGE_STEP: str = "package_step"
_UPLOAD_CREATIVE: str = "UPLOAD_CREATIVE"
_BINDING_FIELDS_MATCHING_STEP: Sequence[str] = (
    "step_kind",
    "local_ref",
    "parent_local_ref",
    "payload_template_hash",
    "expected_done_steps",
)


class CreatedResourceLookup(Protocol):
    """Misma forma que `WriteLedgerStore.created_resource` (T108): el
    recurso confirmado (`platform_request_id`) de una escritura `SUCCEEDED`
    ya reservada bajo esa clave, o `None` si no existe/no tiene exito."""

    def __call__(self, idempotency_key: str) -> str | None: ...


def build_package_step_idempotency_key(publication_id: str, step_index: int) -> str:
    """Copia deliberada de `execution.domain.execution_attempt.
    build_package_step_idempotency_key` -- el bróker no importa `execution`
    (broker -> execution no es una dependencia establecida hoy) y una
    discrepancia de formato solo produce un fallo de busqueda (fail-closed,
    `PACKAGE_PARENT_UNCONFIRMED`/`PACKAGE_PAYLOAD_NOT_REPRODUCIBLE`), nunca
    una escritura admitida de mas."""
    return f"pkg-{publication_id}-{step_index:02d}"


def admit_package_step(  # noqa: PLR0911 - independent fail-closed rule gates (R1-R7)
    *,
    intent: WriteIntent,
    authorization: SignedAuthorization,
    verifier: VerifierPort,
    now: datetime,
    resolve_created_resource: Callable[[str], str | None],
) -> WriteDenialCode | None:
    """`None` = admitido (R1-R7 superadas). Fail-closed: la primera regla
    que falla deniega, las siguientes no se evaluan (mismo criterio que
    `verify_authorization`)."""
    if authorization.kind != _PACKAGE_STEP:
        # R1, mitad "prohibido": ningun otro `kind` lleva sobre ni binding.
        if intent.package_binding is not None or authorization.package_approval is not None:
            return WriteDenialCode.PACKAGE_BINDING_NOT_ALLOWED
        return None
    approval = authorization.package_approval
    binding = intent.package_binding
    if approval is None or binding is None:  # R1, mitad "obligatorio"
        return WriteDenialCode.PACKAGE_BINDING_REQUIRED
    envelope = approval.get("envelope")
    if not isinstance(envelope, Mapping):
        return WriteDenialCode.PACKAGE_APPROVAL_INVALID
    denial = _verify_human_approval(envelope, approval, verifier, now)
    if denial is not None:
        return denial
    denial = _verify_content(binding, envelope)
    if denial is not None:
        return denial
    step_plan = envelope.get("step_plan")
    if not isinstance(step_plan, Sequence):
        return WriteDenialCode.PACKAGE_BINDING_NOT_DERIVABLE
    denial = _verify_derivable(binding, step_plan)
    if denial is not None:
        return denial
    denial = _verify_parent(intent, binding, resolve_created_resource)
    if denial is not None:
        return denial
    denial = _verify_scope(intent, binding)
    if denial is not None:
        return denial
    if binding.get("step_kind") == "ACTIVATE_CAMPAIGN":
        # `packages.domain.approval_envelope.project_step_template` fija la
        # plantilla como `{"status": "ACTIVE"}` (un `dict`, exigido por
        # `canonical_json_bytes`), pero el `WriteIntent` real de un cambio de
        # `status` viaja como escalar suelto (`execution.infrastructure.
        # broker_platform._operation`, mismo convenio que PAUSE/RESUME) --
        # las dos formas nunca pueden ser byte-identicas bajo
        # `reopen_holes`. La activacion no tiene huecos ni variacion por
        # paquete: comprobar la transicion exacta es equivalente en fuerza a
        # reproducir la plantilla.
        return _verify_activation_payload(intent)
    return _verify_payload_reproducible(intent, binding, step_plan, resolve_created_resource)


_ACTIVATION_PARAMETER = "status"
_ACTIVATION_BEFORE = "PAUSED"
_ACTIVATION_AFTER = "ACTIVE"


def _verify_activation_payload(intent: WriteIntent) -> WriteDenialCode | None:
    if (
        intent.parametro != _ACTIVATION_PARAMETER
        or intent.valor_actual != _ACTIVATION_BEFORE
        or intent.valor_propuesto != _ACTIVATION_AFTER
    ):
        return WriteDenialCode.PACKAGE_PAYLOAD_NOT_REPRODUCIBLE
    return None


# ---------------------------------------------------------------------------
# R2 -- firma humana viva
# ---------------------------------------------------------------------------


def _verify_human_approval(
    envelope: Mapping[str, object],
    approval: Mapping[str, object],
    verifier: VerifierPort,
    now: datetime,
) -> WriteDenialCode | None:
    try:
        signature = bytes.fromhex(str(approval["signature"]))
    except (KeyError, ValueError):
        return WriteDenialCode.PACKAGE_APPROVAL_INVALID
    # `envelope` es la forma canonica ya serializada por `packages.domain`
    # (`PackageApprovalEnvelope.to_canonical()`): solo primitivas JSON, nunca
    # un objeto de dominio -- el `cast` documenta esa garantia de frontera en
    # vez de tipar `WriteIntent.package_binding`/`SignedAuthorization.
    # package_approval` como `JsonValue` (rompería la aciclicidad frente a
    # `accounts`, que no importa `shared.crypto`).
    if not verifier.verify(cast("Mapping[str, JsonValue]", envelope), signature):
        return WriteDenialCode.PACKAGE_APPROVAL_INVALID
    if not str(approval.get("authorization_id", "")).strip():
        return WriteDenialCode.PACKAGE_APPROVAL_INVALID
    envelope_expiry = _parse_datetime(envelope.get("approval_expires_at"))
    proof_expiry = _parse_datetime(approval.get("expires_at"))
    if envelope_expiry is None or proof_expiry is None or envelope_expiry != proof_expiry:
        return WriteDenialCode.PACKAGE_APPROVAL_INVALID
    if now >= envelope_expiry:
        return WriteDenialCode.PACKAGE_APPROVAL_EXPIRED
    return None


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# R3 -- contenido: el sobre firmado es la fuente de verdad
# ---------------------------------------------------------------------------


def _verify_content(
    binding: Mapping[str, object], envelope: Mapping[str, object]
) -> WriteDenialCode | None:
    if binding.get("package_hash") != envelope.get("package_hash"):
        return WriteDenialCode.PACKAGE_CHANGED
    if binding.get("envelope_hash") != _envelope_hash(envelope):
        return WriteDenialCode.PACKAGE_CHANGED
    # H-1 (revisión 0.2.22): el binding no puede cotejarse consigo mismo; la
    # identidad del paquete, la publicación y la cuenta salen SOLO del sobre.
    for field in _ENVELOPE_BOUND_FIELDS:
        expected = envelope.get(field)
        if not isinstance(expected, str) or not expected or binding.get(field) != expected:
            return WriteDenialCode.PACKAGE_SCOPE_MISMATCH
    return None


_ENVELOPE_BOUND_FIELDS: tuple[str, ...] = ("package_id", "publication_id", "account_ref")


def _envelope_hash(envelope: Mapping[str, object]) -> str:
    return hashlib.sha256(canonical_json_bytes(dict(envelope))).hexdigest()


# ---------------------------------------------------------------------------
# R4 -- derivabilidad: el binding se reconstruye solo del sobre
# ---------------------------------------------------------------------------


def _verify_derivable(
    binding: Mapping[str, object], step_plan: Sequence[object]
) -> WriteDenialCode | None:
    step_index = binding.get("step_index")
    if not isinstance(step_index, int) or not (0 <= step_index < len(step_plan)):
        return WriteDenialCode.PACKAGE_BINDING_NOT_DERIVABLE
    step = step_plan[step_index]
    if not isinstance(step, Mapping):
        return WriteDenialCode.PACKAGE_BINDING_NOT_DERIVABLE
    for field in _BINDING_FIELDS_MATCHING_STEP:
        if step.get(field) != binding.get(field):
            return WriteDenialCode.PACKAGE_BINDING_NOT_DERIVABLE
    denial = _verify_creative_sources_match(step, binding)
    if denial is not None:
        return denial
    if step.get("step_kind") == "ACTIVATE_CAMPAIGN":
        expected_done = step.get("expected_done_steps")
        if not isinstance(expected_done, int) or expected_done <= 0:
            return WriteDenialCode.PACKAGE_BINDING_NOT_DERIVABLE
    return _verify_parent_step_index(binding, step_plan, step_index)


def _verify_creative_sources_match(
    step: Mapping[str, object], binding: Mapping[str, object]
) -> WriteDenialCode | None:
    """L1 (revision de seguridad 0.2.23): `creative_sources` (binding) es
    `step.depends_on` (sobre) por construccion (`derive_step_binding`), pero
    nunca estuvo en `_BINDING_FIELDS_MATCHING_STEP` -- nombres de clave
    distintos a cada lado. Sin esta comprobacion, un binding podia declarar
    un `creative_sources` que el paso firmado NUNCA declaro
    (`_verify_payload_reproducible`/R5 lo habrian resuelto igual, sin que R4
    lo hubiera impedido antes). `or ()` tolera un sobre sin la clave (formas
    de prueba anteriores a `depends_on`) sin debilitar la regla: un paso
    real (`StepTemplate.to_canonical()`) siempre la incluye. Un valor que no
    sea una lista/tupla (a cualquier lado) deniega -- nunca revienta con un
    `TypeError` al comparar."""
    depends_on = step.get("depends_on") or ()
    creative_sources = binding.get("creative_sources") or ()
    if not isinstance(depends_on, Sequence) or not isinstance(creative_sources, Sequence):
        return WriteDenialCode.PACKAGE_BINDING_NOT_DERIVABLE
    if tuple(depends_on) != tuple(creative_sources):
        return WriteDenialCode.PACKAGE_BINDING_NOT_DERIVABLE
    return None


def _verify_parent_step_index(
    binding: Mapping[str, object], step_plan: Sequence[object], step_index: int
) -> WriteDenialCode | None:
    parent_local_ref = binding.get("parent_local_ref")
    parent_step_index = binding.get("parent_step_index")
    if parent_local_ref is None:
        return None if parent_step_index is None else WriteDenialCode.PACKAGE_BINDING_NOT_DERIVABLE
    candidates = [
        entry.get("step_index")
        for entry in step_plan
        if isinstance(entry, Mapping)
        and entry.get("local_ref") == parent_local_ref
        and isinstance(entry.get("step_index"), int)
        and entry["step_index"] < step_index
    ]
    if len(candidates) != 1 or candidates[0] != parent_step_index:
        return WriteDenialCode.PACKAGE_BINDING_NOT_DERIVABLE
    return None


# ---------------------------------------------------------------------------
# R5 -- padre con recibo confirmado, resuelto SOLO del libro propio
# ---------------------------------------------------------------------------


def _verify_parent(
    intent: WriteIntent,
    binding: Mapping[str, object],
    resolve_created_resource: Callable[[str], str | None],
) -> WriteDenialCode | None:
    publication_id = binding.get("publication_id")
    parent_local_ref = binding.get("parent_local_ref")
    if not isinstance(publication_id, str):
        return WriteDenialCode.PACKAGE_BINDING_NOT_DERIVABLE
    if parent_local_ref is None:
        account_ref = binding.get("account_ref")
        if str(intent.entity_ref) != account_ref:
            return WriteDenialCode.PACKAGE_PARENT_UNCONFIRMED
        return None
    parent_step_index = binding.get("parent_step_index")
    if not isinstance(parent_step_index, int):
        return WriteDenialCode.PACKAGE_PARENT_UNCONFIRMED
    key = build_package_step_idempotency_key(publication_id, parent_step_index)
    resource = resolve_created_resource(key)
    if resource is None or intent.entity_ref.external_id != resource:
        return WriteDenialCode.PACKAGE_PARENT_UNCONFIRMED
    return None


# ---------------------------------------------------------------------------
# R6 -- alcance: negocio/conexion/plataforma iguales, ninguno None
# ---------------------------------------------------------------------------


def _verify_scope(  # noqa: PLR0911 - independent fail-closed scope checks
    intent: WriteIntent, binding: Mapping[str, object]
) -> WriteDenialCode | None:
    raw_account_ref = binding.get("account_ref")
    if not isinstance(raw_account_ref, str):
        return WriteDenialCode.PACKAGE_SCOPE_MISMATCH
    try:
        account_ref = EntityRef.parse(raw_account_ref)
    except EntityRefFormatError:
        return WriteDenialCode.PACKAGE_SCOPE_MISMATCH
    entity_ref = intent.entity_ref
    if account_ref.business_id is None or account_ref.connection_id is None:
        return WriteDenialCode.PACKAGE_SCOPE_MISMATCH
    if entity_ref.business_id != account_ref.business_id:
        return WriteDenialCode.PACKAGE_SCOPE_MISMATCH
    if entity_ref.connection_id != account_ref.connection_id:
        return WriteDenialCode.PACKAGE_SCOPE_MISMATCH
    if entity_ref.platform != account_ref.platform:
        return WriteDenialCode.PACKAGE_SCOPE_MISMATCH
    if intent.business_id is None or intent.business_id != str(account_ref.business_id):
        return WriteDenialCode.PACKAGE_SCOPE_MISMATCH
    return None


# ---------------------------------------------------------------------------
# R7 -- reproduccion de la carga, huecos resueltos SOLO del libro propio
# ---------------------------------------------------------------------------


def _verify_payload_reproducible(
    intent: WriteIntent,
    binding: Mapping[str, object],
    step_plan: Sequence[object],
    resolve_created_resource: Callable[[str], str | None],
) -> WriteDenialCode | None:
    publication_id = binding.get("publication_id")
    creative_sources = binding.get("creative_sources")
    if not isinstance(publication_id, str) or not isinstance(creative_sources, Sequence):
        return WriteDenialCode.PACKAGE_PAYLOAD_NOT_REPRODUCIBLE
    resolutions: dict[str, str] = {}
    for local_ref in creative_sources:
        upload_index = _upload_step_index(step_plan, local_ref)
        if upload_index is None:
            return WriteDenialCode.PACKAGE_PAYLOAD_NOT_REPRODUCIBLE
        resource = resolve_created_resource(
            build_package_step_idempotency_key(publication_id, upload_index)
        )
        if resource is None:
            return WriteDenialCode.PACKAGE_PAYLOAD_NOT_REPRODUCIBLE
        resolutions[f"{{creative_of:{local_ref}}}"] = resource
    reopened = _reopen_holes(intent.valor_propuesto, resolutions)
    if not isinstance(reopened, dict) or _hash_json(reopened) != binding.get(
        "payload_template_hash"
    ):
        return WriteDenialCode.PACKAGE_PAYLOAD_NOT_REPRODUCIBLE
    return None


def _upload_step_index(step_plan: Sequence[object], local_ref: object) -> int | None:
    for entry in step_plan:
        if not isinstance(entry, Mapping):
            continue
        step_index = entry.get("step_index")
        if (
            entry.get("local_ref") == local_ref
            and entry.get("step_kind") == _UPLOAD_CREATIVE
            and isinstance(step_index, int)
        ):
            return step_index
    return None


def _reopen_holes(payload: object, resolutions: Mapping[str, str]) -> object:
    """Inversa de `packages.domain.approval_envelope.substitute_holes`,
    reimplementada aqui a proposito (el bróker no importa `packages.domain`,
    ver docstring del modulo): sustituye cada valor RESUELTO por su hueco
    simbolico, para poder comparar contra `payload_template_hash` sin
    conocer el plan firmado, solo el sobre y sus propios recibos."""
    reverse = {resolved: hole for hole, resolved in resolutions.items()}
    if isinstance(payload, Mapping):
        return {key: _reopen_holes(value, resolutions) for key, value in payload.items()}
    if isinstance(payload, list):
        return [_reopen_holes(item, resolutions) for item in payload]
    if isinstance(payload, str) and payload in reverse:
        return reverse[payload]
    return payload


def _hash_json(value: dict[str, object]) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


# ---------------------------------------------------------------------------
# admit_package_upload -- H1 (revision de seguridad 0.2.23): el paso
# UPLOAD_CREATIVE, admitido con R1-R6 mas la comprobacion de checksum contra
# la plantilla firmada. Nunca R5 (no tiene padre) ni R7 (no hay huecos que
# reabrir): en su lugar, la carga es el propio activo, cuyo `sha256` debe
# coincidir con el `payload_template_hash` de SU paso en el sobre.
# ---------------------------------------------------------------------------


def admit_package_upload(  # noqa: PLR0911 - independent fail-closed rule gates (R1-R6 + checksum)
    *,
    account_ref: AccountRef,
    media: bytes,
    mime_type: str,
    width: int,
    height: int,
    binding: Mapping[str, object] | None,
    approval: Mapping[str, object] | None,
    verifier: VerifierPort,
    now: datetime,
) -> WriteDenialCode | None:
    """`None` = admitido. `binding`/`approval` ausentes los dos: subida
    SUELTA (`upload_creative_asset` independiente, BL-6 original), nunca
    toca este camino -- el llamante (`WriteAuthorizationPipeline.
    admit_upload`) ya filtra ese caso antes de llegar aqui, pero la mitad
    "obligatorio" de R1 (uno presente sin el otro) se comprueba aqui tambien,
    fail-closed."""
    if binding is None or approval is None:
        return WriteDenialCode.PACKAGE_BINDING_REQUIRED
    if binding.get("step_kind") != _UPLOAD_CREATIVE:
        return WriteDenialCode.PACKAGE_BINDING_NOT_DERIVABLE
    envelope = approval.get("envelope")
    if not isinstance(envelope, Mapping):
        return WriteDenialCode.PACKAGE_APPROVAL_INVALID
    denial = _verify_human_approval(envelope, approval, verifier, now)
    if denial is not None:
        return denial
    denial = _verify_content(binding, envelope)
    if denial is not None:
        return denial
    step_plan = envelope.get("step_plan")
    if not isinstance(step_plan, Sequence):
        return WriteDenialCode.PACKAGE_BINDING_NOT_DERIVABLE
    denial = _verify_derivable(binding, step_plan)
    if denial is not None:
        return denial
    denial = _verify_upload_scope(account_ref, binding)
    if denial is not None:
        return denial
    return _verify_upload_checksum(media, mime_type, width, height, binding)


def _verify_upload_scope(
    account_ref: AccountRef, binding: Mapping[str, object]
) -> WriteDenialCode | None:
    """Analogo a R5/R6 para un paso sin padre (`_verify_parent`, rama
    `parent_local_ref is None`): la cuenta que de verdad va a recibir la
    subida (resuelta por el bróker desde la conexion autenticada, nunca del
    binding) debe ser BYTE-IGUAL a la que el sobre firmo -- ninguna de las
    dos puede faltar `business_id`/`connection_id` (cuenta legacy
    ambigua)."""
    if account_ref.business_id is None or account_ref.connection_id is None:
        return WriteDenialCode.PACKAGE_SCOPE_MISMATCH
    if str(account_ref) != binding.get("account_ref"):
        return WriteDenialCode.PACKAGE_SCOPE_MISMATCH
    return None


def _verify_upload_checksum(
    media: bytes, mime_type: str, width: int, height: int, binding: Mapping[str, object]
) -> WriteDenialCode | None:
    """El bróker nunca confia en el `checksum` que declare el llamante --
    lo recalcula el mismo de los bytes recibidos, arma la MISMA plantilla
    que `packages.domain.approval_envelope._upload_creative_template`
    proyecta (`checksum`/`mime_type`/`width`/`height`) y compara su huella
    contra `payload_template_hash` (ya verificado igual al del paso firmado
    por R4): son las UNICAS cuatro claves de esa plantilla, nunca una quinta
    que ads-api pudiera colar."""
    template = {
        "checksum": hashlib.sha256(media).hexdigest(),
        "mime_type": mime_type,
        "width": width,
        "height": height,
    }
    if _hash_json(template) != binding.get("payload_template_hash"):
        return WriteDenialCode.CREATIVE_CHECKSUM_MISMATCH
    return None
