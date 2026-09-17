"""Excepciones de aplicacion de `creative` (plan.md §8: excepciones de
dominio -> mapeador en presentacion; aqui las de caso de uso: entidad no
encontrada, precondicion de puerto)."""

from __future__ import annotations

from safent_ads.shared.errors import ApplicationError


class CreativeBriefNotFoundError(ApplicationError):
    """No existe un `CreativeBrief` con ese `BriefId`."""


class CreativeAssetNotFoundError(ApplicationError):
    """No existe un `CreativeAsset` con ese `AssetId`."""


class CreativeAssetMissingCopyError(ApplicationError):
    """`RunPolicyCheck` sobre un activo sin `ad_copy` compuesto todavia
    (visual crudo, previo a `ComposeBanner`): no hay texto que verificar."""


class CreativeJobNotFoundError(ApplicationError):
    """No existe un `CreativeJob` con ese `JobId`."""


class RenderBudgetExceededError(ApplicationError):
    """`max_cost` superado: el trabajo se aborta, nunca se encarece en
    silencio (creative-port.md §"Seleccion de renderizador"). Tambien la
    lanza `mcp.infrastructure.broker_image_renderer_port.BrokerImageRenderer`
    (traducida desde `RENDER_COST_CAP`, M-2 revision de seguridad 0.2.22)
    cuando el tope de coste lo aplica `ads-broker` ANTES de llamar al
    proveedor, no solo el chequeo posterior de `GenerateCreativeAssets`."""


class RenderQuotaExceededError(ApplicationError):
    """M-2 (revision de seguridad 0.2.22): cuota de `render_image` agotada
    para este negocio (minuto o dia). Distinta de
    `ImageGenerationUnavailableError` -- probar otro renderizador de la
    cascada no libera la cuota de ESTE negocio, asi que
    `GenerateCreativeAssets` para en vez de seguir intentando."""


class ImageGenerationUnavailableError(ApplicationError):
    """Se agoto la cascada de `RendererSelector.candidates_for`: ni la
    delegacion al agente ni ningun proveedor directo produjeron un activo
    (correccion del propietario 2026-09-09: "the tool result must say
    plainly what is missing and how to enable it" — nunca una degradacion
    silenciosa). `CreativeJob.failure_reason` transporta el mensaje
    accionable hasta el panel/la propuesta."""


class CreativeAssetPolicyCheckRequiredError(ApplicationError):
    """`ProposeCreative` sobre un activo cuyo `PolicyVerdict` no es `PASS`
    (rest-api.md: "422 POLICY_CHECK_REQUIRED si verdict != PASS") — mas
    estricto que el invariante del propio agregado (`propose()` solo exige
    "no FAIL"): la puerta REST de publicacion no deja pasar ni `WARN` ni
    "todavia sin revisar"."""


class AdSetReferenceNotFoundError(ApplicationError):
    """`ad_set_ref` no resuelve a una entidad publicitaria conocida, o
    resuelve a una de OTRO negocio (IDOR, threat-model.md C-27: mismo 404
    que "no existe", nunca 403 ni una diferenciacion que filtre
    existencia)."""
