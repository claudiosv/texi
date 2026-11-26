from __future__ import annotations

from typing import Any

from rich.text import Text

from texi._vendor.pyclack.core import MultiSelectPrompt, Option, is_cancel
from texi._vendor.pyclack.utils.console import console
from texi._vendor.pyclack.utils.styling import (
    S_BAR,
    S_BAR_END,
    S_CHECKBOX_ACTIVE,
    S_CHECKBOX_INACTIVE,
    S_CHECKBOX_SELECTED,
    Color,
    limit_options,
    symbol,
)


async def multiselect(
    message: str,
    options: list[Option],
    initial_values: list[Any] | None = None,
    max_items: int | None = None,
    required: bool = True,
    cursor_at: Any = None,
) -> list[Any] | object:

    def render(prompt: MultiSelectPrompt) -> str:
        def opt(option: Option, state: str) -> str:
            label = option.label or str(option.value)
            if state == "active":
                return (
                    f"{Color.cyan(S_CHECKBOX_ACTIVE)} {label} "
                    f"{(option.hint and Color.dim(f'({option.hint})')) or ''}"
                )
            if state == "selected":
                return f"{Color.green(S_CHECKBOX_SELECTED)} {Color.dim(label)}"
            if state == "cancelled":
                return Color.strikethrough(Color.dim(label))
            if state == "active-selected":
                return (
                    f"{Color.green(S_CHECKBOX_SELECTED)} {label} "
                    f"{(option.hint and Color.dim(f'({option.hint})')) or ''}"
                )
            if state == "submitted":
                return Color.dim(label)
            return f"{Color.dim(S_CHECKBOX_INACTIVE)} {Color.dim(label)}"

        title = f"{Color.gray(S_BAR)}\n{symbol(prompt.state)}  {message}\n"

        if prompt.state == "submit":
            selected = [opt for opt in prompt.options if opt.value in prompt.value]
            [opt.label for opt in selected]
            return f"{Color.gray(S_BAR)}\n{symbol(prompt.state)}  {message}\n"

        if prompt.state == "cancel":
            styled_options = limit_options(
                options=prompt.options,
                cursor=prompt.cursor,
                max_items=max_items,
                style=lambda item, active: opt(
                    item, "cancelled" if item.value in prompt.value else "inactive"
                ),
            )
            return (
                f"{title}{Color.red(S_BAR)}  "
                f"{f'\n{Color.red(S_BAR)}  '.join(styled_options)}\n"
                f"{Color.red(S_BAR_END)}  {Color.red('Operation cancelled')}\n"
            )
        if prompt.state == "error":
            footer = prompt.error.split("\n")
            footer = [
                f"{Color.yellow(S_BAR_END)}  {Color.yellow(footer[0])}",
                *[f"   {line}" for line in footer[1:]],
            ]
            styled_options = limit_options(
                options=prompt.options,
                cursor=prompt.cursor,
                max_items=max_items,
                style=lambda item, active: opt(
                    item,
                    (
                        "active-selected"
                        if active and item.value in prompt.value
                        else (
                            "selected"
                            if item.value in prompt.value
                            else "active"
                            if active
                            else "inactive"
                        )
                    ),
                ),
            )
            return (
                f"{title}{Color.yellow(S_BAR)}  "
                f"{f'\n{Color.yellow(S_BAR)}  '.join(styled_options)}\n"
                f"{'\n'.join(footer)}\n"
            )
        styled_options = limit_options(
            options=prompt.options,
            cursor=prompt.cursor,
            max_items=max_items,
            style=lambda item, active: opt(
                item,
                (
                    "active-selected"
                    if active and item.value in prompt.value
                    else (
                        "selected"
                        if item.value in prompt.value
                        else "active"
                        if active
                        else "inactive"
                    )
                ),
            ),
        )
        return (
            f"{title}{Color.cyan(S_BAR)}  "
            f"{f'\n{Color.cyan(S_BAR)}  '.join(styled_options)}\n"
            f"{Color.cyan(S_BAR_END)}\n"
        )

    prompt = MultiSelectPrompt(
        render=render,
        options=options,
        initial_values=initial_values,
        required=required,
        cursor_at=cursor_at,
        debug=False,
    )
    result = await prompt.prompt()

    if is_cancel(result):
        return result

    # Print final state
    selected = [opt for opt in options if opt.value in result]
    console.print(
        Text.from_markup(
            f"{Color.gray(S_BAR)}  {Color.dim(', '.join(opt.label for opt in selected))}"
        )
    )

    return result
