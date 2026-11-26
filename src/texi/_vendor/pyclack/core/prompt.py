from __future__ import annotations

from collections.abc import Callable
from typing import Any

import readchar
from rich.live import Live
from rich.text import Text

from texi._vendor.pyclack.utils.console import console

# Constants
CANCEL = object()
KEYS = {"up", "down", "left", "right", "space", "enter"}
ALIASES = {"k": "up", "j": "down", "h": "left", "l": "right"}


def is_cancel(value: Any) -> bool:
    return value is CANCEL


class Prompt:
    def __init__(
        self,
        render: Callable[[Prompt], str | None],
        placeholder: str = "",
        initial_value: Any = None,
        validate: Callable[[Any], str | None] | None = None,
        debug: bool = False,
        track_value: bool = True,
    ):
        self._subscribers: dict[str, list[dict]] = {}
        self.render_fn = render
        self.placeholder = placeholder
        self.initial_value = initial_value
        self.validate = validate
        self.debug = debug
        self._track = track_value

        # State
        self.state = "initial"  # One of: initial, active, cancel, submit, error
        self.value = initial_value
        self.error = ""
        self._cursor = 0

        self.cols = console.size.width
        self._live: Live | None = None

    def on(self, event: str, callback: Callable[..., Any]) -> None:
        """Add an event listener."""
        if event not in self._subscribers:
            self._subscribers[event] = []
        self._subscribers[event].append({"cb": callback, "once": False})

    def once(self, event: str, callback: Callable[..., Any]) -> None:
        """Add a one-time event listener."""
        if event not in self._subscribers:
            self._subscribers[event] = []
        self._subscribers[event].append({"cb": callback, "once": True})

    def emit(self, event: str, *args: Any) -> None:
        """Emit an event to all listeners."""
        if event not in self._subscribers:
            return

        cleanup = []
        for sub in self._subscribers[event]:
            sub["cb"](*args)
            if sub["once"]:
                cleanup.append(sub)

        for sub in cleanup:
            self._subscribers[event].remove(sub)

    def handle_key(self, key: str) -> bool:
        """Handle a keypress. Returns True if should continue, False if should exit."""
        if self.state == "error":
            self.state = "active"

        # Special key handling
        if key == readchar.key.CTRL_C:
            self.state = "cancel"
            return False

        if key == readchar.key.ENTER:
            if self.validate:
                problem = self.validate(self.value)
                if problem:
                    self.error = problem
                    self.state = "error"
                    return True
            self.state = "submit"
            return False

        # Handle navigation keys
        if key in {readchar.key.UP, "k"}:
            self.emit("cursor", "up")
        elif key in {readchar.key.DOWN, "j"}:
            self.emit("cursor", "down")
        elif key in {readchar.key.LEFT, "h"}:
            self.emit("cursor", "left")
        elif key in {readchar.key.RIGHT, "l"}:
            self.emit("cursor", "right")

        # Handle regular input
        elif key == " ":
            self.emit("cursor", "space")
        elif key in "yYnN":
            self.emit("confirm", key.lower() == "y")
        elif key == "\t" and self.placeholder and not self.value:
            self.value = self.placeholder
            self._cursor = len(self.value)
            self.emit("value", self.value)

        if key and len(key) == 1:
            self.emit("key", key.lower())

        return True

    def _renderable(self) -> Text:
        return Text.from_markup(self.render_fn(self) or "")

    def render(self) -> None:
        """Push the current frame to the live display."""
        if self._live is not None:
            self._live.update(self._renderable(), refresh=True)

    async def prompt(self) -> str | object:
        """Start the prompt and return the final value."""
        try:
            if self.initial_value is not None and self._track:
                self.value = str(self.initial_value)
                self._cursor = len(self.value)

            with Live(
                self._renderable(),
                console=console,
                auto_refresh=False,
                transient=True,
            ) as live:
                self._live = live
                if self.state == "initial":
                    self.state = "active"

                while True:
                    try:
                        key = readchar.readkey()
                        if not self.handle_key(key):
                            break
                        self.render()
                    except KeyboardInterrupt:
                        self.state = "cancel"
                        break

            self._live = None

            if self.state in {"submit", "cancel"}:
                console.print(self._renderable(), end="")

            return self.value if self.state == "submit" else CANCEL

        finally:
            self._live = None
            self.emit(self.state, self.value)
            self._subscribers.clear()

    def close(self) -> None:
        """Clean up any active live display."""
        if self._live is not None:
            self._live.stop()
            self._live = None
