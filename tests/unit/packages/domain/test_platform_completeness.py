"""`platform_completeness._google_asset_group_wire` (gap cerrado en esta
rama, 005-google-campaign-types): sin `assets.name` ni un hueco
`{creative_of:X}` por imagen, `native_ad_child.google_create` recibiria el
objeto de imagen entero donde Google exige el `resource_name` de texto del
recurso ya subido, y el viaje completo de Maximo Rendimiento no podia
correr (T040)."""

from __future__ import annotations

from safent_ads.packages.domain.approval_envelope import creative_hole, image_local_ref
from safent_ads.packages.domain.platform_completeness import ad_set_wire_plan
from safent_ads.shared.ids import PlatformCode

from .conftest import performance_max_ad_set


class TestGoogleAssetGroupWireCarriesNameAndCreativeHoles:
    def test_el_grupo_de_recursos_lleva_el_nombre_del_conjunto_planificado(self) -> None:
        ad_set = performance_max_ad_set(name="Grupo de recursos Reservas")

        wire = ad_set_wire_plan(ad_set, PlatformCode.GOOGLE)

        assert wire["native"]["assets"]["name"] == "Grupo de recursos Reservas"

    def test_cada_imagen_se_proyecta_como_el_hueco_creative_of_de_su_paso_de_subida(self) -> None:
        ad_set = performance_max_ad_set()
        assets = ad_set.native.assets

        wire = ad_set_wire_plan(ad_set, PlatformCode.GOOGLE)

        native_assets = wire["native"]["assets"]
        for field_name in ("logo", "marketing_image", "square_image"):
            checksum = getattr(assets, field_name).checksum
            assert native_assets[field_name] == creative_hole(image_local_ref(checksum))

    def test_la_forma_de_cable_sigue_teniendo_solo_kind_final_url_y_assets(self) -> None:
        """El `{"kind", "final_url", "assets"}` exacto que
        `proposals.domain.ad_child_creation._validate_google_asset_group_native`
        exige a nivel de `native` no cambia -- los huecos nuevos viven
        DENTRO de `assets`, no como hermanos suyos."""
        ad_set = performance_max_ad_set()

        wire = ad_set_wire_plan(ad_set, PlatformCode.GOOGLE)

        assert set(wire["native"].keys()) == {"kind", "final_url", "assets"}

    def test_las_tres_imagenes_distintas_producen_tres_huecos_distintos(self) -> None:
        ad_set = performance_max_ad_set()

        wire = ad_set_wire_plan(ad_set, PlatformCode.GOOGLE)

        native_assets = wire["native"]["assets"]
        holes = {native_assets[field] for field in ("logo", "marketing_image", "square_image")}
        assert len(holes) == 3
