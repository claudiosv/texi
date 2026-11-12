"""The layout of .texenv/: the skeleton of a standalone clone tree or a usertree, and the
environment in which only that tree is on TeX's search path.

The packages come from the archive store (see :mod:`texi.archive`).
"""

import os
from collections.abc import Mapping
from pathlib import Path


def env_bin(env_dir: Path) -> Path | None:
    """The env's bin/<arch> directory, or None if the env doesn't exist yet."""
    return next(iter(sorted((env_dir / "bin").glob("*"))), None)


# The tlpkg/texlive.tlpdb of a usertree (the format `tlmgr init-usertree` writes):
# the system's 00texlive.installation with no architectures and setting_usertree:1.
_USERTREE_TLPDB = """\
name 00texlive.installation
category TLCore
shortdesc TeX Live configuration settings
longdesc This package serves on an installed system as a configuration
longdesc file. We have to remember these settings for additional package
longdesc installation, removal, etc. There are two types here: 1) If the
longdesc key starts with opt_ this is an option that can be set by the
longdesc user either at installation time or via tlmgr later. 2) If the
longdesc key starts with setting_ this value cannot be changed by the
longdesc user directly with tlmgr, but only by other actions. Currently
longdesc there are only two settings supported, and the second is only
longdesc used in unusual cases: 2a) setting_available_architectures
longdesc lists all available platforms and should be always present 2b)
longdesc setting_platform *overrides* the auto-detected platform. This
longdesc value will only be present when you forced a specific
longdesc architecture with -force-arch to install-tl The value of
longdesc __MASTER__ for the location field tells the installer to use
longdesc the present directory itself. For example, the DVD can be
longdesc mounted anywhere and we want the installer to work. Concerning
longdesc the 00* names: All packages starting with 00texlive are
longdesc ``virtual packages'', in the sense that no containers are
longdesc generated and these packages are never split into .src and .doc
longdesc sub-packages in the tlpdb.
depend opt_autobackup:1
depend opt_backupdir:tlpkg/backups
depend opt_create_formats:1
depend opt_desktop_integration:1
depend opt_file_assocs:1
depend opt_generate_updmap:0
depend opt_install_docfiles:0
depend opt_install_srcfiles:0
depend opt_location:{location}
depend opt_post_code:1
depend opt_sys_bin:/usr/local/bin
depend opt_sys_info:/usr/local/share/info
depend opt_sys_man:/usr/local/share/man
depend opt_w32_multi_user:1
depend setting_available_architectures:
depend setting_usertree:1

"""


def init_usertree(
    env_dir: Path, repository: str = "https://mirror.ctan.org/systems/texlive/tlnet"
) -> None:
    """Create an empty usertree skeleton.

    Writes tlpkg/texlive.tlpdb (only if missing) and creates tlpkg/tlpobj, web2c,
    texmf-var and texmf-config.
    """
    for sub in ("tlpkg/tlpobj", "web2c", "texmf-var", "texmf-config"):
        (env_dir / sub).mkdir(parents=True, exist_ok=True)
    db = env_dir / "tlpkg" / "texlive.tlpdb"
    if not db.exists():
        db.write_text(_USERTREE_TLPDB.format(location=repository), encoding="utf-8")


def is_usertree(env_dir: Path) -> bool:
    """True if the env's tlpdb carries `setting_usertree:1` (written by init_usertree)."""
    try:
        text = (env_dir / "tlpkg" / "texlive.tlpdb").read_text(encoding="utf-8")
    except OSError:
        return False
    return "\ndepend setting_usertree:1\n" in "\n" + text


# Packages of a standalone clone tree: plain TeX and the engines (scheme-minimal),
# LaTeX, latexmk, and the Perl modules and scripts that fmtutil and updmap need.
CLONE_ROOTS = [
    "scheme-minimal",
    "texlive.infra",
    "texlive-scripts",
    "texlive-scripts-extra",
    "latex",
    "latex-bin",
    "latexmk",
]

#: Written when a clone tree has all of CLONE_ROOTS; without it `texi sync` finishes the job.
CLONE_DONE = "tlpkg/texi-clone-complete"

# Variables that add search trees or files from outside the .texenv/.
SCRUBBED = (
    "TEXMFAUXTREES",
    "TEXMFDOTDIR",
    "TEXINPUTS",
    "BIBINPUTS",
    "BSTINPUTS",
    "TFMFONTS",
    "TEXFONTS",
    "OPENTYPEFONTS",
    "TTFONTS",
    "T1FONTS",
    "VFFONTS",
    "TEXFORMATS",
    "TEXPOOL",
    "TEXPSHEADERS",
)

#: Subdirectories a clone tree needs even when empty.
CLONE_DIRS = (
    "tlpkg/tlpobj",
    "texmf-var",
    "texmf-config",
    "texmf-local",
    "texmf-home",
)


def clone_env(
    env_dir: Path, arch: str, base: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Environment in which only `env_dir` is on TeX's search path."""
    base = os.environ if base is None else base
    root = env_dir.resolve()
    env = {k: v for k, v in base.items() if k not in SCRUBBED}
    env |= {
        "TEXMFROOT": str(root),
        "TEXMFDIST": str(root / "texmf-dist"),
        "TEXMFLOCAL": str(root / "texmf-local"),
        "TEXMFHOME": str(root / "texmf-home"),
        "TEXMFVAR": str(root / "texmf-var"),
        "TEXMFSYSVAR": str(root / "texmf-var"),
        "TEXMFCONFIG": str(root / "texmf-config"),
        "TEXMFSYSCONFIG": str(root / "texmf-config"),
        "TEXMFCNF": str(root / "texmf-dist" / "web2c"),
        "PATH": f"{root / 'bin' / arch}{os.pathsep}{base.get('PATH', '')}",
    }
    return env


def init_clone(
    env_dir: Path, repository: str = "https://mirror.ctan.org/systems/texlive/tlnet"
) -> None:
    """Create the empty skeleton of a standalone clone tree.

    Writes tlpkg/texlive.tlpdb (only if missing) with a `00texlive.installation`
    stanza that has no `setting_usertree`, so the tree counts as standalone.
    """
    for sub in CLONE_DIRS:
        (env_dir / sub).mkdir(parents=True, exist_ok=True)
    db = env_dir / "tlpkg" / "texlive.tlpdb"
    if not db.exists():
        text = _USERTREE_TLPDB.format(location=repository)
        db.write_text(text.replace("depend setting_usertree:1\n", ""), encoding="utf-8")
