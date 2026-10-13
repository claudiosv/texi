"""Thin `sh` wrappers around the TeX Live maintenance tools."""

import sh

_ENGINE_FMT = {
    "pdflatex": ("pdftex", "pdflatex.fmt"),
    "lualatex": ("luahbtex", "lualatex.fmt"),
    "xelatex": ("xetex", "xelatex.fmt"),
}


def _cmd(name: str) -> sh.Command:
    return sh.Command(name)


def which(name: str) -> str:
    return str(sh.which(name)).strip()


def kpsewhich(*args: str) -> list[str]:
    """Run kpsewhich; returns one path per output line."""
    # exit 1 just means "not found"
    return str(_cmd("kpsewhich")(*args, _ok_code=[0, 1])).splitlines()


def engine_version(engine: str = "pdflatex") -> str:
    return str(_cmd(engine)("--version")).splitlines()[0]


def _run_sys(name: str, args: tuple[str, ...], sudo: bool) -> str:
    cmd = _cmd(name)
    return str(sh.sudo(cmd, *args) if sudo else cmd(*args))


def fmtutil_sys(*args: str, sudo: bool = True) -> str:
    """Rebuild system formats (`--all` by default); via sudo unless the tree is writable."""
    return _run_sys("fmtutil-sys", args or ("--all",), sudo)


def fmtutil_user(*args: str) -> str:
    """Rebuild user-level formats (`--all` by default)."""
    return str(_cmd("fmtutil-user")(*(args or ("--all",))))


def updmap_sys(*args: str, sudo: bool = True) -> str:
    """Regenerate system font maps; via sudo unless the tree is writable."""
    return _run_sys("updmap-sys", args, sudo)


def updmap_user(*args: str) -> str:
    """Regenerate user-level font maps."""
    return str(_cmd("updmap-user")(*args))


def mktexlsr(*dirs: str, sudo: bool = False) -> str:
    """Rebuild ls-R filename databases (all texmf trees by default)."""
    return _run_sys("mktexlsr", dirs, sudo)


def luaotfload_update(*args: str) -> str:
    """Rebuild the LuaTeX font name database (`--update` by default)."""
    return str(_cmd("luaotfload-tool")(*(args or ("--update",))))


def tlmgr_update(*args: str, sudo: bool = True) -> str:
    """`tlmgr update --self --all` by default; via sudo unless the tree is writable."""
    return _run_sys("tlmgr", ("update", *(args or ("--self", "--all"))), sudo)


def format_paths(engine: str = "pdflatex") -> list[str]:
    """All visible copies of an engine's format; >1 means a stale user format may shadow the system one."""
    kpse_engine, fmt = _ENGINE_FMT[engine]
    return kpsewhich("--all", f"--engine={kpse_engine}", fmt)


def diagnose(engine: str = "pdflatex") -> dict[str, list[str] | str]:
    """The checks from the 'rebuild formats' recipe: all paths should share one TeX Live."""
    return {
        "which engine": which(engine),
        "which fmtutil-sys": which("fmtutil-sys"),
        "expl3.sty": kpsewhich("expl3.sty"),
        "latex.fmt": kpsewhich("latex.fmt"),
        "version": engine_version(engine),
        "formats": format_paths(engine),
    }
