from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from texi._vendor.pyclack.core import ShimmerProgress


@asynccontextmanager
async def progress() -> AsyncGenerator[ShimmerProgress]:
    """Async context manager for a multi-phase shimmering progress bar.

    Usage:
        async with progress() as p:
            p.on_progress("Scanning files", current=10, total=100)
            p.on_progress("Scanning files", current=100, total=100)
            p.on_progress("Indexing", current=42)
    """
    prog = ShimmerProgress()
    try:
        prog.start()
        yield prog
    finally:
        prog.stop()
