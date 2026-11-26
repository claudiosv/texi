"""Group functionality for pyclack prompts."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, TypeVar, Union

from .prompt import is_cancel

T = TypeVar("T", bound=dict[str, Any])
PromptResult = Union[Any, None]


class PromptGroupOptions[T: dict[str, Any]]:
    """Options for prompt groups."""

    def __init__(self, on_cancel: Callable[[dict[str, Any]], None] | None = None):
        """
        Initialize prompt group options.

        Args:
            on_cancel: Function to call when a prompt is canceled
        """
        self.on_cancel = on_cancel


async def group(
    prompts: dict[str, Callable[[dict[str, Any]], Awaitable[PromptResult]]],
    options: PromptGroupOptions | None = None,
) -> dict[str, Any]:
    """
    Define a group of prompts to be displayed and return results of objects within the group.

    Args:
        prompts: Dictionary of prompt functions
        options: Optional configuration for the prompt group

    Returns
    -------
        Dictionary of prompt results
    """
    results: dict[str, Any] = {}
    prompt_names = list(prompts.keys())

    for name in prompt_names:
        prompt = prompts[name]
        try:
            result = await prompt({"results": results})
        except Exception:
            raise

        # Pass the results to the on_cancel function
        # so the user can decide what to do with the results
        if options and options.on_cancel and is_cancel(result):
            results[name] = "canceled"
            options.on_cancel({"results": results})
            continue

        results[name] = result

    return results
