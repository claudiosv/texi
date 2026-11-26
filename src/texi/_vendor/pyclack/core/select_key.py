from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar

from .prompt import *

if TYPE_CHECKING:
    from .select import Option

T = TypeVar("T")


class SelectKeyPrompt[T](Prompt):
    def __init__(
        self,
        render: Callable[[SelectKeyPrompt], str | None],
        options: list[Option],
        initial_value: Any = None,
        debug: bool = False,
    ):
        super().__init__(
            render=render,
            debug=debug,
            track_value=False,  # We handle value tracking ourselves
        )

        self.options = options
        self._cursor = 0

        # Extract the first character of each option value as the key
        self.keys = {
            str(opt.value)[0].lower(): opt.value for opt in options if str(opt.value)
        }

        # Set initial cursor position based on initial value
        if initial_value is not None:
            initial_key = str(initial_value)[0].lower()
            self._cursor = max(
                next(
                    (
                        i
                        for i, opt in enumerate(options)
                        if str(opt.value)[0].lower() == initial_key
                    ),
                    0,
                ),
                0,
            )

        # Set up event handler
        self.on("key", self._handle_key)

    @property
    def cursor(self) -> int:
        return self._cursor

    @cursor.setter
    def cursor(self, value: int) -> None:
        self._cursor = value

    def _handle_key(self, key: str) -> None:
        """Handle key press events."""
        if key not in self.keys:
            return

        self.value = self.keys[key]
        self.state = "submit"
        self.emit("submit")

    def handle_key(self, key: str) -> bool:
        """Override key handling for key-select specific behavior."""
        if key == readchar.key.ENTER:
            if self.validate:
                problem = self.validate(self.value)
                if problem:
                    self.error = problem
                    self.state = "error"
                    return True

            self.state = "submit"
            return False

        return super().handle_key(key)
