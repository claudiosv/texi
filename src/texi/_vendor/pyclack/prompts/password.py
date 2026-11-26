from __future__ import annotations

from collections.abc import Callable

from rich.text import Text

from texi._vendor.pyclack.core import PasswordPrompt, is_cancel
from texi._vendor.pyclack.utils.console import console
from texi._vendor.pyclack.utils.styling import S_BAR, S_BAR_END, S_PASSWORD_MASK, Color, symbol


async def password(
    message: str,
    mask: str = S_PASSWORD_MASK,
    validate: Callable[[str], str | None] | None = None,
) -> str | object:
    def render(prompt: PasswordPrompt) -> str:
        title = f"{Color.gray(S_BAR)}\n{symbol(prompt.state)}  {message}\n"
        value = prompt.value_with_cursor
        masked = prompt.masked

        if prompt.state == "error":
            return (
                f"{title.rstrip()}\n"
                f"{Color.yellow(S_BAR)}  {masked}\n"
                f"{Color.yellow(S_BAR_END)}  {Color.yellow(prompt.error)}\n"
            )
        if prompt.state == "submit":
            return f"{title}"
        if prompt.state == "cancel":
            return (
                f"{title.rstrip()}\n"
                f"{Color.red(S_BAR)}  {masked}\n"
                f"{Color.red(S_BAR_END)}  {Color.red('Operation cancelled')}\n"
            )
        return f"{title}{Color.cyan(S_BAR)}  {value}\n{Color.cyan(S_BAR_END)}\n"

    prompt = PasswordPrompt(render=render, mask=mask, validate=validate)
    result = await prompt.prompt()

    if is_cancel(result):
        return result

    console.print(
        Text.from_markup(
            f"{Color.gray(S_BAR)}  {Color.dim(S_PASSWORD_MASK * len(result))}"
        )
    )
    return result
