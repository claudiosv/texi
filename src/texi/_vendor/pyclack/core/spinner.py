from __future__ import annotations

from rich.live import Live
from rich.spinner import SPINNERS
from rich.spinner import Spinner as RichSpinner
from rich.text import Text

from texi._vendor.pyclack.utils.console import console
from texi._vendor.pyclack.utils.styling import S_BAR, UNICODE, symbol

# Register pyclack's own ASCII fallback frames alongside rich's built-in
# spinner set, so the non-unicode path still uses rich's Spinner machinery
# instead of a hand-rolled animation loop.
if "pyclackAscii" not in SPINNERS:
    SPINNERS["pyclackAscii"] = {"interval": 120, "frames": ["•", "o", "O", "0"]}

_SPINNER_NAME = "circleHalves" if UNICODE else "pyclackAscii"


class Spinner:
    """Terminal spinner for loading states, built on rich's Live + Spinner."""

    def __init__(self) -> None:
        self.message = ""
        self.active = False
        self._live: Live | None = None

    def start(self, message: str = ""):
        """Start the spinner with an optional message."""
        self.message = message.rstrip(".")
        self.active = True

        console.print(Text.from_markup(f"[bright_black]{S_BAR}[/bright_black]"))

        renderable = RichSpinner(_SPINNER_NAME, text=self.message, style="magenta", speed=0.4)
        self._live = Live(
            renderable, console=console, transient=True, refresh_per_second=12.5
        )
        self._live.start()

    def stop(self, message: str | None = None, code: int = 0):
        """Stop the spinner and show final message."""
        self.active = False
        if self._live is not None:
            self._live.stop()
            self._live = None

        final_message = message or self.message
        step = symbol({0: "submit", 1: "cancel"}.get(code, "error"))

        console.print(Text.from_markup(f"{step}  {final_message}"))

    def update(self, message: str):
        """Update the spinner message."""
        self.message = message.rstrip(".")
        if self._live is not None and isinstance(self._live.renderable, RichSpinner):
            self._live.renderable.update(text=self.message)
