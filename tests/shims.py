"""Fake TeX Live tools on PATH for tests: they record calls instead of running anything."""

from __future__ import annotations

import os
import stat
import sys
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


def _fmtutil(tool: str) -> str:
    return f"""#!{sys.executable}
import os, pathlib, sys
args = sys.argv[1:]
with open(os.environ['SHIM_LOG'], 'a') as log:
    log.write('{tool}|' + ' '.join(args) + '|TEXMFHOME=' + os.environ.get('TEXMFHOME', '') + ' TEXMFVAR=' + os.environ.get('TEXMFVAR', '') + ' TEXMFCONFIG=' + os.environ.get('TEXMFCONFIG', '') + '\\n')
code = int(os.environ.get('SHIM_EXIT_{tool.replace("-", "_")}', '0'))
selected = {{args[i+1] for i, value in enumerate(args[:-1]) if value == '--byfmt'}}
status = next((a.split('=', 1)[1] for a in args if a.startswith('--status-file=')), None)
if status:
    override = dict(item.split('=', 1) for item in os.environ.get('SHIM_FMT_STATUSES', '').split(',') if item)
    for root in [os.environ.get('TEXMFDIST'), os.environ.get('TEXMFHOME')]:
        if not root: continue
        cnf = pathlib.Path(root) / 'web2c/fmtutil.cnf'
        if not cnf.exists(): continue
        for line in cnf.read_text().splitlines():
            parts = line.split()
            if len(parts) < 2 or parts[0] not in selected: continue
            name, engine = parts[:2]
            state = override.get(name, 'FAILURE' if code else 'SUCCESS')
            with open(status, 'a') as output: output.write(state + ' ' + name + ' ' + engine + '\\n')
sys.exit(code)
"""


def _updmap(tool: str) -> str:
    script = _recorder(tool)
    return script.replace(
        "exit $",
        'if [ "${SHIM_EXIT_'
        + tool.replace("-", "_")
        + ':-0}" = "0" ]; then\nmkdir -p "$TEXMFVAR/fonts/map/pdftex/updmap"\necho "% generated" > "$TEXMFVAR/fonts/map/pdftex/updmap/pdftex.map"\nfi\nexit $',
    )


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
        script(
            tool,
            _fmtutil(tool)
            if tool.startswith("fmtutil")
            else _updmap(tool)
            if tool.startswith("updmap")
            else _recorder(tool),
        )
    script("pdftex", _recorder("pdftex"))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("SHIM_LOG", str(log))
    return Shims(log)
