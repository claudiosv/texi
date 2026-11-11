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
    env_dir: Path, repository: str | None = None, scheme: str = "minimal"
) -> None:
    """Fresh TeX Live install into `env_dir` with the official install-tl (needs network)."""
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
    # scheme-minimal has no LaTeX; add just the core and latexmk (tlmgr builds the formats)
    subprocess.run(
        [
            str(env_bin(env_dir) / "tlmgr"),
            *(["--repository", repository] if repository else []),
            "install",
            "latex",
            "latexmk",
        ],
        check=True,
    )
