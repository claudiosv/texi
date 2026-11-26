from __future__ import annotations

from rich.text import Text

from texi._vendor.pyclack.core import ConfirmPrompt, is_cancel
from texi._vendor.pyclack.utils.console import console
from texi._vendor.pyclack.utils.styling import (
    S_BAR,
    S_BAR_END,
    S_RADIO_ACTIVE,
    S_RADIO_INACTIVE,
    Color,
    symbol,
)


async def confirm(
    message: str, active: str = "Yes", inactive: str = "No", initial_value: bool = True
) -> bool | object:
    def render(prompt: ConfirmPrompt) -> str:
        title = f"{Color.gray(S_BAR)}\n{symbol(prompt.state)}  {message}\n"
        value = active if prompt.value else inactive

        if prompt.state == "submit":
            return f"{title}"
        if prompt.state == "cancel":
            return (
                f"{title}{Color.gray(S_BAR)}  "
                f"{Color.strikethrough(Color.dim(value))}\n"
                f"{Color.gray(S_BAR)}"
            )
        active_style = (
            f"{Color.green(S_RADIO_ACTIVE)} {active}"
            if prompt.value
            else f"{Color.dim(S_RADIO_INACTIVE)} {Color.dim(active)}"
        )
        inactive_style = (
            f"{Color.green(S_RADIO_ACTIVE)} {inactive}"
            if not prompt.value
            else f"{Color.dim(S_RADIO_INACTIVE)} {Color.dim(inactive)}"
        )
        return (
            f"{title}{Color.cyan(S_BAR)}  "
            f"{active_style} {Color.dim('/')} {inactive_style}\n"
            f"{Color.cyan(S_BAR_END)}\n"
        )

    prompt = ConfirmPrompt(
        render=render, active=active, inactive=inactive, initial_value=initial_value
    )
    result = await prompt.prompt()

    if is_cancel(result):
        return result

    console.print(Text.from_markup(f"{Color.gray(S_BAR)}  {Color.dim(result)}"))
    return result
