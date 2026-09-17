"""Red de regresion de Busqueda (tasks.md T003, plan T021): congela, como
constantes del propio test, la proyeccion canonica JSON y el `package_hash`
del paquete de Busqueda valido de hoy (mismo arbol que
`tests/contract/mcp/test_search_native_args_wire_shape_unchanged.py` y que
`google_1x3_payload()` en `tests/contract/mcp/test_propose_campaign_package.py`).

Ni un byte de este fichero se toca al introducir Display/Demand
Gen/Performance Max: si alguna de las dos aserciones deja de cumplirse,
Busqueda se ha movido."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from safent_ads.packages.domain.campaign_package import CampaignPackage
from safent_ads.packages.domain.identifiers import OfferingId, PackageId
from safent_ads.packages.domain.package_hash import compute_package_hash, package_tree_payload
from safent_ads.proposals.domain.diff_hash import canonical_json_bytes
from safent_ads.proposals.domain.money import Money
from safent_ads.shared.ids import BusinessId, EntityLevel, EntityRef, PlatformCode

from .conftest import google_ad_set, google_campaign, package_budget, package_rationale

_BUSINESS_ID = BusinessId(uuid.UUID("11111111-1111-1111-1111-111111111111"))
_ACCOUNT_REF = EntityRef(
    PlatformCode.GOOGLE,
    EntityLevel.ACCOUNT,
    "act_123",
    _BUSINESS_ID.value,
    uuid.UUID("22222222-2222-2222-2222-222222222222"),
)
_OFFERING_ID = OfferingId("offering-1")
_NOW = datetime(2026, 9, 14, 10, 0, tzinfo=UTC)

# Congelado ejecutando el codigo de hoy (T003): `canonical_json_bytes` del
# payload que produce `package_tree_payload` para el paquete de Busqueda
# de `google_1x3_payload()`.
_FROZEN_CANONICAL_JSON = (
    '{"account_ref":"google:account:11111111-1111-1111-1111-111111111111:'
    '22222222-2222-2222-2222-222222222222:act_123","budget":{"daily":'
    '{"amount":"20.00","currency":"EUR"},"duration_days":14},"business_id":'
    '"11111111-1111-1111-1111-111111111111","offering_id":"offering-1",'
    '"plan":{"ad_sets":[{"ads":[{"copy":{"descriptions":["Reserva tu cita '
    'en minutos","Atencion profesional cercana"],"headlines":["Reserva ya",'
    '"Cita veterinaria","Atencion 24h"]},"creative":{"kind":"text_only"},'
    '"cta":null,"landing":"https://clinicax.example/reservar","local_ref":'
    '"as#1/ad#1","name":"Anuncio 1"}],"audience":{"native":{"age_max":65,'
    '"age_min":25,"geo":"valencia"},"plain":"Mujeres y hombres de 25 a 65 '
    'a\\u00f1os, a 15 km de Valencia"},"cpc_bid":{"amount":"0.80","currency":'
    '"EUR"},"keywords":[{"match_type":"PHRASE","text":"reserva cita '
    'veterinario"}],"local_ref":"as#1","name":"Grupo 1","native":'
    '{"bidding_strategy":"MANUAL_CPC","targeting_mode":"INHERIT_CAMPAIGN",'
    '"type":"SEARCH_STANDARD"},"schedule":null}],"campaign":{"name":'
    '"Reserva de citas Google","native":{"advertising_channel_type":'
    '"SEARCH","bidding_strategy":"MANUAL_CPC",'
    '"contains_eu_political_advertising":'
    '"DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING","network_settings":'
    '{"target_content_network":false,"target_google_search":true,'
    '"target_partner_search_network":false,"target_search_network":true}},'
    '"objective":"reservas"}},"platform":"google","publish_as":null,'
    '"rationale":{"kill_criterion":"CPL sobre 40 EUR durante 3 dias '
    'seguidos","owner_request":"Hazme una campa\\u00f1a de reserva de '
    'citas veterinarias","success_criterion":"CPL bajo 15 EUR en 7 dias",'
    '"why":"Tus clientes buscan cita fuera de horario y no hay campa\\u00f1a '
    'que los recoja"},"research":null,"schema_version":2}'
)

_FROZEN_PACKAGE_HASH = "34e8ce570df21ef62ee0f62f3573a119bc5fb8fd0e51019b04a7461388102eed"


def _search_payload() -> dict[str, object]:
    return package_tree_payload(
        business_id=_BUSINESS_ID,
        platform=PlatformCode.GOOGLE,
        account_ref=_ACCOUNT_REF,
        publish_as=None,
        offering_id=_OFFERING_ID,
        campaign=google_campaign(),
        ad_sets=(google_ad_set(),),
        daily_budget=Money.of("20.00"),
        rationale=package_rationale(),
        research=None,
    )


class TestSearchCanonicalUnchanged:
    def test_search_native_canonical_unchanged(self) -> None:
        canonical_bytes = canonical_json_bytes(_search_payload())

        assert canonical_bytes.decode() == _FROZEN_CANONICAL_JSON

    def test_hash_de_paquete_search_existente_no_cambia(self) -> None:
        payload_hash = compute_package_hash(_search_payload()).value

        assert payload_hash == _FROZEN_PACKAGE_HASH

        package = CampaignPackage.propose(
            package_id=PackageId.new(),
            business_id=_BUSINESS_ID,
            account_ref=_ACCOUNT_REF,
            publish_as=None,
            offering_id=_OFFERING_ID,
            campaign=google_campaign(),
            ad_sets=(google_ad_set(),),
            budget=package_budget(),
            rationale=package_rationale(),
            research=None,
            now=_NOW,
            expires_at=_NOW + timedelta(hours=72),
        )

        assert package.package_hash.value == _FROZEN_PACKAGE_HASH
