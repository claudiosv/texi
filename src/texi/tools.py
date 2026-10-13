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


def fmtutil_sys(*args: str) -> str:
    """Rebuild system formats (`--all` by default). Needs root, so runs via sudo."""
    return str(sh.sudo(_cmd("fmtutil-sys"), *(args or ("--all",))))


def fmtutil_user(*args: str) -> str:
    """Rebuild user-level formats (`--all` by default)."""
    return str(_cmd("fmtutil-user")(*(args or ("--all",))))


def updmap_sys(*args: str) -> str:
    """Regenerate system font maps. Needs root, so runs via sudo."""
    return str(sh.sudo(_cmd("updmap-sys"), *args))


def updmap_user(*args: str) -> str:
    """Regenerate user-level font maps."""
    return str(_cmd("updmap-user")(*args))


def mktexlsr(*dirs: str, sudo: bool = False) -> str:
    """Rebuild ls-R filename databases (all texmf trees by default)."""
    cmd = _cmd("mktexlsr")
    return str(sh.sudo(cmd, *dirs) if sudo else cmd(*dirs))


def luaotfload_update(*args: str) -> str:
    """Rebuild the LuaTeX font name database (`--update` by default)."""
    return str(_cmd("luaotfload-tool")(*(args or ("--update",))))


def tlmgr_update(*args: str) -> str:
    """`sudo tlmgr update --self --all` by default."""
    return str(sh.sudo(_cmd("tlmgr"), "update", *(args or ("--self", "--all"))))


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
