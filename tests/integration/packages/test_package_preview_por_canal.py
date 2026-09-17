"""`packages.presentation.panel_read._build_preview` por canal (tasks.md
T036; contracts/panel.md). Pruebas puras contra el árbol de dominio -- sin
Postgres (`_build_preview` sólo necesita el paquete, un `AssetStorePort` y un
`PackageStepRepository`, y este archivo pasa `publication=None`, así que
ninguno de los dos hace I/O real). Vive bajo `tests/integration/packages/`
por convención de vecindario con el resto de pruebas de `panel_read.py`, pero
no lleva `pytestmark = pytest.mark.integration`: no exige testcontainers ni
`make test-integration`, corre en `make test`."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from safent_ads.packages.domain.campaign_package import CampaignPackage
from safent_ads.packages.presentation.panel_read import _build_preview
from safent_ads.proposals.domain.google_bidding import ManualCpc
from safent_ads.shared.ids import BusinessId, PlatformCode
from tests.unit.creative.infrastructure.fakes import FakeAssetStore
from tests.unit.packages.application.conftest import FakePackageStepRepository
from tests.unit.packages.domain.conftest import (
    account_ref,
    google_campaign,
    google_campaign_native,
    performance_max_ad_set,
    performance_max_campaign_native,
    propose_google_package,
    propose_meta_package,
)

_GOAL_RESOURCE_NAME = "customers/1112223333/conversionActions/4445556666"
_PLATFORM_WIRE_CONTRACT = (
    Path(__file__).parents[2] / "contracts" / "package-preview-platform.json"
)


async def _preview_of(package: CampaignPackage) -> dict[str, Any]:
    return await _build_preview(package, None, FakeAssetStore(), FakePackageStepRepository())


def _pmax_package() -> CampaignPackage:
    campaign = google_campaign(native=performance_max_campaign_native())
    return propose_google_package(campaign=campaign, ad_sets=(performance_max_ad_set(),))


def _search_package() -> CampaignPackage:
    return propose_google_package()


def _row(native_summary: list[dict[str, str]], label: str) -> str | None:
    return next((item["value"] for item in native_summary if item["label"] == label), None)


def _collect_strings(node: object) -> list[str]:
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        return [text for value in node.values() for text in _collect_strings(value)]
    if isinstance(node, list | tuple):
        return [text for item in node for text in _collect_strings(item)]
    return []


def _collect_keys(node: object) -> set[str]:
    if isinstance(node, dict):
        keys = set(node.keys())
        for value in node.values():
            keys |= _collect_keys(value)
        return keys
    if isinstance(node, list | tuple):
        return {key for item in node for key in _collect_keys(item)}
    return set()


class TestResumenDeMaximoRendimiento:
    async def test_resumen_de_maximo_rendimiento_declara_canal_puja_y_meta(self) -> None:
        preview = await _preview_of(_pmax_package())

        summary = preview["campaign"]["native_summary"]
        assert _row(summary, "Canal") == "Máximo rendimiento"
        assert _row(summary, "Tipo y puja") == "Maximizar conversiones"
        assert _row(summary, "Optimiza para") == preview["campaign"]["objective_label"]
        assert _row(summary, "Automatización") is not None
        assert "aprobado" in _row(summary, "Automatización")

    async def test_resumen_de_pmax_no_lleva_resource_name(self) -> None:
        preview = await _preview_of(_pmax_package())

        all_text = " ".join(_collect_strings(preview))
        assert _GOAL_RESOURCE_NAME not in all_text
        assert "resource_name" not in _collect_keys(preview)
        assert "connection_id" not in _collect_keys(preview)
        assert "page_id" not in _collect_keys(preview)


class TestEtiquetaDelNodoPorCanal:
    async def test_etiqueta_del_nodo_por_canal(self) -> None:
        meta_preview = await _preview_of(propose_meta_package())
        search_preview = await _preview_of(_search_package())
        pmax_preview = await _preview_of(_pmax_package())

        assert meta_preview["campaign"]["ad_sets"][0]["node_label"] == "Conjunto de anuncios"
        assert search_preview["campaign"]["ad_sets"][0]["node_label"] == "Grupo de anuncios"
        assert pmax_preview["campaign"]["ad_sets"][0]["node_label"] == "Grupo de recursos"


class TestGrupoDeRecursos:
    async def test_final_url_completo_es_visible(self) -> None:
        preview = await _preview_of(_pmax_package())

        asset_group = preview["campaign"]["ad_sets"][0]["asset_group"]
        assert asset_group is not None
        assert asset_group["final_url"] == "https://clinicax.example/reservar"
        assert len(asset_group["images"]) == 3
        assert asset_group["audience_signal_count"] == 0

    async def test_paquetes_no_pmax_no_llevan_grupo_de_recursos(self) -> None:
        search_preview = await _preview_of(_search_package())
        meta_preview = await _preview_of(propose_meta_package())

        assert search_preview["campaign"]["ad_sets"][0]["asset_group"] is None
        assert meta_preview["campaign"]["ad_sets"][0]["asset_group"] is None


class TestSobreentregaPorPujaDeConversiones:
    async def test_puja_por_conversiones_declara_la_sobreentrega(self) -> None:
        preview = await _preview_of(_pmax_package())

        summary = preview["campaign"]["native_summary"]
        notice = _row(summary, "Aviso de gasto")
        assert notice is not None
        assert preview["money"]["total_cap"]["amount"] in notice

    async def test_manual_cpc_no_declara_sobreentrega(self) -> None:
        campaign = google_campaign(native=google_campaign_native(bidding_strategy=ManualCpc()))
        preview = await _preview_of(propose_google_package(campaign=campaign))

        summary = preview["campaign"]["native_summary"]
        assert _row(summary, "Aviso de gasto") is None


class TestCuentaConectada:
    """contracts/api.md §2 (`PackagePreview.platform`): `account` es siempre
    `{entity_ref, name}` y `publish_as` es hermano de `account`, nunca su
    campo -- un backend que sólo declaraba `entity_ref`/`publish_as`
    anidado rompía el zod real del panel (`packages.ts` exige
    `account.name`; bug detectado por el lane del panel, commits
    998c92e/7172f7a)."""

    async def test_account_lleva_nombre_legible_y_publish_as_va_al_lado(self) -> None:
        package = _search_package()
        preview = await _preview_of(package)

        assert preview["platform"]["account"] == {
            "entity_ref": str(package.account_ref),
            "name": package.account_ref.external_id,
        }
        assert preview["platform"]["publish_as"] is None

    async def test_publish_as_de_meta_no_va_anidado_en_account(self) -> None:
        preview = await _preview_of(propose_meta_package())

        assert "publish_as" not in preview["platform"]["account"]
        assert preview["platform"]["publish_as"] == {"page_name": "Clinica X"}


class TestContratoDeCablePlataforma:
    """El JSON real de `platform` (no un objeto Python) es lo que el panel
    parsea con `packagePreviewSchema` (`packages.ts`) -- `tests/contracts/
    package-preview-platform.json` es la fuente compartida entre este test
    y `panel/src/api/schemas/packages.contract.test.ts` (mismo patrón que
    `tests/contracts/cockpit-wire.json` / `panelRead.contract.test.ts`)."""

    async def test_el_bloque_platform_serializado_coincide_con_el_fixture_compartido(
        self,
    ) -> None:
        business = BusinessId.parse("10000000-0000-4000-8000-000000000099")
        connection_id = uuid.UUID("20000000-0000-4000-8000-000000000099")

        google_package = propose_google_package(
            business=business,
            account=account_ref(
                business,
                platform=PlatformCode.GOOGLE,
                connection_id=connection_id,
                external_id="100-000-0002",
            ),
        )
        meta_package = propose_meta_package(
            business=business,
            account=account_ref(
                business,
                platform=PlatformCode.META,
                connection_id=connection_id,
                external_id="act_100000000000001",
            ),
        )

        wire = {
            "google": (await _preview_of(google_package))["platform"],
            "meta": (await _preview_of(meta_package))["platform"],
        }
        expected = json.loads(_PLATFORM_WIRE_CONTRACT.read_text())
        assert wire == expected


class TestOnApproveDeMaximoRendimiento:
    """`_on_approve_block`: un grupo de recursos ES el anuncio (`ads_per_node
    = (0, 0)`) -- la frase nunca puede decir «0 anuncios» (bug detectado por
    el lane del panel)."""

    async def test_la_frase_habla_de_grupos_de_recursos_e_imagenes_no_de_anuncios(self) -> None:
        preview = await _preview_of(_pmax_package())

        sentence = preview["on_approve"]["sentence"]
        assert sentence == (
            "Se crearán 1 campaña y 1 grupo de recursos con 3 imágenes "
            "en Google, y la campaña quedará activa."
        )
        assert "anuncios" not in sentence

    async def test_los_paquetes_search_y_meta_conservan_la_frase_de_siempre(self) -> None:
        search_sentence = (await _preview_of(_search_package()))["on_approve"]["sentence"]
        meta_sentence = (await _preview_of(propose_meta_package()))["on_approve"]["sentence"]

        assert "conjuntos" in search_sentence
        assert "anuncios" in search_sentence
        assert "conjuntos" in meta_sentence
        assert "anuncios" in meta_sentence


class TestPalabrasProhibidas:
    """002-panel-simple/design.md §13.1: nada de «asset group», «PMax»,
    «opt-out» en el texto que ve el dueño -- aquí, todo el árbol de
    `PackagePreview` (los nombres de campo en inglés, como `asset_group`,
    no cuentan: son el contrato, no la vista, igual que en `schemas.ts`)."""

    async def test_ningun_texto_visible_usa_jerga_de_proveedor(self) -> None:
        preview = await _preview_of(_pmax_package())

        forbidden = ("asset group", "pmax", "opt-out")
        for text in _collect_strings(preview["campaign"]) + _collect_strings(preview["why"]):
            lowered = text.lower()
            for word in forbidden:
                assert word not in lowered, f"{word!r} en {text!r}"
