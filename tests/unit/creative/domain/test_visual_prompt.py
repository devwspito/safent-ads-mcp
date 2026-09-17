"""Separacion capa-visual / capa-copy (medido sobre renders reales el
2026-09-09): el prompt que llega al
renderizador nunca menciona la plataforma/UI/dispositivo, y el titular
sobre-imagen que el modelo puede dibujar de forma fiable esta acotado a
3 palabras — el copy exacto lo compone siempre `BannerComposerPort`."""

from __future__ import annotations

import pytest

from safent_ads.creative.domain.visual_prompt import (
    VisualPromptError,
    build_visual_prompt,
    sanitize_scene_description,
)


def test_sanitizer_strips_platform_mention() -> None:
    sanitized = sanitize_scene_description("Anuncio para Instagram de una tienda de bicicletas")

    assert "instagram" not in sanitized.lower()
    assert "anuncio" not in sanitized.lower()


def test_sanitizer_strips_device_and_ui_terms() -> None:
    sanitized = sanitize_scene_description(
        "Captura de pantalla de un móvil mostrando la interfaz de la app"
    )

    for forbidden in ("captura de pantalla", "móvil", "interfaz", "app"):
        assert forbidden not in sanitized.lower()


def test_sanitizer_collapses_double_spaces_left_by_removal() -> None:
    sanitized = sanitize_scene_description("Estudiante feliz en Instagram estudiando")

    assert "  " not in sanitized


def test_sanitizer_preserves_unrelated_content() -> None:
    sanitized = sanitize_scene_description("Estudiante sonriendo con libros en una biblioteca")

    assert sanitized == "Estudiante sonriendo con libros en una biblioteca"


def test_sanitizer_rejects_empty_text() -> None:
    with pytest.raises(VisualPromptError):
        sanitize_scene_description("   ")


def test_build_visual_prompt_without_headline() -> None:
    prompt = build_visual_prompt("Estudiante sonriendo en una biblioteca")

    assert "biblioteca" in prompt.text
    assert prompt.on_screen_headline is None


def test_build_visual_prompt_includes_negative_guidance() -> None:
    prompt = build_visual_prompt("Estudiante sonriendo en una biblioteca")

    assert "mockups" in prompt.text


def test_build_visual_prompt_accepts_short_headline() -> None:
    prompt = build_visual_prompt("Estudiante sonriendo", on_screen_headline="Tu plaza ya")

    assert prompt.on_screen_headline == "Tu plaza ya"
    assert "Tu plaza ya" in prompt.text


def test_build_visual_prompt_rejects_headline_over_three_words() -> None:
    with pytest.raises(VisualPromptError):
        build_visual_prompt("Estudiante sonriendo", on_screen_headline="Tu plaza empieza aqui hoy")


def test_build_visual_prompt_sanitizes_headline_too() -> None:
    prompt = build_visual_prompt("Estudiante sonriendo", on_screen_headline="Anuncio ya")

    assert prompt.on_screen_headline == "ya"


def test_build_visual_prompt_never_contains_full_ad_copy_via_headline_limit() -> None:
    long_copy = "Prepárate con Negocio Ejemplo y consigue tu plaza en el próximo lanzamiento"

    with pytest.raises(VisualPromptError):
        build_visual_prompt("Aula de estudio", on_screen_headline=long_copy)
