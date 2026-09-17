"""Conversores puros de argumentos de presentacion a DTOs de aplicacion
(T045). Sin esto cada handler repetiria la misma traduccion `WindowArgs` ->
`Window` / `PageArgs` -> `(limit, cursor)`."""

from __future__ import annotations

from safent_ads.mcp.application.dto import Window
from safent_ads.mcp.presentation.args import PageArgs, WindowArgs


def to_window(args: WindowArgs) -> Window:
    return Window(
        preset=args.preset, lag_days=args.lag_days, date_from=args.date_from, date_to=args.date_to
    )


def page_params(args: PageArgs) -> tuple[int, str | None]:
    return args.limit, args.cursor
