"""Fake TeX Live tools on PATH for tests: they record calls instead of running anything."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest


def _recorder(tool: str) -> str:
    return f"""#!/bin/sh
printf '%s|%s|TEXMFHOME=%s TEXMFVAR=%s TEXMFCONFIG=%s\\n' "{tool}" "$*" \\
  "$TEXMFHOME" "$TEXMFVAR" "$TEXMFCONFIG" >> "$SHIM_LOG"
exit ${{SHIM_EXIT_{tool.replace("-", "_")}:-0}}
"""


_KPSEWHICH = """#!/bin/sh
printf 'kpsewhich|%s\\n' "$*" >> "$SHIM_LOG"
case "$1" in
  -var-value=*) var=${1#-var-value=}; eval "echo \\"\\$KPSE_$var\\"" ;;
  *) exit 1 ;;
esac
"""

_TLMGR = """#!/bin/sh
printf 'tlmgr|%s\\n' "$*" >> "$SHIM_LOG"
echo "tlmgr must never be called" >&2
exit 99
"""


@dataclass
class Shims:
    """Recorded tool calls."""

    log: Path

    def calls(self, tool: str | None = None) -> list[tuple[str, str, str]]:
        """``(tool, args, env)`` of each call, in order; ``env`` is ``TEXMFHOME=..|..``."""
        out: list[tuple[str, str, str]] = []
        if not self.log.exists():
            return out
        for line in self.log.read_text().splitlines():
            name, _, rest = line.partition("|")
            args, _, env = rest.partition("|")
            if tool is None or name == tool:
                out.append((name, args, env))
        return out


def install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Shims:
    """Put fake tlmgr, kpsewhich, mktexlsr, updmap-* and fmtutil-* first on PATH."""
    bin_dir = tmp_path / "shims"
    bin_dir.mkdir()
    log = tmp_path / "shim.log"

    def script(name: str, text: str) -> None:
        p = bin_dir / name
        p.write_text(text)
        p.chmod(p.stat().st_mode | stat.S_IXUSR)

    script("tlmgr", _TLMGR)
    script("kpsewhich", _KPSEWHICH)
    for tool in (
        "mktexlsr",
        "updmap-user",
        "updmap-sys",
        "fmtutil-user",
        "fmtutil-sys",
    ):
        script(tool, _recorder(tool))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("SHIM_LOG", str(log))
    return Shims(log)
