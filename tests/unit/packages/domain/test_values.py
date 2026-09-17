"""`AssetGroupAssetPlan` -- solo PERFORMANCE_MAX (data-model.md
`AssetGroupAssetPlan`, tasks.md T022). Relacion de aspecto por papel y la
aritmetica explicita del tope de operaciones por mutacion (threat-model.md
D-1/AL-6): nunca "25 a ojo", el tope se deriva de las propias cotas del
plan."""

from __future__ import annotations

import pytest

from safent_ads.packages.domain.errors import PlannedTreeError
from safent_ads.packages.domain.values import MAX_ASSET_GROUP_OPERATIONS, AssetGroupAssetPlan

from .conftest import asset_group_assets, image_creative


class TestAspectRatioPerRole:
    def test_relacion_de_aspecto_por_papel(self) -> None:
        plan = asset_group_assets()

        assert plan.logo.width == plan.logo.height
        assert plan.square_image.width == plan.square_image.height
        assert plan.marketing_image.width > plan.marketing_image.height

    def test_logo_fuera_del_2_por_ciento_de_1_1_falla(self) -> None:
        with pytest.raises(PlannedTreeError, match="asset_group_logo_aspect_ratio_invalid"):
            asset_group_assets(logo=image_creative(checksum="e" * 64, width=1080, height=900))

    def test_imagen_de_marketing_fuera_del_2_por_ciento_de_1_91_falla(self) -> None:
        with pytest.raises(
            PlannedTreeError, match="asset_group_marketing_image_aspect_ratio_invalid"
        ):
            asset_group_assets(
                marketing_image=image_creative(checksum="f" * 64, width=1200, height=1200)
            )

    def test_imagen_cuadrada_fuera_del_2_por_ciento_de_1_1_falla(self) -> None:
        with pytest.raises(
            PlannedTreeError, match="asset_group_square_image_aspect_ratio_invalid"
        ):
            asset_group_assets(
                square_image=image_creative(checksum="0" * 64, width=1080, height=600)
            )


class TestAssetGroupOperationCountArithmetic:
    def test_el_grupo_de_recursos_al_maximo_cabe_en_una_mutacion(self) -> None:
        plan = AssetGroupAssetPlan(
            headlines=tuple(f"Titular {index}" for index in range(15)),
            long_headlines=tuple(f"Titular largo numero {index}" for index in range(5)),
            descriptions=("Corta", *(f"Descripcion larga numero {index}" for index in range(4))),
            business_name="Clinica X",
            logo=image_creative(checksum="1" * 64, width=1080, height=1080),
            marketing_image=image_creative(checksum="2" * 64, width=1200, height=628),
            square_image=image_creative(checksum="3" * 64, width=1080, height=1080),
        )

        # Aritmetica explicita (threat-model.md D-1): 15 + 5 + 5 titulares/
        # descripciones + 4 recursos fijos (nombre, logo, marketing, cuadrada)
        # = 29 recursos; un `asset.create` + un `asset_group_asset.create`
        # por recurso, mas la creacion del propio `asset_group`.
        assert MAX_ASSET_GROUP_OPERATIONS == 2 * (15 + 5 + 5 + 4) + 1
        assert MAX_ASSET_GROUP_OPERATIONS == 59
        assert plan.headlines  # el plan al maximo se construye sin error
