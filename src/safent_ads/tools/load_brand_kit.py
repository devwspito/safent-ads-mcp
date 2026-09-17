"""Carga o actualiza el `BrandKit` de un negocio desde
`config/brand/<business>.yaml` (tool-surface.md §6: "seed loader from a
YAML file so the owner can fill it by hand"). Mismo espiritu que
`tools/seed_owner.py`: una via minima, no un endpoint ni un flujo
automatico -- el propietario edita el YAML a mano y vuelve a correr esto.

Uso:
    python -m safent_ads.tools.load_brand_kit \\
        --business-id <uuid> --file config/brand/example.yaml

`--business-id` viene del operador, nunca del contenido del YAML
(`yaml_brand_kit_schema.BrandKitPayload` no tiene ese campo a proposito):
un YAML mal editado no puede escribir el kit de marca de otro negocio."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from safent_ads.brand.infrastructure.sql_brand_kit_repository import SqlBrandKitRepository
from safent_ads.brand.infrastructure.yaml_brand_kit_loader import load_brand_kit_yaml
from safent_ads.composition.settings import ApiSettings
from safent_ads.shared.clock import SystemClock
from safent_ads.shared.ids import BusinessId


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--business-id", required=True)
    parser.add_argument("--file", required=True)
    return parser.parse_args(argv)


async def _load(*, business_id: str, path: Path) -> None:
    settings = ApiSettings()  # type: ignore[call-arg]
    parsed_business_id = BusinessId.parse(business_id)
    brand_kit = load_brand_kit_yaml(path, business_id=parsed_business_id, clock=SystemClock())

    engine = create_async_engine(settings.database_url.get_secret_value())
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session, session.begin():
            await SqlBrandKitRepository(session).save(brand_kit)
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    asyncio.run(_load(business_id=args.business_id, path=Path(args.file)))
    print(f"BRAND_KIT_LOADED business_id={args.business_id} file={args.file}")


if __name__ == "__main__":
    main()
