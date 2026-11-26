from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from .prompt import *

T = TypeVar("T")


@dataclass
class Option:
    value: Any
    label: str = ""  # Optional label, will use str(value) if not provided


class SelectPrompt(Prompt):
    def __init__(
        self,
        render: Callable[[SelectPrompt], str],
        options: list[Option],
        initial_value: Any = None,
        validate: Callable[[Any], str | None] | None = None,
        debug: bool = False,
    ):
        super().__init__(
            render=render, validate=validate, debug=debug, track_value=False
        )

        self.options = options
        self._cursor = 0

        # Set initial cursor position if initial_value provided
        if initial_value is not None:
            for i, opt in enumerate(options):
                if opt.value == initial_value:
                    self._cursor = i
                    break

        self.value = self.options[self._cursor].value

    @property
    def cursor(self) -> int:
        return self._cursor

    def handle_key(self, key: str) -> bool:
        """Handle keyboard input."""
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

        # Handle arrow keys
        if key in {readchar.key.UP, "k"}:
            self._cursor = (self._cursor - 1) % len(self.options)
            self.value = self.options[self._cursor].value

        elif key in {readchar.key.DOWN, "j"}:
            self._cursor = (self._cursor + 1) % len(self.options)
            self.value = self.options[self._cursor].value

        return True
