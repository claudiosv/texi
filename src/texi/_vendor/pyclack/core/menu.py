from __future__ import annotations

import readchar
from rich.align import Align, AlignMethod
from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.rule import Rule
from rich.text import Text

from texi._vendor.pyclack.utils.console import console as default_console


def _get_key() -> str | None:
    """Read one keypress and normalize it, reusing pyclack's own readchar
    key constants (and vi-style h/j/k/l) instead of hand-matching raw
    platform-specific escape sequences.
    """
    key = readchar.readkey()
    if key == readchar.key.ENTER:
        return "enter"
    if key == readchar.key.CTRL_C:
        return "cancel"
    if key == readchar.key.ESC:
        return "cancel"
    if key in {readchar.key.DOWN, "j"}:
        return "down"
    if key in {readchar.key.UP, "k"}:
        return "up"
    if key in {readchar.key.LEFT, "h"}:
        return "left"
    if key in {readchar.key.RIGHT, "l"}:
        return "right"
    if key == " ":
        return "space"
    return None


class Menu:
    """A boxed, centered single/multi-select menu.

    Ported from rich_menu (https://github.com/gbPagano/rich_menu) onto
    pyclack's own conventions: reads keys through `readchar` (already a
    pyclack dependency, no need for `click`), renders through the shared
    pyclack console, and returns `None` on cancel (Esc/Ctrl+C) instead of
    killing the process with `exit()`, so callers can handle it like any
    other pyclack prompt result.
    """

    def __init__(
        self,
        *options: str,
        start_index: int = 0,
        title: str = "MENU",
        rule: bool = True,
        panel: bool = True,
        panel_title: str = "",
        color: str = "bold green",
        align: AlignMethod = "center",
        selection_char: str = ">",
        selected_char: str = "*",
        selected_color: str = "bold blue",
        highlight_color: str = "",
        console=None,
    ):
        self.options = options
        self.index = start_index
        self.title = title
        self.rule = rule
        self.panel = panel
        self.panel_title = panel_title
        self.color = color
        self.align = align
        self.selection_char = selection_char
        self.highlight_color = highlight_color
        self.selected_char = selected_char
        self.selected_color = selected_color
        self.selected_options: list[str] = []
        self.console = console or default_console

    def _update_index(self, key: str | None) -> None:
        if key == "down":
            self.index += 1
        elif key == "up":
            self.index -= 1

        if self.index > len(self.options) - 1:
            self.index = 0
        elif self.index < 0:
            self.index = len(self.options) - 1

    @property
    def _group(self) -> Group:
        menu = Text(justify="left")

        current = Text(self.selection_char + " ", self.color)
        not_selected = Text(" " * (len(self.selection_char) + 1))
        selected = Text(self.selected_char + " ", self.selected_color)

        for idx, option in enumerate(self.options):
            if idx == self.index and option in self.selected_options:
                # current cursor row, already selected (multi-select mode)
                menu.append(
                    Text.assemble(current, Text(option + "\n", self.selected_color))
                )
            elif idx == self.index:
                # current cursor row (single-select mode)
                menu.append(
                    Text.assemble(current, Text(option + "\n", self.highlight_color))
                )
            elif option in self.selected_options:
                # selected, not under the cursor (multi-select mode)
                menu.append(
                    Text.assemble(selected, Text(option + "\n", self.selected_color))
                )
            else:
                menu.append(Text.assemble(not_selected, option + "\n"))
        menu.rstrip()

        if self.panel:
            menu = Panel.fit(menu)
            menu.title = Text(self.panel_title, self.color)
        if self.title:
            group = Group(
                Rule(self.title, style=self.color) if self.rule else self.title,
                Align(menu, self.align),
            )
        else:
            group = Group(Align(menu, self.align))

        return group

    def ask(self, screen: bool = True) -> str | None:
        """Run the single-select loop. Returns None if cancelled."""
        with Live(
            self._group,
            console=self.console,
            auto_refresh=False,
            screen=screen,
            transient=not screen,
        ) as live:
            live.update(self._group, refresh=True)
            while True:
                try:
                    key = _get_key()
                    if key == "enter":
                        break
                    if key == "cancel":
                        return None

                    self._update_index(key)
                    live.update(self._group, refresh=True)
                except KeyboardInterrupt, EOFError:
                    return None

        return self.options[self.index]

    def ask_multiple(self, screen: bool = True) -> list[str] | None:
        """Run the multi-select loop. Returns None if cancelled."""
        self.selected_options = []
        with Live(
            self._group,
            console=self.console,
            auto_refresh=False,
            screen=screen,
            transient=not screen,
        ) as live:
            live.update(self._group, refresh=True)
            while True:
                try:
                    key = _get_key()
                    if key == "enter":
                        break
                    if key == "cancel":
                        return None
                    if key in {"down", "up"}:
                        self._update_index(key)
                    elif key == "space":
                        current = self.options[self.index]
                        if current in self.selected_options:
                            self.selected_options.remove(current)
                        else:
                            self.selected_options.append(current)

                    live.update(self._group, refresh=True)
                except KeyboardInterrupt, EOFError:
                    return None

        return self.selected_options
