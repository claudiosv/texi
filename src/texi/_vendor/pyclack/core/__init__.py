from dataclasses import dataclass
from typing import Any

from .confirm import ConfirmPrompt as ConfirmPrompt
from .group import PromptGroupOptions as PromptGroupOptions
from .group import group as group
from .menu import Menu as Menu
from .multiselect import MultiSelectPrompt as MultiSelectPrompt
from .password import PasswordPrompt as PasswordPrompt
from .prompt import is_cancel as is_cancel
from .select import SelectPrompt as SelectPrompt
from .select_key import SelectKeyPrompt as SelectKeyPrompt
from .shimmer_progress import ShimmerProgress as ShimmerProgress
from .spinner import Spinner as Spinner
from .text import MultilineTextPrompt as MultilineTextPrompt
from .text import TextPrompt as TextPrompt


@dataclass
class Option:
    value: Any
    label: str = ""
    hint: str = ""
