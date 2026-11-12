"""Build a standalone, tlmgr-managed TeX Live tree (.texenv/) from the system installation.

Copies only the files (binfiles + runfiles) of a minimal package set, resolved from the
system tlpdb, and writes a matching tlpdb. `.texenv/bin/<arch>/tlmgr`
then installs further packages into .texenv/, never into the system tree.
Approach from https://github.com/ricklyon/texenv.
"""

import hashlib
import os
import shutil
import subprocess
import tarfile
import tempfile
from collections.abc import Mapping
from pathlib import Path

import httpx

# 00texlive.* hold the platform and repository tlmgr installs from. Closure of these (via tlpdb `depend` lines) is copied: tlmgr itself (infra), plain TeX and
# the engines (minimal), and LaTeX. Everything else comes from `texi sync`.
ROOTS = [
    "00texlive.config",
    "00texlive.installation",
    "scheme-infraonly",
    "scheme-minimal",
    "latex",
]


def minimal_set(pkgs: dict, arch: str) -> list[str]:
    """ROOTS plus all their dependencies, with `<name>.<arch>` binary variants."""
    seen: dict[str, None] = {}
    todo = list(ROOTS)
    while todo:
        n = todo.pop()
        if n in seen or n not in pkgs:
            continue
        seen[n] = None
        todo += [f"{n}.{arch}"] + [
            ln.split()[1] for ln in pkgs[n][0].splitlines() if ln.startswith("depend ")
        ]
    return list(seen)


def tlpdb_parse(path: Path) -> dict[str, tuple[str, dict[str, list[str]]]]:
    """name -> (raw stanza text after the name line, {"binfiles"|"runfiles": [paths]})."""
    pkgs: dict[str, tuple[str, dict[str, list[str]]]] = {}
    for stanza in path.read_text(encoding="utf-8").split("\n\n"):
        lines = stanza.strip("\n").split("\n")
        if not lines[0].startswith("name "):
            continue
        files: dict[str, list[str]] = {"binfiles": [], "runfiles": []}
        cur = None
        for ln in lines[1:]:
            if ln.startswith(" "):
                if cur:
                    files[cur].append(ln.split()[0])  # drop "size=..." / "arch=..."
            else:
                cur = ln.split()[0] if ln.split()[0] in files else None
        pkgs[lines[0][5:].strip()] = ("\n".join(lines[1:]) + "\n", files)
    return pkgs


def env_bin(env_dir: Path) -> Path | None:
    """The env's bin/<arch> directory, or None if the env doesn't exist yet."""
    return next(iter(sorted((env_dir / "bin").glob("*"))), None)


# What `tlmgr init-usertree` writes as tlpkg/texlive.tlpdb (checked against tlmgr 2026):
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
    """Create an empty usertree skeleton, as `tlmgr init-usertree` does, without tlmgr.

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


def create_env(env_dir: Path, root: Path) -> None:
    """Copy the minimal package set from the TeX Live tree at `root` into `env_dir`."""
    pkgs = tlpdb_parse(root / "tlpkg" / "texlive.tlpdb")
    arch = next(p.name for p in (root / "bin").iterdir() if p.is_dir())
    wanted = minimal_set(pkgs, arch)

    shutil.rmtree(env_dir, ignore_errors=True)
    for name in wanted:
        for rel in pkgs[name][1]["binfiles"] + pkgs[name][1]["runfiles"]:
            src, dst = root / rel, env_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                shutil.copytree(src, dst, symlinks=True, dirs_exist_ok=True)
            elif src.exists() or src.is_symlink():
                shutil.copy2(src, dst, follow_symlinks=False)
        tlpobj = root / "tlpkg" / "tlpobj" / f"{name}.tlpobj"
        if tlpobj.exists():
            (env_dir / "tlpkg" / "tlpobj").mkdir(parents=True, exist_ok=True)
            shutil.copy2(tlpobj, env_dir / "tlpkg" / "tlpobj")

    # the env's own package database: only what was copied
    (env_dir / "tlpkg" / "backups").mkdir(parents=True, exist_ok=True)
    (env_dir / "tlpkg" / "texlive.tlpdb").write_text(
        "".join(f"name {n}\n{pkgs[n][0]}\n" for n in wanted), encoding="utf-8"
    )
    shutil.copytree(
        root / "texmf-var", env_dir / "texmf-var", symlinks=True, dirs_exist_ok=True
    )


def _fetch_installer(base: str) -> Path:
    """install-tl-unx.tar.gz from the user cache, re-downloaded only when the published sha512 changes."""
    name = "install-tl-unx.tar.gz"
    cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "texi"
    cached = cache / name
    # httpx (certifi CA bundle), not urllib: uv-managed Pythons often lack CA certs on macOS
    try:
        r = httpx.get(f"{base}/{name}.sha512", follow_redirects=True, timeout=60)
        r.raise_for_status()
        sha = r.text.split()[0]
        # mirror.ctan.org redirects per request; pin the tarball to the mirror that gave the hash
        pinned = str(r.url).rsplit("/", 1)[0]
        if cached.exists() and hashlib.sha512(cached.read_bytes()).hexdigest() == sha:
            return cached
        t = httpx.get(f"{pinned}/{name}", follow_redirects=True, timeout=300)
        t.raise_for_status()
    except httpx.HTTPError:
        if cached.exists():  # offline or mirror down: a stale installer beats none
            return cached
        raise
    if hashlib.sha512(t.content).hexdigest() != sha:
        raise OSError(f"{name} does not match its published sha512")
    cache.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(t.content)
    return cached


def install_env(
    env_dir: Path,
    repository: str | None = None,
    scheme: str = "minimal",
    *,
    tlmgr: bool = True,
) -> None:
    """Fresh TeX Live install into `env_dir` with the official install-tl (needs network).

    With ``tlmgr=False`` only install-tl runs; the caller adds LaTeX and latexmk
    itself (scheme-minimal has neither).
    """
    tarball = _fetch_installer(
        (repository or "https://mirror.ctan.org/systems/texlive/tlnet").rstrip("/")
    )
    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(tarball) as tf:
            tf.extractall(tmp, filter="data")
        script = next(Path(tmp).glob("install-tl-*/install-tl"))
        subprocess.run(
            [
                "perl",
                str(script),
                f"--scheme={scheme}",
                f"--texdir={env_dir}",
                f"--texuserdir={env_dir / 'user'}",
                "--no-interaction",
                "--no-doc-install",
                "--no-src-install",
                "--paper=letter",
                *(["--repository", repository] if repository else []),
            ],
            check=True,
        )
    if not tlmgr:
        return
    bin_dir = env_bin(env_dir)
    if bin_dir is None:
        raise OSError(f"install-tl created no bin/<arch> in {env_dir}")
    # scheme-minimal has no LaTeX; add just the core and latexmk (tlmgr builds the formats)
    subprocess.run(
        [
            str(bin_dir / "tlmgr"),
            *(["--repository", repository] if repository else []),
            "install",
            "latex",
            "latexmk",
        ],
        check=True,
    )
