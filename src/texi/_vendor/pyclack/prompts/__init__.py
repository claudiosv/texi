from __future__ import annotations

from rich.text import Text

from texi._vendor.pyclack.core import Option as Option
from texi._vendor.pyclack.core import is_cancel as is_cancel
from texi._vendor.pyclack.utils.console import console
from texi._vendor.pyclack.utils.styling import (
    S_BAR,
    S_BAR_END,
    S_BAR_H,
    S_BAR_START,
    S_CONNECT_LEFT,
    S_CORNER_BOTTOM_RIGHT,
    S_CORNER_TOP_RIGHT,
    S_STEP_SUBMIT,
    Color,
    visible_len,
)

from .confirm import confirm as confirm
from .menu import menu as menu
from .menu import multi_menu as multi_menu
from .mutliselect import multiselect as multiselect
from .password import password as password
from .progress import progress as progress
from .select import select as select
from .spinner import spinner as spinner
from .spinner import with_spinner as with_spinner
from .text import multiline_text as multiline_text
from .text import text as text


def create_note(message: str = "", title: str = "") -> str:
    lines = f"\n{message}\n".split("\n")
    title_len = visible_len(title)
    max_len = max(*(visible_len(ln) for ln in lines), title_len) + 2

    formatted_lines = [
        f"{Color.gray(S_BAR)}  {Color.dim(ln)}{' ' * (max_len - visible_len(ln))}{Color.gray(S_BAR)}"
        for ln in lines
    ]

    note_display = "\n".join(formatted_lines)

    return (
        f"{Color.gray(S_BAR)}\n"
        f"{Color.reset(S_STEP_SUBMIT)}  {Color.reset(title)} {Color.gray(S_BAR_H * max(max_len - title_len - 1, 1))}{Color.gray(S_CORNER_TOP_RIGHT)}\n"
        f"{note_display}\n"
        f"{Color.gray(S_CONNECT_LEFT)}{Color.gray(S_BAR_H * (max_len + 2))}{Color.gray(S_CORNER_BOTTOM_RIGHT)}"
    )


def note(
    message: str | None = None, title: str = "", content: list | None = None
) -> str:
    if content is None:
        content = []
    console.print(
        Text.from_markup(
            create_note(
                message=message or "\n".join(content),
                title=title or "Next steps.",
            )
        )
    )


def intro(title: str = "", options=None) -> None:
    """Display intro with optional title and styling.

    Args:
        title: Optional title text
        options: Dict with 'color' styling (defaults to gray)
    """
    if options is None:
        options = {"color": Color.gray}

    color = options.get("color", Color.gray)
    console.clear()
    console.print(Text.from_markup(f"{color(S_BAR_START)}  {title}"))


def outro(message: str = "", options=None) -> None:
    """Display outro with optional message and styling.

    Args:
        message: Optional message text
        options: Dict with 'color' styling (defaults to gray)
    """
    if options is None:
        options = {"color": Color.gray}

    color = options.get("color", Color.gray)
    console.print(Text.from_markup(f"{color(S_BAR)}\n{color(S_BAR_END)}  {message}\n"))


def link(url, label=None, options=None):
    """Generate a terminal hyperlink with optional styling.

    Args:
        url: The URL to link to
        label: Optional text to display (defaults to URL if None)
        options: Dict with 'color' and 'bg_color' keys for styling
    """
    if options is None:
        options = {"color": Color.cyan, "bg_color": None}

    label = label or url
    color = options.get("color")

    link_markup = f"[link={url}]{label}[/link]"
    return color(link_markup) if color else link_markup
