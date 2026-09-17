"""Errores de aplicacion de `brand`."""

from __future__ import annotations

from safent_ads.shared.errors import ApplicationError


class BrandKitNotFoundError(ApplicationError):
    """Ningun `BrandKit` guardado para ese `business_id` (el propietario
    aun no ha cargado `config/brand/<business>.yaml`)."""


class BrandDraftNotFoundError(ApplicationError):
    """Ningun `BrandDiscoveryDraft` guardado para ese `business_id`: no se
    ha rastreado ninguna web ni subido nada a mano todavia."""


class BrandDraftAssetNotFoundError(ApplicationError):
    """Un `asset_id` referenciado (`ConfirmBrandDraft.selected_asset_ids`,
    `UploadBrandAsset.select_existing`) no esta entre los candidatos de
    logo del borrador actual."""


class BrandAssetNotFoundError(ApplicationError):
    """Ningun `BrandAsset` confirmado ni `LogoCandidate` de borrador con
    ese `asset_id` para el negocio (`GetBrandAssetPreview`): mismo motivo
    para negocio ajeno, kit inexistente o `asset_id` desconocido -- la
    presentacion siempre lo mapea a 404, nunca a 403 (threat-model.md
    C-27, evita filtrar cual de los tres caso es)."""


class BrandWebsiteUnreachableError(ApplicationError):
    """`WebsiteBrandDiscoveryPort.discover` fallo (SSRF bloqueado,
    robots.txt, tamano excedido, error de red): traduce cualquier
    `InfrastructureError` del puerto a un resultado que la presentacion
    puede mapear a un `422` sin conocer el adaptador concreto -- la causa
    real sigue en `__cause__` para los logs, nunca en el mensaje al
    cliente."""
