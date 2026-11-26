from __future__ import annotations

import math
import time
from dataclasses import dataclass
from types import TracebackType
from typing import TYPE_CHECKING, Self

from rich.progress import Progress, ProgressColumn, Task, TaskID, TextColumn
from rich.text import Text

from texi._vendor.pyclack.utils.console import console as default_console
from texi._vendor.pyclack.utils.styling import S_BAR, UNICODE

if TYPE_CHECKING:
    from rich.console import Console


@dataclass(frozen=True)
class Glyphs:
    """Glyph set for ShimmerProgress, following pyclack's UNICODE/ASCII split."""

    ok: str
    err: str
    info: str
    warn: str
    spinner: list[str]
    bar_filled: str
    bar_empty: str
    phase_done: str
    dash: str


UNICODE_GLYPHS = Glyphs(
    ok="✓",
    err="✗",
    info="ℹ",
    warn="⚠",
    spinner=["·", "✢", "✳", "✶", "✻", "✽"],
    bar_filled="█",
    bar_empty="░",
    phase_done="◆",
    dash="—",
)

ASCII_GLYPHS = Glyphs(
    ok="[OK]",
    err="[ERR]",
    info="[i]",
    warn="[!]",
    spinner=[".", "*", "+", "x", "o", "O"],
    bar_filled="#",
    bar_empty="-",
    phase_done="*",
    dash="-",
)


def get_glyphs() -> Glyphs:
    """Pick the glyph set matching pyclack's own unicode-support detection."""
    return UNICODE_GLYPHS if UNICODE else ASCII_GLYPHS


def lerp(a: float, b: float, t: float) -> int:
    """Linear interpolation helper.

    Returns
    -------
    int
        The value at `t` between `a` and `b`, rounded to the nearest int.
    """
    return round(a + (b - a) * t)


class ShimmerSpinnerColumn(ProgressColumn):
    """A progress column that cycles spinner glyphs.

    Applies the sine-wave shimmer color effect over time.
    """

    def __init__(self, start_time: float, glyphs: Glyphs) -> None:
        self.start_time = start_time
        self.glyphs = glyphs
        super().__init__()

    def render(self, task: Task) -> Text:
        frame = int((time.time() - self.start_time) / 0.150)
        t = (math.sin(frame * 2 * math.pi / 13) + 1) / 2

        r = lerp(160, 251, t)
        g = lerp(100, 191, t)
        b = lerp(9, 36, t)

        frames_per_glyph = 3
        glyph_idx = (frame // frames_per_glyph) % len(self.glyphs.spinner)
        glyph = self.glyphs.spinner[glyph_idx]

        return Text(glyph, style=f"bold rgb({r},{g},{b})")


class ShimmerBarColumn(ProgressColumn):
    """A progress column that draws a bar with a sweeping shimmer light effect."""

    def __init__(self, start_time: float, glyphs: Glyphs) -> None:
        self.start_time = start_time
        self.glyphs = glyphs
        super().__init__()

    def render(self, task: Task) -> Text:
        if task.total is None:
            return Text("")

        frame = int((time.time() - self.start_time) / 0.150)
        percent = (
            min(1.0, max(0.0, task.percentage / 100.0)) if task.percentage else 0.0
        )

        bar_width = 25
        filled = round(bar_width * percent)
        empty = bar_width - filled

        if filled == 0:
            return Text(self.glyphs.bar_empty * empty, style="dim")

        cycle_frames = 24
        shimmer_pos = ((frame % cycle_frames) / cycle_frames) * (filled + 6) - 3
        shimmer_width = 3

        text = Text()
        for i in range(filled):
            dist = abs(i - shimmer_pos)
            t = max(0.0, 1.0 - dist / shimmer_width)
            r = lerp(160, 251, t)
            g = lerp(100, 191, t)
            b = lerp(9, 36, t)
            text.append(self.glyphs.bar_filled, style=f"bold rgb({r},{g},{b})")

        if empty > 0:
            text.append(self.glyphs.bar_empty * empty, style="dim")

        return text


class ShimmerStatsColumn(ProgressColumn):
    """A progress column handling percentage rendering or indeterminate counts."""

    def __init__(self, glyphs: Glyphs) -> None:
        self.glyphs = glyphs
        super().__init__()

    def render(self, task: Task) -> Text:
        if task.total is not None:
            percentage = int(task.percentage) if task.percentage else 0
            return Text(f" {percentage}%")

        count = task.fields.get("current_count", 0)
        if count > 0:
            return Text(f" {count:,} found")

        return Text("")


class ShimmerProgress:
    """Multi-phase progress display driven by phase/count/total updates.

    Built directly on `rich.progress.Progress` and `rich.live.Live` (via
    `Progress.start`/`stop`) rather than a hand-rolled redraw loop; each
    phase transition prints a finished-phase line above the live bar and
    starts a fresh task for the next phase.
    """

    def __init__(self, console: Console | None = None) -> None:
        self.console = console or default_console
        self.glyphs = get_glyphs()
        self.start_time = time.time()

        self._progress = Progress(
            TextColumn(f"[dim]{S_BAR}[/dim] "),
            ShimmerSpinnerColumn(self.start_time, self.glyphs),
            TextColumn(" {task.description}"),
            ShimmerBarColumn(self.start_time, self.glyphs),
            ShimmerStatsColumn(self.glyphs),
            console=self.console,
            transient=True,  # Removes the task bar on finish naturally
            refresh_per_second=20,  # Equivalent to 50ms interval loop
        )
        self._current_task_id: TaskID | None = None
        self._last_phase: str = ""
        self._last_count: int = 0
        self._last_total: int = 0
        self._running: bool = False

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.stop()

    def start(self) -> None:
        """Start the rich Progress live context."""
        self._progress.start()
        self._running = True

    def _finish_phase(self, phase: str, count: int, total: int) -> None:
        if not phase:
            return

        detail = ""
        if total > 0:
            detail = f" {self.glyphs.dash} done"
        elif count > 0:
            detail = f" {self.glyphs.dash} {count:,} found"

        # Prints directly above the live progressing task
        self._progress.console.print(
            f"[dim]{S_BAR}[/dim]  "
            f"[green]{self.glyphs.phase_done}[/green] "
            f"{phase}{detail}"
        )

    def on_progress(self, phase: str, current: int = 0, total: int = 0) -> None:
        """
        Update the progress bar, transitioning to a new phase if it changed.

        Parameters
        ----------
        phase : str
            The name of the current execution phase.
        current : int
            The current progress or count.
        total : int
            The total items (if 0, treats the progress as indeterminate).
        """
        if not self._running:
            self.start()

        # Handle phase changes
        if phase != self._last_phase and self._last_phase:
            self._finish_phase(self._last_phase, self._last_count, self._last_total)
            if self._current_task_id is not None:
                self._progress.remove_task(self._current_task_id)
            self._current_task_id = None

        self._last_phase = phase
        self._last_count = current
        self._last_total = total

        target_total = total if total > 0 else None
        target_completed = current if total > 0 else 0

        if self._current_task_id is None:
            self._current_task_id = self._progress.add_task(
                f"{phase}...",
                total=target_total,
                completed=target_completed,
                current_count=current,
            )
        else:
            self._progress.update(
                self._current_task_id,
                description=f"{phase}...",
                completed=target_completed,
                total=target_total,
                current_count=current,
            )

    def stop(self) -> None:
        """Stop the live display and finalize the last phase."""
        if self._running:
            if self._last_phase:
                self._finish_phase(self._last_phase, self._last_count, self._last_total)
            self._progress.stop()
            self._running = False
