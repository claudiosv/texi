from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any

from rich.text import Text

from texi._vendor.pyclack.utils.console import console


def is_unicode_supported() -> bool:
    """Check if terminal supports Unicode characters."""
    try:
        return bool(sys.stdout.encoding.lower().startswith("utf"))
    except Exception:
        return False


UNICODE = is_unicode_supported()


def s(unicode: str, fallback: str) -> str:
    """Select Unicode or fallback character based on terminal support."""
    return unicode if UNICODE else fallback


# Symbols
S_STEP_ACTIVE = s("◆", "*")
S_STEP_CANCEL = s("■", "x")
S_STEP_ERROR = s("▲", "x")
S_STEP_SUBMIT = s("◇", "o")

S_BAR_START = s("┌", "T")
S_BAR = s("│", "|")
S_BAR_END = s("└", "—")

S_RADIO_ACTIVE = s("●", ">")
S_RADIO_INACTIVE = s("○", " ")
S_CHECKBOX_ACTIVE = s("◻", "[•]")
S_CHECKBOX_SELECTED = s("◼", "[+]")
S_CHECKBOX_INACTIVE = s("◻", "[ ]")
S_PASSWORD_MASK = s("▪", "•")

S_BAR_H = s("─", "-")
S_CORNER_TOP_RIGHT = s("╮", "+")
S_CONNECT_LEFT = s("├", "+")
S_CORNER_BOTTOM_RIGHT = s("╯", "+")

S_INFO = s("●", "•")
S_SUCCESS = s("◆", "*")
S_WARN = s("▲", "!")
S_ERROR = s("■", "x")


class Color:
    """Style helpers that wrap text in rich console markup.

    Kept as the same call-style API pyclack's rendering code already uses
    (`Color.cyan("...")`), but instead of emitting raw ANSI escapes it emits
    rich markup tags, which the prompt renderers understand via
    `rich.text.Text.from_markup`.
    """

    @staticmethod
    def _wrap(style: str, text: str) -> str:
        return f"[{style}]{text}[/{style}]"

    @staticmethod
    def gray(text: str) -> str:
        return Color._wrap("bright_black", text)

    @staticmethod
    def cyan(text: str) -> str:
        return Color._wrap("cyan", text)

    @staticmethod
    def red(text: str) -> str:
        return Color._wrap("red", text)

    @staticmethod
    def green(text: str) -> str:
        return Color._wrap("green", text)

    @staticmethod
    def yellow(text: str) -> str:
        return Color._wrap("yellow", text)

    @staticmethod
    def blue(text: str) -> str:
        return Color._wrap("blue", text)

    @staticmethod
    def magenta(text: str) -> str:
        return Color._wrap("magenta", text)

    @staticmethod
    def dim(text: str) -> str:
        return Color._wrap("dim", text)

    @staticmethod
    def inverse(text: str) -> str:
        return Color._wrap("reverse", text)

    @staticmethod
    def hidden(text: str) -> str:
        return Color._wrap("conceal", text)

    @staticmethod
    def strikethrough(text: str) -> str:
        return Color._wrap("strike", text)

    @staticmethod
    def reset(text: str) -> str:
        return Color._wrap("default", text)


def visible_len(text: str) -> int:
    """Cell-width of `text`, ignoring rich markup (replaces the old strip_ansi + len)."""
    return Text.from_markup(text).cell_len


def symbol(state: str) -> str:
    """Get the appropriate symbol for the current state."""
    if state in {"initial", "active"}:
        return Color.cyan(S_STEP_ACTIVE)
    if state == "cancel":
        return Color.red(S_STEP_CANCEL)
    if state == "error":
        return Color.yellow(S_STEP_ERROR)
    if state == "submit":
        return Color.green(S_STEP_SUBMIT)
    return ""


def limit_options(
    options: list[Any],
    cursor: int,
    max_items: int | None = None,
    style: Callable[[Any, bool], str] = lambda x, _: str(x),
) -> list[str]:
    """Limit visible options based on terminal size and cursor position."""
    param_max_items = max_items or float("inf")
    output_max_items = max(console.size.height - 4, 0)
    max_items = min(output_max_items, max(param_max_items, 5))

    window_start = 0
    if cursor >= window_start + max_items - 3:
        window_start = max(min(cursor - max_items + 3, len(options) - max_items), 0)
    elif cursor < window_start + 2:
        window_start = max(cursor - 2, 0)

    show_top_dots = max_items < len(options) and window_start > 0
    show_bottom_dots = max_items < len(options) and window_start + max_items < len(
        options
    )

    visible_options = options[window_start : window_start + max_items]
    result = []

    for i, option in enumerate(visible_options):
        if (i == 0 and show_top_dots) or (
            i == len(visible_options) - 1 and show_bottom_dots
        ):
            result.append(Color.dim("..."))
        else:
            result.append(style(option, i + window_start == cursor))

    return result
