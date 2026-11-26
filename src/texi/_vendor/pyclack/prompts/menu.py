from __future__ import annotations

from typing import Any

from texi._vendor.pyclack.core import Menu
from texi._vendor.pyclack.core.prompt import CANCEL


async def menu(
    message: str, *options: str, screen: bool = False, **kwargs: Any
) -> str | object:
    """A boxed, centered single-select menu - an alternate visual style to
    `select`, ported from rich_menu (https://github.com/gbPagano/rich_menu).

    Usage:
        choice = await menu("Pick a fruit", "Apple", "Banana", "Cherry")
    """
    picker = Menu(*options, title=message, **kwargs)
    result = picker.ask(screen=screen)
    return CANCEL if result is None else result


async def multi_menu(
    message: str, *options: str, screen: bool = False, **kwargs: Any
) -> list[str] | object:
    """A boxed, centered multi-select menu - an alternate visual style to
    `multiselect`, ported from rich_menu (https://github.com/gbPagano/rich_menu).

    Usage:
        choices = await multi_menu("Pick fruits", "Apple", "Banana", "Cherry")
    """
    picker = Menu(*options, title=message, **kwargs)
    result = picker.ask_multiple(screen=screen)
    return CANCEL if result is None else result
