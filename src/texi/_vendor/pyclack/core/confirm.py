from __future__ import annotations

from collections.abc import Callable

from .prompt import *


class ConfirmPrompt(Prompt):
    def __init__(
        self,
        render: Callable[[ConfirmPrompt], str | None],
        active: str = "Yes",
        inactive: str = "No",
        initial_value: bool = False,
        debug: bool = False,
    ):
        super().__init__(
            render=render,
            initial_value=initial_value,
            debug=debug,
            track_value=False,  # Important: we're handling value tracking differently for confirm
        )

        self.active = active
        self.inactive = inactive
        self.value = initial_value

        # Set up event handlers
        self.on("value", self._handle_value)
        self.on("confirm", self._handle_confirm)
        self.on("cursor", self._handle_cursor)

    @property
    def cursor(self) -> int:
        return 0 if self.value else 1

    @property
    def _value(self) -> bool:
        return self.cursor == 0

    def _handle_value(self, *args) -> None:
        """Handle value changes."""
        self.value = self._value

    def _handle_confirm(self, confirm: bool) -> None:
        """Handle confirmation (y/n key press)."""
        self.value = confirm
        self.state = "submit"

    def _handle_cursor(self, direction: str) -> None:
        """Handle cursor movement (left/right/up/down)."""
        if direction in {"left", "right", "up", "down"}:
            self.value = not self.value

    def handle_key(self, key: str) -> bool:
        """Override key handling for confirm-specific behavior."""
        if key.lower() in {"y", "n"}:
            self._handle_confirm(key.lower() == "y")
            return False

        return super().handle_key(key)
