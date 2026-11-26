"""Shared rich Console instance used across pyclack."""

from rich.console import Console

console = Console(highlight=False, soft_wrap=False)
