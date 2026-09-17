"""Tope compartido de decodificacion de imagenes (Bj-1, threat-model.md
C-28/C-30): `Image.MAX_IMAGE_PIXELS` es un atributo GLOBAL de Pillow para
todo el proceso -- mutarlo por separado en mas de un modulo (con el mismo
valor o, peor, con valores distintos) deja el limite real a merced de cual
modulo se importo ultimo. Una sola fuente de verdad, importada donde haga
falta abrir una imagen no confiable."""

from __future__ import annotations

from PIL import Image

# 16 M pixeles (~4000x4000): un logo/favicon/og:image o un activo
# publicitario real cabe de sobra (mismo criterio que se uso primero en
# `brand/infrastructure/website_brand_extractor.py`).
MAX_IMAGE_PIXELS = 16_000_000
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS

__all__ = ["MAX_IMAGE_PIXELS"]
