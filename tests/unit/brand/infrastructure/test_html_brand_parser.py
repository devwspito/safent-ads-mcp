"""Parsers de `html_brand_parser` sobre fixtures HTML/CSS neutras (sitios
falsos, ninguna marca real) -- sin red, sin fetch."""

from __future__ import annotations

from safent_ads.brand.domain.discovery import ContactChannelKind, DiscoverySource, SocialNetwork
from safent_ads.brand.infrastructure.html_brand_parser import (
    LogoHint,
    dedupe_and_cap_logo_hints,
    extract_business_name_candidates,
    extract_contact_channels,
    extract_copy_samples,
    extract_css_custom_property_colors,
    extract_css_font_family_candidates,
    extract_css_most_used_colors,
    extract_inline_style_text,
    extract_linked_page_urls,
    extract_logo_hints,
    extract_manifest_icon_hints,
    extract_manifest_url,
    extract_social_links,
    extract_stylesheet_urls,
    extract_web_font_link_candidates,
)

_BASE_URL = "https://example-business.test/"

_FIXTURE_HTML = """
<html><head>
<title>Example Business - Servicios</title>
<meta property="og:site_name" content="Example Business">
<meta property="og:image" content="/img/social.png">
<link rel="icon" href="/favicon.ico">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<link rel="manifest" href="/manifest.json">
<link rel="stylesheet" href="/styles.css">
<link rel="stylesheet" href="/print.css">
<link rel="stylesheet" href="/extra.css">
<link rel="stylesheet" href="/ignored-past-cap.css">
<link href="https://fonts.googleapis.com/css2?family=Poppins:wght@400;700" rel="stylesheet">
<style>
  :root { --brand-primary: #112233; --accent: #ff00aa; }
  body { font-family: 'Poppins', sans-serif; color: #112233; }
  .cta { color: #112233; }
</style>
<script type="application/ld+json">{"@type": "Organization", "name": "Example Business SL"}</script>
</head>
<body>
<img src="/img/logo.svg" alt="Example Business logo" class="site-logo">
<h1>Prepara tu lanzamiento con nosotros</h1>
<h2>Servicio de calidad desde 2010</h2>
<a href="https://instagram.com/examplebiz">Instagram</a>
<a href="https://wa.me/34912345678">WhatsApp</a>
<a href="/contacto" class="btn cta">Info ya</a>
<a href="/sobre-nosotros">Sobre nosotros</a>
<a href="/blog/post-1">Blog</a>
<form><input name="email"></form>
<p>Contacto: hola@example-business.test o 912345678</p>
</body></html>
"""


def test_extract_logo_hints_finds_favicon_apple_touch_og_image_and_img_hint() -> None:
    hints = extract_logo_hints(_BASE_URL, _FIXTURE_HTML)

    by_source = {h.source: h for h in hints}
    assert by_source[DiscoverySource.FAVICON].url == "https://example-business.test/favicon.ico"
    assert by_source[DiscoverySource.APPLE_TOUCH_ICON].url.endswith("apple-touch-icon.png")
    assert by_source[DiscoverySource.OG_IMAGE].url.endswith("/img/social.png")
    assert by_source[DiscoverySource.IMG_LOGO_HINT].url.endswith("/img/logo.svg")


def test_extract_logo_hints_caps_at_ten_after_deduping_by_url() -> None:
    """F-2 (CWE-770): 2 MiB de HTML sin este tope puede llevar decenas de
    miles de `<img>` con "logo" en el nombre -- cada uno, una descarga
    saliente."""
    many_logo_imgs = "".join(
        f'<img src="/img/logo-{i}.png" alt="logo {i}">' for i in range(50)
    )
    html = f"<html><body>{many_logo_imgs}</body></html>"

    hints = extract_logo_hints(_BASE_URL, html)

    assert len(hints) == 10


def test_dedupe_and_cap_logo_hints_keeps_first_occurrence_per_url() -> None:
    hints = [
        LogoHint("https://example-business.test/a.png", DiscoverySource.FAVICON, 0.3),
        LogoHint("https://example-business.test/a.png", DiscoverySource.IMG_LOGO_HINT, 0.7),
        LogoHint("https://example-business.test/b.png", DiscoverySource.OG_IMAGE, 0.6),
    ]

    capped = dedupe_and_cap_logo_hints(hints)

    assert [h.url for h in capped] == [
        "https://example-business.test/a.png",
        "https://example-business.test/b.png",
    ]
    assert capped[0].source == DiscoverySource.FAVICON  # se queda la primera aparicion


def test_dedupe_and_cap_logo_hints_caps_at_ten() -> None:
    hints = [
        LogoHint(f"https://example-business.test/{i}.png", DiscoverySource.IMG_LOGO_HINT, 0.5)
        for i in range(25)
    ]

    assert len(dedupe_and_cap_logo_hints(hints)) == 10


def test_extract_business_name_candidates_finds_all_three_sources() -> None:
    candidates = extract_business_name_candidates(_FIXTURE_HTML)

    by_source = {c.source: c.name for c in candidates}
    assert by_source[DiscoverySource.OG_SITE_NAME] == "Example Business"
    assert by_source[DiscoverySource.TITLE_TAG] == "Example Business - Servicios"
    assert by_source[DiscoverySource.SCHEMA_ORG_ORGANIZATION] == "Example Business SL"


def test_extract_social_links_matches_known_networks_only() -> None:
    links = extract_social_links(_FIXTURE_HTML)

    assert len(links) == 1
    assert links[0].network == SocialNetwork.INSTAGRAM


def test_extract_contact_channels_finds_email_phone_whatsapp_and_form() -> None:
    channels = extract_contact_channels(_BASE_URL + "contacto", _FIXTURE_HTML)

    kinds = {c.kind for c in channels}
    assert kinds == {
        ContactChannelKind.EMAIL,
        ContactChannelKind.PHONE,
        ContactChannelKind.WHATSAPP,
        ContactChannelKind.CONTACT_FORM,
    }
    assert all(c.page_url == _BASE_URL + "contacto" for c in channels)


def test_extract_contact_channels_never_leaks_the_raw_email_or_phone() -> None:
    channels = extract_contact_channels(_BASE_URL + "contacto", _FIXTURE_HTML)

    serialized = " ".join(f"{c.kind}:{c.page_url}" for c in channels)
    assert "hola@example-business.test" not in serialized
    assert "912345678" not in serialized


def test_extract_copy_samples_finds_headline_tagline_and_cta_without_pii() -> None:
    samples = extract_copy_samples(_FIXTURE_HTML)

    by_source = {s.source: s.text for s in samples}
    assert by_source[DiscoverySource.HERO_HEADLINE] == "Prepara tu lanzamiento con nosotros"
    assert by_source[DiscoverySource.TAGLINE] == "Servicio de calidad desde 2010"
    assert by_source[DiscoverySource.CTA_TEXT] == "Info ya"
    assert "@" not in " ".join(by_source.values())


def test_extract_linked_page_urls_filters_same_origin_and_keyword_and_caps() -> None:
    urls = extract_linked_page_urls(_BASE_URL, _FIXTURE_HTML)

    assert _BASE_URL + "contacto" in urls
    assert _BASE_URL + "sobre-nosotros" in urls
    assert not any("blog" in url for url in urls)


def test_extract_css_custom_property_colors_ranks_brand_named_properties_higher() -> None:
    css = extract_inline_style_text(_FIXTURE_HTML)
    colors = extract_css_custom_property_colors(css)

    primary = next(c for c in colors if c.hex == "#112233")
    assert primary.confidence > 0.5


def test_extract_css_most_used_colors_ranks_by_frequency() -> None:
    css = extract_inline_style_text(_FIXTURE_HTML)
    colors = extract_css_most_used_colors(css)

    assert colors[0].hex == "#112233"
    assert colors[0].confidence >= colors[1].confidence


def test_extract_css_font_family_candidates_skips_generic_families() -> None:
    css = extract_inline_style_text(_FIXTURE_HTML)
    candidates = extract_css_font_family_candidates(css)

    assert candidates[0].family == "Poppins"


def test_extract_web_font_link_candidates_reads_google_fonts_family_param() -> None:
    candidates = extract_web_font_link_candidates(_FIXTURE_HTML)

    assert candidates[0].family == "Poppins"


def test_extract_stylesheet_urls_caps_at_three() -> None:
    urls = extract_stylesheet_urls(_BASE_URL, _FIXTURE_HTML)

    assert len(urls) == 3
    assert "ignored-past-cap.css" not in " ".join(urls)


def test_extract_manifest_url_resolves_relative_href() -> None:
    assert extract_manifest_url(_BASE_URL, _FIXTURE_HTML) == _BASE_URL + "manifest.json"


def test_extract_manifest_icon_hints_reads_icons_array() -> None:
    hints = extract_manifest_icon_hints(_BASE_URL, '{"icons": [{"src": "/icon-192.png"}]}')

    assert hints[0].url == _BASE_URL + "icon-192.png"
    assert hints[0].source == DiscoverySource.MANIFEST_ICON


def test_extract_manifest_icon_hints_returns_empty_on_malformed_json() -> None:
    assert extract_manifest_icon_hints(_BASE_URL, "not json") == []
