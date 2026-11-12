import asyncio
import json
import getpass
import os
import shlex
import re
import textwrap
import shutil
import sqlite3
import subprocess
import sys
import time
import tomllib
from enum import Enum
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import inflect
import sh
import typer
from pyclack.core import ShimmerProgress, is_cancel
from pyclack.prompts import Option, confirm, note, outro, select
from pyclack.utils.console import console
from pyclack.utils.styling import S_BAR_START, Color
from pydantic import ValidationError
from rich.console import Console
from rich.filesize import decimal
from rich.markup import escape
from rich.text import Text
from rich.tree import Tree

from texi import archive, deptree, latexmk_files, logparse_bib, lsr, pdffonts, runaway, texenv, texfmt, tools, unused as unused_mod
from texi import tlpdb as tlpdb_mod
from texi.logparse_texfmt import parse_texfmt
from texi.ctan import CtanClient
from texi.logparse import (
    Diagnostic,
    dedupe,
    missing_package,
    guess_lines,
    parse_blg,
    parse_latexmk,
    parse_tex_log,
)
from texi.config import (
    BuildConfig,
    GoMode,
    ProjectConfig,
    TexFmtConfig,
    TexProjectConfig,
)

app = typer.Typer(
    help=(
        "[bold]texi[/bold]: a workspace manager for LaTeX projects.\n\n"
        "Each project gets its own TeX packages in [cyan].texenv/[/cyan], so builds "
        "are reproducible and your system TeX Live stays clean. "
        "Everything is configured in [cyan]texproject.toml[/cyan]."
    ),
    epilog=(
        "[bold]Quick start:[/bold]  texi init  →  texi sync  →  texi build\n"
        "Run [cyan]texi COMMAND --help[/cyan] for details on a command."
    ),
    rich_markup_mode="rich",
    no_args_is_help=True,
    add_completion=False,
)
DEBUG = False


def _set_debug(value: bool) -> None:
    global DEBUG
    DEBUG = DEBUG or value


# Accepted both before the command (`texi --debug sync`) and after it.
DebugOpt = Annotated[
    bool,
    typer.Option(
        "--debug",
        callback=_set_debug,
        is_eager=True,
        help="Show every external command texi runs, along with its output.",
    ),
]


@app.callback()
def main(debug: DebugOpt = False) -> None:
    """LaTeX project workspace manager."""
    _activate_env(Path.cwd() / ".texenv")


def _debug_cmd(cmd: list[str], out: Console | None = None) -> None:
    if DEBUG:
        (out or console).print(f"[dim]$ {escape(' '.join(map(str, cmd)))}[/dim]")


def _intro(title: str) -> None:
    """pyclack's intro bar, minus its console.clear() (which wipes build logs)."""
    console.print(Text.from_markup(f"{Color.gray(S_BAR_START)}  {title}"))


def _note(title: str, content: list[str]) -> None:
    """pyclack note, with lines wrapped so the box fits the terminal."""
    width = max(20, console.width - 8)
    lines = [w for line in content for w in textwrap.wrap(line, width) or [""]]
    note(title=title, content=lines)


def _confirm(message: str, *, default: bool = False) -> bool:
    answer = asyncio.run(confirm(message=message, initial_value=default))
    return not is_cancel(answer) and bool(answer)


def _select(
    message: str, choices: list[str], descriptions: dict[str, str] | None = None
) -> str | None:
    """Pick one of `choices`; `descriptions` are shown next to the names."""
    descriptions = descriptions or {}
    options = [
        Option(c, f"{c} - {descriptions[c]}" if descriptions.get(c) else c)
        for c in choices
    ]
    answer = asyncio.run(select(message=message, options=options))
    return None if is_cancel(answer) else answer


def _texenv_vars(env_dir: Path) -> dict[str, str]:
    """TeX env vars that point TeX (and updmap's generated font maps) at .texenv/."""
    root = env_dir.resolve()
    if (bin_dir := texenv.env_bin(env_dir)) is not None and _standalone(env_dir):
        # no PATH: callers put bin/<arch> in front of their own
        return {k: v for k, v in texenv.clone_env(root, bin_dir.name, {}).items() if k != "PATH"}
    return {
        "TEXMFHOME": str(root),
        "TEXMFVAR": str(root / "texmf-var"),
        "TEXMFCONFIG": str(root / "texmf-config"),
    }


def _updmap_local(env_dir: Path) -> None:
    """Regenerate font maps for packages in .texenv/ (a usertree has no updmap-sys)."""
    if not shutil.which("updmap-user"):
        return
    env = os.environ | _texenv_vars(env_dir)
    cmd = ["updmap-user"]
    _debug_cmd(cmd)
    # updmap-user exits non-zero on harmless warnings; the maps are still written
    result = subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)
    if DEBUG:
        console.print(f"[dim]{escape(result.stdout + result.stderr)}[/dim]")
    console.print("[green]✔ Font maps updated[/green]")


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    """Run a command, echoing it and its stdout/stderr under --debug."""
    _debug_cmd(cmd)
    result = subprocess.run(
        cmd, capture_output=True, text=True, check=False, stdin=subprocess.DEVNULL
    )
    if DEBUG:
        console.print(
            f"[dim]exit {result.returncode}\n--- stdout ---\n{escape(result.stdout)}"
            f"--- stderr ---\n{escape(result.stderr)}[/dim]"
        )
    return result


# Pass the generated .latexmkrc to latexmk via `-norc -r` so it is read exactly once.
EXPLICIT_RC = True
# Piping the rc needs /dev/stdin; elsewhere (Windows) it is always written to disk.
PIPE_RC_SUPPORTED = sys.platform != "win32"

PROJECT_REPOSITORY: str | None = None  # set from [project] repository


def _peek_project() -> ProjectConfig | None:
    """The cwd's [project] table if texproject.toml is readable; None otherwise (no errors)."""
    path = Path.cwd() / "texproject.toml"
    try:
        return TexProjectConfig(**tomllib.loads(path.read_text(encoding="utf-8"))).project
    except OSError, tomllib.TOMLDecodeError, ValidationError:
        return None


latexmk_path = shutil.which("latexmk")
texfmt_path = shutil.which("tex-fmt")
inflect_engine = inflect.engine()

# --- Core Logic ---


def load_project_config(config_path: Path) -> TexProjectConfig:
    """Loads, parses, and validates the texproject.toml file."""
    if not config_path.exists():
        console.print(
            f"[red]Error:[/red] Configuration file '{config_path.name}' not found."
        )
        raise typer.Exit(code=1)

    try:
        raw_data = tomllib.loads(config_path.read_text(encoding="utf-8"))
        return TexProjectConfig(**raw_data)
    except tomllib.TOMLDecodeError as e:
        console.print(f"[red]TOML Parsing Error:[/red] {e}")
        raise typer.Exit(code=1)
    except ValidationError as e:
        console.print("[red]Configuration Validation Error:[/red]")
        console.print(e)
        raise typer.Exit(code=1)


def format_perl_value(key: str, value: str | int | bool | list[str]) -> str:
    """Formats Python types back to Perl latexmkrc syntax."""
    if isinstance(value, bool):
        return "1" if value else "0"

    if isinstance(value, list):
        perl_array = ", ".join(f"'{v}'" for v in value)
        return f"({perl_array})"

    if isinstance(value, str):
        # Specific enum mappings
        pdf_modes: dict[str, str] = {
            "off": "0",
            "pdflatex": "1",
            "ps2pdf": "2",
            "dvi2pdf": "3",
            "lualatex": "4",
            "xelatex": "5",
        }
        bibtex_modes: dict[str, str] = {
            "never": "0",
            "bib_exists": "1",
            "even_without_bib": "1.5",
            "always": "2",
        }

        go_modes: dict[str, str] = {
            "rebuild": "0",
            "full-rebuild": "1",
            "clean": "2",
            "force-main": "3",
        }
        subdirs: dict[str, str] = {"none": "0", "aux": "1", "all": "2"}
        update_methods: dict[str, str] = {
            "auto": "0",
            "manual": "1",
            "signal": "2",
            "none": "3",
            "command": "4",
        }

        if key == "allow_subdir_creation" and value in subdirs:
            return subdirs[value]
        if key == "go_mode" and value in go_modes:
            return go_modes[value]
        if key.endswith("_update_method") and value in update_methods:
            return update_methods[value]
        if key == "pdf_mode" and value in pdf_modes:
            return pdf_modes[value]
        if key == "bibtex_use" and value in bibtex_modes:
            return bibtex_modes[value]

        return f"'{value}'"

    return str(value)


def inject_pdf_primitives(engine_cmd: str) -> str:
    """Wraps a pdflatex command so pdfTeX primitives run before the source is read."""
    primitives = (
        r"\pdfminorversion=5\pdfcompresslevel=9\pdfobjcompresslevel=3\input{%S}"
    )
    quoted = f'"{primitives}"'
    if "%S" in engine_cmd:
        return engine_cmd.replace("%S", quoted)
    return f"{engine_cmd} {quoted}"


_ENGINES = ("pdflatex", "lualatex", "xelatex", "latex")
_NOT_LATEXMK = {
    "synctex",
    "shell_escape",
    "file_line_error",
    "extra_args",
    "env",
    "bib_dirs",
    "tex_inputs",
    "check_type3",
    "write_latexmkrc",
}


def engine_command(name: str, cfg: BuildConfig) -> str | None:
    """The latexmk command for a TeX engine, with synctex/shell-escape/extra args added."""
    base = getattr(cfg, name)
    flags = ["-synctex=1"] if cfg.synctex else []
    flags += ["-shell-escape"] if cfg.shell_escape else []
    flags += ["-file-line-error"] if cfg.file_line_error else []
    flags += cfg.extra_args
    flags = [f for f in flags if f not in (base or "").split()]
    if not flags:
        return base
    exe, _, rest = (base or f"{name} %O %S").partition(" ")
    return f"{exe} {' '.join(flags)} {rest}".rstrip()


def _perl_str(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _search_path(dirs: list[str]) -> str:
    """BIBINPUTS/TEXINPUTS value: each dir searched recursively, then the defaults."""
    parts = [d if os.path.isabs(d) else f"$root/{d.strip('/')}" for d in dirs]
    return ":".join(f"{p}//" for p in parts) + ":"


def _skim_app() -> bool:
    return any(
        (base / "Skim.app").is_dir()
        for base in (Path("/Applications"), Path.home() / "Applications")
    )


# TeX's max_print_line. BibTeX's line buffer (buf_size) is 20000, so anything
# above 19999 can overflow it when TeX output is read back.
MAX_PRINT_LINE = 19999
BIBTEX_LINE_LIMIT = 19999


def render_latexmkrc(config: TexProjectConfig) -> str:
    """Renders the latexmkrc text from the validated Pydantic model."""
    rc_lines: list[str] = ["# Auto-generated by texi.", "# Do not edit manually.", ""]

    # mode='json' ensures all enums are reduced to their primitive values
    settings = config.build.model_dump(mode="json")
    settings["jobname"] = config.set_jobname
    if (
        sys.platform == "darwin"
        and settings.get("pdf_previewer") is None
        and _skim_app()
    ):
        # Skim reloads the PDF by itself when it changes
        settings["pdf_previewer"] = "open -a Skim %O %S"
        if settings.get("pdf_update_method") is None:
            settings["pdf_update_method"] = "auto"

    in_subdir = config.src_dir not in ("", ".")
    rc_lines += [
        "use Cwd qw(abs_path);",
        "use File::Basename qw(dirname);",
        "my $root = dirname(abs_path(__FILE__));  # project root, wherever latexmk runs",
        'if (-d "$root/.texenv") {',
        '  my ($bin) = glob("$root/.texenv/bin/*");  # standalone tree',
        "  if ($bin) {",
        "    # only .texenv/ is on TeX's search path",
        f"    delete @ENV{{qw({' '.join(texenv.SCRUBBED)})}};",
        '    my $t = "$root/.texenv";',
        '    $ENV{TEXMFROOT} = $t;',
        '    $ENV{TEXMFDIST} = "$t/texmf-dist";',
        '    $ENV{TEXMFLOCAL} = "$t/texmf-local";',
        '    $ENV{TEXMFHOME} = "$t/texmf-home";',
        '    $ENV{TEXMFVAR} = $ENV{TEXMFSYSVAR} = "$t/texmf-var";',
        '    $ENV{TEXMFCONFIG} = $ENV{TEXMFSYSCONFIG} = "$t/texmf-config";',
        '    $ENV{TEXMFCNF} = "$t/texmf-dist/web2c";',
        '    $ENV{PATH} = "$bin:$ENV{PATH}";',
        "  } else {  # usertree over the system TeX Live",
        '    $ENV{TEXMFHOME} = "$root/.texenv";',
        '    $ENV{TEXMFVAR} = "$root/.texenv/texmf-var";  # font maps from updmap-user',
        '    $ENV{TEXMFCONFIG} = "$root/.texenv/texmf-config";',
        "  }",
        "}",
        f"$ENV{{max_print_line}} = {MAX_PRINT_LINE};  # avoid line wrapping in logs",
        f"$log_wrap = {MAX_PRINT_LINE};  # latexmk's own idea of the wrap width",
        '$ENV{error_line} = "254";  # longest allowed, so error context is not cut',
        '$ENV{half_error_line} = "238";',
    ]
    # latexmk -cd moves into src_dir, so anchor build dirs and TeX/BibTeX search
    # paths at the project root.
    src = [config.src_dir] if in_subdir else []
    for var, dirs in (
        ("BIBINPUTS", src + config.build.bib_dirs),
        ("TEXINPUTS", src + config.build.tex_inputs),
    ):
        if dirs:
            rc_lines.append(
                f"$ENV{{{var}}} = \"{_search_path(dirs)}\" . ($ENV{{{var}}} // '');"
            )
    for k, v in config.build.env.items():
        rc_lines.append(f"$ENV{{{k}}} = {_perl_str(v)};")
    rc_lines.append("")

    rc_lines.append("# --- Build ---")
    rc_lines.append("$rc_report = 0;  # don't list the rc files read")
    for key, value in settings.items():
        if key == "optimize_pdf" or key in _NOT_LATEXMK or key in _ENGINES:
            continue
        if value is None:
            continue
        if isinstance(value, list) and not value:
            continue
        if (
            in_subdir
            and key in ("out_dir", "aux_dir", "out2_dir")
            and not os.path.isabs(value)
        ):
            rc_lines.append(f'${key} = "$root/{value}";')
            continue
        if key == "default_files":
            continue  # emitted below, relative to the project root
        if key in ("clean_ext", "clean_full_ext"):
            rc_lines.append(f"${key} = '{' '.join(value)}';")  # space-separated string
            continue

        perl_val = format_perl_value(key, value)
        prefix = "@" if isinstance(value, list) else "$"
        rc_lines.append(f"{prefix}{key} = {perl_val};")
    targets = [str(Path(config.src_dir) / f) for f in config.main_files]
    if in_subdir:
        rc_lines.append("$do_cd = 1;  # run in src_dir; paths below are from the root")
    rc_lines.append(f"@default_files = ({', '.join(_perl_str(t) for t in targets)});")
    rc_lines.append("")

    commands = {n: engine_command(n, config.build) for n in _ENGINES}
    if config.build.optimize_pdf:
        commands["pdflatex"] = inject_pdf_primitives(
            commands["pdflatex"] or "pdflatex %O %S"
        )
    rc_lines.append("# --- TeX engine commands ---")
    rc_lines += [f"${n} = {_perl_str(c)};" for n, c in commands.items() if c]
    rc_lines.append("")

    return "\n".join(rc_lines) + "\n"


def generate_latexmkrc(config: TexProjectConfig, rc_path: Path) -> None:
    """Writes the generated .latexmkrc to rc_path."""
    rc_path.write_text(render_latexmkrc(config), encoding="utf-8")


def sty_cache_path() -> Path:
    """Location of the persistent .sty -> bundle name lookup cache."""
    cache_home = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return cache_home / "texi" / "sty_bundle_cache.json"


def load_sty_cache() -> dict[str, list[str]]:
    """Loads the persistent .sty -> bundle name cache, tolerating a missing/corrupt file."""
    path = sty_cache_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError, OSError:
        return {}


def save_sty_cache(cache: dict[str, list[str]]) -> None:
    """Persists the .sty -> bundle name cache to disk."""
    path = sty_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")


def find_texlive_for_ctan(name: str) -> list[str] | None:
    """Ask CTAN which TeX Live package provides `name`, e.g. binhex -> kastrup.

    Looks `name` up as a CTAN key or alias, then reads the `texlive` field of the
    package record. None means CTAN could not be reached (do not cache that).
    """
    with CtanClient() as client:
        return client.find_texlive(name)


_TLPDB_UNSET: Any = object()
_tlpdb_memo: Any = _TLPDB_UNSET


def _reset_tlpdb_memo() -> None:
    """Forget the index loaded by `_resolver_tlpdb` (for tests)."""
    global _tlpdb_memo
    _tlpdb_memo = _TLPDB_UNSET


def _resolver_tlpdb(
    console: Console, refresh: bool = False
) -> tlpdb_mod.Tlpdb | None:
    """The TeX Live package index for name lookups, loaded once per process.

    Uses the cached index (building it from a download when there is none yet).
    With `refresh` it asks the repository for a newer one (a conditional request,
    so an unchanged index costs almost nothing) and falls back to the cache when
    the repository cannot be reached.
    None when it cannot be loaded; callers then fall back to CTAN.
    """
    global _tlpdb_memo
    if _tlpdb_memo is not _TLPDB_UNSET and not refresh:
        return _tlpdb_memo
    db: tlpdb_mod.Tlpdb | None = None
    try:
        repo, _ = tlpdb_mod.resolve_repository(None, None)
        if not tlpdb_mod.has_cache(repo):
            console.print("[dim]Building TeX Live package index…[/dim]")
        if refresh:
            console.print(
                f"[blue]Fetching the latest package index from {escape(repo)}[/blue]"
            )
            with ShimmerProgress(console) as prog:
                db = tlpdb_mod.load(
                    repo,
                    refresh=True,
                    progress=lambda done, total: prog.on_progress(
                        "Fetching the latest package index", done, total
                    ),
                )
            _report_index(console, db, repo)
        else:
            db = tlpdb_mod.load(repo)
    except (tlpdb_mod.TlpdbError, OSError, sqlite3.Error) as e:
        if DEBUG:
            console.print(f"[dim]tlpdb index unavailable: {escape(str(e))}[/dim]")
    _tlpdb_memo = db
    return db


def _index_change(db: tlpdb_mod.Tlpdb) -> tuple[str, bool]:
    """Compare a freshly downloaded index with the one it replaced, by package revisions.

    Every package revision is a TeX Live SVN revision, so the highest one dates the
    index: a mirror that is behind serves a lower maximum. Returns the message and
    whether the new index is at least as new as the old one.
    """
    old = db.previous_revisions
    if not old:
        return "Downloaded an updated package index", True
    new = db.revisions()
    changed = sum(1 for n, r in new.items() if n in old and old[n] != r)
    added = len(new.keys() - old.keys())
    removed = len(old.keys() - new.keys())
    top_new, top_old = max(new.values(), default=0), max(old.values(), default=0)
    detail = f"{changed} changed, {added} new, {removed} removed"
    if top_new > top_old:
        return f"Downloaded a newer package index (r{top_old} → r{top_new}; {detail})", True
    if top_new < top_old:
        return (
            f"Downloaded an OLDER package index (r{top_new}, cached r{top_old}; "
            f"{detail}): the mirror looks stale"
        ), False
    return (
        f"Downloaded a package index with the same newest revision r{top_new} ({detail})",
        True,
    )


def _report_index(console: Console, db: tlpdb_mod.Tlpdb, repo: str) -> None:
    """Say where the package index came from and whether it changed."""
    source = str(getattr(db, "source", "") or repo)
    mirror = source.removesuffix("/tlpkg/texlive.tlpdb.xz")
    via = f" (via {escape(mirror)})" if mirror.rstrip("/") != repo.rstrip("/") else ""
    if getattr(db, "offline", False):
        when = f" from {db.fetched:%Y-%m-%d %H:%M}" if db.fetched else ""
        console.print(
            f"[yellow]Could not reach {escape(repo)}; using the cached package index"
            f"{when}.[/yellow]"
        )
    elif getattr(db, "stale_mirror", False):
        console.print(
            "[yellow]⚠ The mirrors offered an older package index than the cached one; "
            f"kept the cached index from {db.fetched:%Y-%m-%d %H:%M}.[/yellow]"
        )
    elif db.from_cache:
        console.print(
            f"[green]✔ Package index is up to date (the repository reports it unchanged){via}[/green]"
        )
    elif db.changed is None:
        console.print(f"[green]✔ Downloaded the package index{via}[/green]")
    elif not db.changed:
        console.print(
            f"[green]✔ Downloaded the package index; identical to the cached copy{via}[/green]"
        )
    else:
        text, fresh = _index_change(db)
        mark = "[green]✔" if fresh else "[yellow]⚠"
        console.print(f"{mark} {escape(text)}{via}[/]")


def _tlpdb_bundles(db: tlpdb_mod.Tlpdb, name: str, exts: tuple[str, ...]) -> list[str]:
    """Packages of the index that provide `name`; see `find_bundle_for_sty`."""
    by_file = db.packages_for_files([f"{name}{e}" for e in exts], section="runfiles")
    found = dict.fromkeys(p for pkgs in by_file.values() for p in pkgs)
    found.update(dict.fromkeys(db.names_by_catalogue(name)))
    names = list(found)
    cats = db.categories(names)
    names = [n for n in names if cats.get(n) == "Package"] or names
    if name in names:
        return [name]
    same_key = set(db.names_by_catalogue_nocase(name))
    by_key = [n for n in names if n in same_key]
    if by_key:
        return by_key
    return names


def find_bundle_for_sty(
    sty_name: str,
    console: Console,
    cache: dict[str, list[str]] | None = None,
    exts: tuple[str, ...] = (".sty", ".cls"),
) -> list[str]:
    """
    Find the TeX Live packages that provide {sty_name}.

    The cache is tried first, then the local TeX Live package index: packages
    shipping a run file {sty_name}{ext}, plus packages whose `catalogue` key is
    {sty_name} (binhex -> kastrup). Collections and schemes are ignored. A
    package called exactly {sty_name} wins, then one whose catalogue key is it,
    then a single candidate; otherwise all candidates are returned. If the
    index is unavailable or knows nothing, or has several candidates, CTAN is asked and its `texlive` field
    settles the choice when it is one of them, otherwise all candidates are
    returned for the caller to choose from. With no candidates, CTAN is the
    last resort.
    """
    if cache is not None and sty_name in cache:
        return cache[sty_name]

    db = _resolver_tlpdb(console)
    consulted = False
    if db is not None:
        try:
            found = _tlpdb_bundles(db, sty_name, exts)
            consulted = True
        except sqlite3.Error:
            found = []
        if found:
            if DEBUG:
                console.print(f"[dim]{escape(sty_name)}: found in tlpdb index[/dim]")
            if cache is not None:
                cache[sty_name] = found
            return found

    # the index, then CTAN
    bundles: list[str] = []
    ctan: list[str] | None = None
    settled = len(bundles) == 1
    if not settled:  # none or ambiguous: CTAN knows the TeX Live name
        ctan = find_texlive_for_ctan(sty_name)
        if ctan:
            exact = [b for b in bundles if b in ctan]
            bundles = exact or ([] if bundles else ctan) or bundles
    if DEBUG:
        console.print(f"[dim]{escape(sty_name)}: CTAN fallback[/dim]")
    answered = settled or ctan is not None  # an unanswered CTAN query is not remembered
    if cache is not None and answered and (bundles or consulted):
        cache[sty_name] = bundles
    return bundles


_TEX_PACKAGE_RE = re.compile(
    r"\\(?:usepackage|RequirePackage|documentclass|LoadClass)(?:WithOptions)?"
    r"(?:\s*\[[^\]]*\])?\s*\{([^}]*)\}"
)
_SKIP_DIR_NAMES = {"build", ".texenv"}
_SCAN_SUFFIXES = {".tex", ".cls", ".sty", ".bbx", ".cbx", ".def", ".ltx"}


def _source_files(cwd: Path) -> list[Path]:
    """TeX source files under cwd, skipping build output, .texenv and dot-dirs."""
    return [
        f
        for f in cwd.rglob("*")
        if f.suffix in _SCAN_SUFFIXES
        and f.is_file()
        and not any(
            part in _SKIP_DIR_NAMES or part.startswith(".")
            for part in f.relative_to(cwd).parts[:-1]
        )
    ]


def extract_packages_from_tex(cwd: Path, files: list[Path] | None = None) -> list[str]:
    r"""Scan tex/cls/sty/... files for package and class names, minus ones the project ships itself."""
    packages: set[str] = set()
    files = _source_files(cwd) if files is None else files

    for tex_file in files:
        try:
            text = tex_file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        for match in _TEX_PACKAGE_RE.finditer(text):
            for raw_name in match.group(1).split(","):
                stripped_name = raw_name.strip()
                if stripped_name:
                    packages.add(stripped_name)

    local = {f.stem for f in files if f.suffix in {".sty", ".cls"}}
    return sorted(packages - local)


def resolve_dependencies(
    packages: list[str],
    console: Console,
    on_progress: Callable[[int, int], None] | None = None,
) -> list[str]:
    r"""Resolves raw \usepackage names to TeX Live package names, using a persistent cache."""
    cache = load_sty_cache()
    dependencies: set[str] = set()

    for i, name in enumerate(packages):
        if on_progress:
            on_progress(i, len(packages))
        bundles = find_bundle_for_sty(name, console, cache=cache)
        dependencies.add(bundles[0] if bundles else name)

    save_sty_cache(cache)

    return sorted(dependencies)


# --- CLI Commands ---


@app.command(
    help="List the packages your source files use, and flag any missing from texproject.toml.",
    rich_help_panel="Project",
)
def scan(
    resolve: bool = typer.Option(
        True, "--resolve/--no-resolve", help="Resolve names to TeX Live packages."
    ),
    debug: DebugOpt = False,
) -> None:
    """List packages and classes used by tex/cls/sty files in the current directory."""
    _intro("texi scan")
    files = _source_files(Path.cwd())
    packages = extract_packages_from_tex(Path.cwd(), files)
    _note(
        title=f"{len(packages)} package(s) across {len(files)} source file(s)",
        content=[escape(", ".join(packages)) or "(none)"],
    )
    if resolve and packages:
        with ShimmerProgress(console) as prog:
            deps = resolve_dependencies(
                packages,
                console,
                lambda done, total: prog.on_progress(
                    "Resolving to TeX Live packages", done, total
                ),
            )
        _note(title="TeX Live packages", content=[escape(", ".join(deps))])
        config_path = Path.cwd() / "texproject.toml"
        if config_path.exists():
            declared = set(load_project_config(config_path).dependencies)
            missing = sorted(set(deps) - declared)
            if missing:
                _note(
                    title=f"{len(missing)} possibly missing from texproject.toml",
                    content=[
                        escape(", ".join(missing)),
                        "Packages covered by a collection (e.g. collection-latex) "
                        "may be fine already.",
                        escape(f"Add with: texi add {' '.join(missing)}"),
                    ],
                )
            else:
                console.print(
                    "[green]✔ All used packages are in texproject.toml[/green]"
                )
    outro("[green]✔ Scan complete[/green]")


@app.command(
    help="Create a texproject.toml for this project, filling in packages found in your .tex files.",
    rich_help_panel="Project",
)
def init(
    force: bool = typer.Option(
        False, "--force", help="Replace an existing texproject.toml."
    ),
    debug: DebugOpt = False,
) -> None:
    """Creates a texproject.toml, scanning any .tex files for package dependencies."""
    _intro("texi init")
    cwd = Path.cwd()
    config_path = cwd / "texproject.toml"

    if config_path.exists() and not force:
        if not (
            sys.stdin.isatty()
            and _confirm(f"'{config_path.name}' already exists. Overwrite?")
        ):
            console.print(
                f"[red]Error:[/red] '{config_path.name}' already exists. Use --force to overwrite."
            )
            raise typer.Exit(code=1)

    tex_files = _source_files(cwd)
    packages = extract_packages_from_tex(cwd, tex_files)

    if packages:
        console.print(
            f"Found {len(packages)} package(s) across {len(tex_files)} source file(s)"
        )
        with ShimmerProgress(console) as prog:
            dependencies = resolve_dependencies(
                packages,
                console,
                lambda done, total: prog.on_progress(
                    "Resolving to TeX Live packages", done, total
                ),
            )
    else:
        dependencies = []
        if tex_files:
            console.print(
                "[dim]No \\usepackage/\\RequirePackage found in .tex files.[/dim]"
            )
        else:
            console.print("[dim]No .tex files found.[/dim]")

    try:
        config = TexProjectConfig(
            project=ProjectConfig(
                name=cwd.name, dependencies=dependencies
            )
        )
    except ValidationError as e:
        console.print("[red]Configuration Validation Error:[/red]")
        console.print(e)
        raise typer.Exit(code=1) from e

    lines = [
        "[project]",
        f'name = "{config.name}"',
        f'src_dir = "{config.src_dir}"',
        f"targets = {json.dumps(config.project.targets)}",
        "",
        "dependencies = [",
    ]
    lines += [f'  "{dep}",' for dep in config.dependencies]
    lines.append("]")
    lines.append("")
    config_path.write_text("\n".join(lines), encoding="utf-8")
    _create_env(config.project.repository)

    outro(
        f"[green]✔ Created {config_path.name} with {len(dependencies)} dependencies.[/green]"
    )


LOCK_NAME = "texi.lock"


def _standalone(env_dir: Path) -> bool:
    """True if .texenv/ is a full TeX Live tree (copy/install) rather than a usertree.

    A usertree can have a bin/<arch> too (cloned binaries); it is marked
    `setting_usertree` in its tlpdb.
    """
    if texenv.is_usertree(env_dir):
        return False
    # a clone tree has texmf-local even before any binary package is in it
    return texenv.env_bin(env_dir) is not None or (env_dir / "texmf-local").is_dir()


def _usertree_packages(env_dir: Path) -> set[str]:
    """Names of packages recorded in the .texenv tlpdb."""
    tlpdb = env_dir / "tlpkg" / "texlive.tlpdb"
    if not tlpdb.exists():
        return set()
    return {
        line[5:].strip()
        for line in tlpdb.read_text().splitlines()
        if line.startswith("name ")
    }


def _activate_env(env_dir: Path) -> None:
    """Put .texenv/bin/<arch> first on PATH (standalone: also the TEXMF variables).

    Every tool (latexmk, fmtutil, updmap, luaotfload...) then resolves
    to the env's copy before the system one.
    """
    global latexmk_path
    if not (bin_dir := texenv.env_bin(env_dir)):
        return
    if _standalone(env_dir):
        for name in texenv.SCRUBBED:
            os.environ.pop(name, None)
        os.environ.update(_texenv_vars(env_dir))
    paths = os.environ.get("PATH", "").split(os.pathsep)
    if paths[0] != str(bin_dir):
        os.environ["PATH"] = os.pathsep.join([str(bin_dir), *paths])
    latexmk_path = shutil.which("latexmk")


def _clone_env_tree(repository: str | None) -> None:
    """Build a standalone .texenv/ from the archive store (no install-tl, no system TeX Live)."""
    env_dir = Path.cwd() / ".texenv"
    repo, _ = tlpdb_mod.resolve_repository(repository, None)
    console.print("[blue]Cloning TeX Live into .texenv/ from the archive store...[/blue]")
    texenv.init_clone(env_dir, repo)
    with ShimmerProgress(console) as prog:
        result = _archive_install(
            prog,
            repository,
            env_dir,
            standalone=True,
            roots=texenv.CLONE_ROOTS,
            update=False,
            infrastructure=True,
        )
    _report_failed(result, "packages")  # exits 1; the next `texi sync` resumes
    _install_tex_fmt(env_dir, update=False)
    (env_dir / texenv.CLONE_DONE).write_text("ok\n", encoding="utf-8")
    _activate_env(env_dir)
    console.print("[green]✔ Created .texenv/[/green]")


def _install_tex_fmt(env_dir: Path, update: bool = True) -> Path | None:
    """tex-fmt from CTAN (`latex-formatter`) into the tree, updated to CTAN's latest version.

    A failure only warns. With `update` false an existing tex-fmt is kept without a request.
    """
    if not _standalone(env_dir):
        return None
    return texfmt.ensure(
        env_dir,
        _target_arch(env_dir),
        notice=lambda m: console.print(f"[yellow]{escape(m)}[/yellow]"),
        update=update,
    )


def _create_env(repository: str | None = None) -> None:
    """Create the standalone .texenv/ from the archive store (no-op if it exists)."""
    env_dir = Path.cwd() / ".texenv"
    if texenv.is_usertree(env_dir):
        return  # an existing usertree stays one; delete .texenv/ to rebuild it
    if (env_dir / texenv.CLONE_DONE).exists():
        return
    _clone_env_tree(repository)  # new, or an earlier run did not finish


def _texlive_year(env_dir: Path) -> str | None:
    """Release year of the TeX Live in use: from the env's own tlpdb, else the system's."""
    own = env_dir / "tlpkg" / "texlive.tlpdb"
    if _standalone(env_dir) and own.is_file():
        return tlpdb_mod.read_system_tlpdb(own).release
    info = tlpdb_mod.load_local_installed()
    return info.release if info else None


def _apply_project(config: TexProjectConfig) -> None:
    """Apply [project] repository / texlive: the repository for texi.lock and a year sanity check."""
    global PROJECT_REPOSITORY
    PROJECT_REPOSITORY = config.project.repository
    if config.project.texlive and (year := _texlive_year(Path.cwd() / ".texenv")):
        if year != config.project.texlive:
            console.print(
                f"[yellow]Warning: texproject.toml expects TeX Live "
                f"{config.project.texlive}, but {year} is installed.[/yellow]"
            )


def _package_revisions(env_dir: Path, names: list[str]) -> dict[str, int]:
    """Installed revision of each package: from .texenv's tlpdb, else the system tlpdb."""
    revs: dict[str, int] = {}
    tlpdb = env_dir / "tlpkg" / "texlive.tlpdb"
    if tlpdb.exists():
        name = None
        for line in tlpdb.read_text().splitlines():
            if line.startswith("name "):
                name = line[5:].strip()
            elif line.startswith("revision ") and name in names:
                revs[name] = int(line[9:])
    rest = [n for n in names if n not in revs]
    if rest:
        info = tlpdb_mod.load_local_installed()
        if info:
            revs |= {n: info.revisions[n] for n in rest if n in info.revisions}
    return revs


def _read_lock(path: Path) -> dict[str, int]:
    try:
        return dict(tomllib.loads(path.read_text(encoding="utf-8")).get("packages", {}))
    except OSError, tomllib.TOMLDecodeError:
        return {}


def _write_lock(path: Path, revs: dict[str, int]) -> None:
    """Write texi.lock at `path`."""
    year = _texlive_year(path.parent / ".texenv")
    lines = [
        "# Generated by `texi sync`. Commit this file to record resolved package revisions.",
        f'texlive = "{year}"' if year else "",
    ]
    if PROJECT_REPOSITORY:
        lines.append(f"repository = {json.dumps(PROJECT_REPOSITORY)}")
    lines += ["", "[packages]"]
    for name, rev in sorted(revs.items()):
        key = name if re.fullmatch(r"[A-Za-z0-9_-]+", name) else json.dumps(name)
        lines.append(f"{key} = {rev}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _update_lock(cwd: Path, installed: set[str], keep: list[str]) -> None:
    """Record installed revisions in texi.lock and report what changed since last time."""
    path = cwd / LOCK_NAME
    old = _read_lock(path)
    new = {n: r for n, r in old.items() if n in keep}
    new |= _package_revisions(cwd / ".texenv", sorted(installed))
    _write_lock(path, new)
    changes = [
        f"{n}: r{old[n]} → r{r}"
        for n, r in sorted(new.items())
        if n in old and old[n] != r
    ] + [f"{n}: new (r{r})" for n, r in sorted(new.items()) if n not in old and old]
    if changes:
        _note(title=f"{LOCK_NAME} changes", content=[escape(c) for c in changes])
    console.print(f"[dim]Wrote {LOCK_NAME} ({len(new)} packages)[/dim]")


def _tl_target() -> Path | None:
    """The TeX Live install root (e.g. /usr/local/texlive), or None if unwritable-check isn't needed.

    Returns None when the tree is already writable (or can't be found).
    """
    root = _run(["kpsewhich", "-var-value=TEXMFROOT"]).stdout.strip()
    if not root or os.access(Path(root) / "tlpkg", os.W_OK):
        return None
    return Path(root).parent  # so yearly upgrades keep working


def _chown_tl(target: Path) -> None:
    cmd = ["sudo", "chown", "-R", getpass.getuser(), str(target)]
    _debug_cmd(cmd)
    if subprocess.run(cmd).returncode != 0:
        console.print("[red]chown failed.[/red]")
        raise typer.Exit(code=1)


def _needs_sudo(what: str, *, yes: bool = False) -> bool:
    """Decide how to run a system TeX Live tool: True = prefix with sudo.

    Writable tree: no sudo. Otherwise offer a one-time chown (so TeX Live tools
    never need sudo again), falling back to sudo after a confirmation.
    """
    if (target := _tl_target()) is None:
        return False
    if not yes and _confirm(
        f"{target} is not writable, so `{what}` needs root. Run "
        f"`sudo chown -R {getpass.getuser()} {target}` so TeX Live tools run without sudo?",
        default=True,
    ):
        _chown_tl(target)
        return False
    if not (yes or _confirm(f"Run `sudo {what}` instead?", default=True)):
        raise typer.Exit(1)
    return True


def _ensure_latexmk() -> None:
    """Install latexmk into .texenv/ from the archive store if it's not there."""
    env_dir = Path.cwd() / ".texenv"
    standalone = _standalone(env_dir)
    env_bin = texenv.env_bin(env_dir) if standalone else None
    found = shutil.which("latexmk")
    if found and (env_bin is None or Path(found).parent == env_bin):
        return
    _archive_tools(["latexmk"], "latexmk")
    found = shutil.which("latexmk")
    if found and (env_bin is None or Path(found).parent == env_bin):
        return
    console.print("[red]Could not install latexmk from the archive.[/red]")
    raise typer.Exit(code=1)


def _archive_tools(packages: list[str], label: str) -> None:
    """Install `packages` into .texenv/ from the archive store; exits on failure."""
    env_dir = Path.cwd() / ".texenv"
    project = _peek_project() or ProjectConfig()
    with ShimmerProgress(console) as prog:
        result = _archive_install(
            prog,
            project.repository,
            env_dir,
            standalone=_standalone(env_dir),
            roots=packages,
            update=False,
        )
    _report_failed(result, label)
    _activate_env(env_dir)


def _report_failed(result: archive.Result, what: str) -> None:
    """Exit 1 if the archive install reported failed downloads or unknown packages."""
    failed = list(getattr(result, "failed", None) or [])
    if failed:
        console.print(
            f"[red]Could not install {escape(what)}: {escape(', '.join(map(str, failed)))}[/red]"
        )
        raise typer.Exit(code=1)


def _make4ht_missing() -> list[str]:
    """TeX Live packages make4ht needs that aren't usable yet."""
    missing = []
    if not shutil.which("make4ht"):
        missing += ["make4ht", "tex4ht"]
    if not tools.kpsewhich("luaxml-domobject.lua"):  # make4ht's domfilters require it
        missing.append("luaxml")
    return missing


def _ensure_make4ht() -> None:
    """Install make4ht and its luaxml dependency into .texenv/ from the archive store."""
    missing = _make4ht_missing()
    if not missing:
        return
    console.print(
        f"[yellow]make4ht is not usable; missing: {', '.join(missing)}[/yellow]"
    )
    _archive_tools(missing, "make4ht")
    if not _make4ht_missing():
        return
    console.print(f"[red]Could not install {', '.join(missing)}.[/red]")
    raise typer.Exit(code=1)


@app.command(
    help="Install and update every package listed in texproject.toml into the project's own .texenv/.",
    rich_help_panel="Project",
)
def sync(
    no_upgrade: bool = typer.Option(
        False,
        "--no-upgrade",
        help="Install only: don't fetch a newer package index or update installed packages.",
    ),
    debug: DebugOpt = False,
) -> None:
    r"""Creates a local texmf tree and installs project dependencies.
    fieldname "\t" value
    ...
    "end-of-header"
    pkgname status localrev serverrev size runtime esttot
    ...
    "end-of-updates"
    other output from post actions, not in machine readable form
    The header section currently has two fields: location-url (the repository source from which updates are being drawn), and total-bytes (the total number of bytes to be downloaded).

    The localrev and serverrev fields for each package are the revision numbers in the local installation and server repository, respectively. The size field is the number of bytes to be downloaded, i.e., the size of the compressed tar file for a network installation, not the unpacked size. The runtime and esttot fields are only present for updated and auto-install packages, and contain the currently passed time since start of installation/updates and the estimated total time.


    --command-logfile file
    --package-logfile file
    """
    _intro("texi sync")
    config = load_project_config(Path.cwd() / "texproject.toml")
    _apply_project(config)
    if not no_upgrade:
        _resolver_tlpdb(console, refresh=True)  # latest index: changed packages get updated
    _sync(config.dependencies, no_upgrade=no_upgrade)


def _target_arch(env_dir: Path) -> str | None:
    """TeX Live architecture of the tree: the env's `bin/<arch>`, else the platform's."""
    if (bin_dir := texenv.env_bin(env_dir)) is not None:
        return bin_dir.name
    return tlpdb_mod.system_arch()


def _archive_install(
    prog: ShimmerProgress,
    repository: str | None,
    env_dir: Path,
    *,
    standalone: bool,
    roots: list[str],
    update: bool,
    infrastructure: bool = False,
) -> archive.Result:
    """Install `roots` into .texenv/ from the archive store."""
    project = _peek_project()
    db = _resolver_tlpdb(console)
    if db is None:
        console.print(
            "[red]Package index unavailable (offline, no cached tlpdb); "
            "cannot install packages.[/red]"
        )
        raise typer.Exit(code=1)
    repo, _ = tlpdb_mod.resolve_repository(repository, None)
    if not standalone:
        texenv.init_usertree(env_dir, repo)  # no-op for an existing usertree

    def progress(label: str, done: int, total: int) -> None:
        if total:
            prog.on_progress(label, done, total)
        else:
            prog.on_progress(label)

    def log(msg: str) -> None:
        if DEBUG:
            console.print(f"[dim]{escape(msg)}[/dim]")

    result = archive.install_packages(
        db,
        repo,
        roots,
        env_dir,
        standalone=standalone,
        # a usertree sits over the system TeX Live: read its tlpdb
        system_installed=None if standalone else tlpdb_mod.system_installed,
        update=update,
        arch=_target_arch(env_dir),
        infrastructure=infrastructure or standalone,
        verify=bool(project and project.verify_checksums),
        progress=progress,
        log=log,
        notice=lambda m: console.print(f"[yellow]{escape(m)}[/yellow]"),
    )
    if result.installed:
        try:
            lsr.write_all(archive.mktexlsr_targets(env_dir, standalone=standalone))
        except lsr.LsrError as e:
            console.print(f"[yellow]{escape(str(e))}[/yellow]")
        n = result.cached
        console.print(
            f"[green]✔ Cloned {n} {inflect_engine.plural('package', n)} from cache, "
            f"downloaded {len(result.downloaded)}[/green]"
        )
    if result.recorded:
        console.print(
            f"[dim]Collections: {escape(', '.join(sorted(result.recorded)))}[/dim]"
        )
    if result.updated:
        console.print(f"[dim]Updated: {', '.join(sorted(result.updated))}[/dim]")
    for name in sorted(getattr(result, "skipped", None) or []):
        console.print(f"[dim]Skipped {escape(str(name))}[/dim]")
    return result


def _sync_archive(
    project: ProjectConfig,
    packages: list[str],
    *,
    no_upgrade: bool,
    keep: list[str] | None,
) -> None:
    """`texi sync`: nothing is written outside .texenv/."""
    env_dir = Path.cwd() / ".texenv"
    standalone = _standalone(env_dir)
    plural = inflect_engine.plural
    _ensure_latexmk()
    if not packages:
        console.print(
            "[yellow]No dependencies found in texproject.toml. Nothing to sync.[/yellow]"
        )
        return
    n = len(packages)
    console.print(
        f"[blue]Syncing {n} {plural('package', n)}:[/blue]\n"
        f"[dim]{', '.join(sorted(packages))}[/dim]"
    )
    with ShimmerProgress(console) as prog:
        result = _archive_install(
            prog,
            project.repository,
            env_dir,
            standalone=standalone,
            roots=packages,
            update=not no_upgrade,
        )
    if result.present:
        console.print(
            f"[green]✔ Skipping {len(result.present)} already installed "
            f"{plural('package', len(result.present))}[/green]"
        )
    roots = set(packages)
    for name in sorted(n for n in result.missing if n in roots):
        console.print(
            f"[yellow]Warning: Package '{escape(name)}' is not available in the "
            "current TeX Live repository.[/yellow]"
        )
    _report_failed(result, "packages")
    _activate_env(env_dir)  # arch binaries cloned into bin/<arch>
    if standalone or _standalone(env_dir):
        _install_tex_fmt(env_dir, update=not no_upgrade)
    # font maps and formats are handled by archive.install_packages (tlactions)
    synced = roots - set(result.missing)
    _update_lock(Path.cwd(), synced, keep if keep is not None else packages)
    _note(
        title=f"Synced {len(synced)} {plural('dependency', len(synced))} into .texenv/",
        content=[escape(", ".join(sorted(synced)))],
    )
    outro("[green]✔ Sync complete[/green]")


def _sync(
    packages: list[str], *, no_upgrade: bool, keep: list[str] | None = None
) -> None:
    """Install `packages` into .texenv/ (and update them unless no_upgrade)."""
    project = load_project_config(Path.cwd() / "texproject.toml").project
    _create_env(project.repository)
    _sync_archive(project, packages, no_upgrade=no_upgrade, keep=keep)


def _short_descriptions(names: list[str]) -> dict[str, str]:
    """Package name -> short description from the index. Unknown names are left out."""
    if not names:
        return {}
    db = _resolver_tlpdb(console)
    return db.shortdesc(names) if db is not None else {}


def _tl_package_exists(name: str) -> bool:
    """True if `name` is a TeX Live package in the index."""
    db = _resolver_tlpdb(console)
    return db is not None and db.has(name)


def _lookup_packages(names: list[str]) -> dict[str, list[str] | None]:
    """Map each name to None if it's a TeX Live package, else the packages providing a file by that name."""
    found: dict[str, list[str] | None] = {}
    with ShimmerProgress(console) as prog:
        prog.on_progress("Checking package index", 0, len(names))
        for i, name in enumerate(names, 1):
            if _tl_package_exists(name):
                found[name] = None
            else:
                prog.on_progress(f"Searching for {name}", i - 1, len(names))
                found[name] = find_bundle_for_sty(
                    name, console, exts=(".sty", ".cls", ".tex")
                )
            prog.on_progress("Checking package index", i, len(names))
    return found


def _choose_package(name: str, candidates: list[str]) -> str | None:
    """Ask which package to use for a name that isn't a TeX Live package.

    e.g. `binhex` is a file shipped inside the `kastrup` package.
    """
    console.print(f"[yellow]'{escape(name)}' is not a TeX Live package.[/yellow]")
    if not candidates:
        console.print(f"[red]No package provides {escape(name)}; skipping.[/red]")
        return None
    if not sys.stdin.isatty():
        console.print(
            f"[red]Provided by {', '.join(candidates)}; "
            "rerun with one of those names.[/red]"
        )
        return None
    if len(candidates) == 1:
        pkg = candidates[0]
        desc = _short_descriptions([pkg]).get(pkg)
        shown = f"'{pkg}' ({desc})" if desc else f"'{pkg}'"
        ok = _confirm(
            f"'{name}' is provided by {shown}. Add '{pkg}' instead?", default=True
        )
        return pkg if ok else None
    return _select(
        f"{name} is provided by several packages. Which one?",
        candidates,
        _short_descriptions(candidates),
    )


def _add_to_toml(path: Path, new: list[str]) -> None:
    """Append packages to the `dependencies` array, preserving the rest of the file."""
    text = path.read_text(encoding="utf-8")
    entries = "".join(f'  "{p}",\n' for p in new)
    if m := re.search(r"^dependencies\s*=\s*\[(.*?)\]", text, re.M | re.S):
        body = m.group(1).rstrip()
        if body and not body.endswith(","):
            body += ","
        body = body + "\n" if body else "\n"
        text = text[: m.start(1)] + body + entries + text[m.end(1) :]
    else:  # top-level keys must precede any [table]
        block = f"dependencies = [\n{entries}]\n"
        if h := re.search(r"^\[project\][^\n]*\n", text, re.M):
            text = text[: h.end()] + block + text[h.end() :]
        else:
            text = f"[project]\n{block}\n" + text
    path.write_text(text, encoding="utf-8")


def _resolve_names(names: list[str], verb: str) -> list[str]:
    """Resolve file-like names (binhex) to TeX Live packages, asking when ambiguous; exits 1 if none."""
    lookup = _lookup_packages(list(dict.fromkeys(names)))
    resolved = [n if c is None else _choose_package(n, c) for n, c in lookup.items()]
    packages = list(dict.fromkeys(r for r in resolved if r))
    if not packages:
        outro(f"[red]Nothing to {verb}.[/red]")
        raise typer.Exit(code=1)
    return packages


@app.command(
    help="Add packages to texproject.toml and install them. File names like binhex are resolved to their package.",
    rich_help_panel="Project",
)
def add(
    packages: Annotated[
        list[str], typer.Argument(help="Package names to add, e.g. acmart or biblatex.")
    ],
    sync_all: bool = typer.Option(
        False,
        "--sync",
        help="Afterwards, install and update all dependencies, as `texi sync` does.",
    ),
    debug: DebugOpt = False,
) -> None:
    """Add packages to texproject.toml and install them into .texenv/."""
    _intro("texi add")
    config_path = Path.cwd() / "texproject.toml"
    config = load_project_config(config_path)
    _apply_project(config)
    packages = _resolve_names(packages, "add")
    new = [p for p in packages if p not in config.dependencies]
    if new:
        _add_to_toml(config_path, new)
        console.print(f"[green]✔ Added to {config_path.name}:[/green] {', '.join(new)}")
    else:
        console.print("[dim]Already in dependencies.[/dim]")
    if sync_all:
        _sync([*config.dependencies, *new], no_upgrade=False)
    else:
        _sync(
            list(dict.fromkeys(packages)),
            no_upgrade=True,
            keep=[*config.dependencies, *new],
        )


@app.command(
    help="Install packages into .texenv/ without adding them to texproject.toml. File names like binhex are resolved to their package.",
    rich_help_panel="Project",
)
def install(
    packages: Annotated[
        list[str], typer.Argument(help="Package names to install, e.g. latexpand.")
    ],
    debug: DebugOpt = False,
) -> None:
    """Install packages into .texenv/; texproject.toml stays untouched."""
    _intro("texi install")
    config_path = Path.cwd() / "texproject.toml"
    config = load_project_config(config_path)
    _apply_project(config)
    packages = _resolve_names(packages, "install")
    locked = list(_read_lock(Path.cwd() / LOCK_NAME))  # keep earlier installs in texi.lock
    _sync(packages, no_upgrade=True, keep=[*locked, *config.dependencies, *packages])


_RUN_RE = re.compile(r"Run number (\d+) of rule '([^']+)'")


def _collect_diagnostics(config: TexProjectConfig, since: float) -> list[Diagnostic]:
    """Parse this run's TeX and biber/bibtex logs from the aux dir."""
    aux = Path.cwd() / config.build.aux_dir
    names = (
        [config.set_jobname]
        if config.set_jobname
        else [Path(f).stem for f in config.main_files]
    )
    diags: list[Diagnostic] = []
    for name in names:
        for ext, parse in (
            (".log", parse_tex_log),
            (
                ".blg",
                lambda t, n=name: parse_blg(
                    t, "biber" if (aux / f"{n}.bcf").exists() else "bibtex"
                ),
            ),
        ):
            path = aux / f"{name}{ext}"
            if path.is_file() and path.stat().st_mtime >= since - 2:
                diags += parse(path.read_text(encoding="utf-8", errors="replace"))
        _resolve_biber_temp_files(diags, config, aux / f"{name}.bcf")
    _explain_runaways(diags, config)
    return diags


def _explain_runaways(diags: list[Diagnostic], config: TexProjectConfig) -> None:
    """For "File ended while scanning use of \\x": point at where the argument starts."""
    todo = [
        d
        for d in diags
        if d.kind == "error"
        and d.code in ("file-ended", "paragraph-ended")
        and runaway.runaway_text(d.stack)
    ]
    if not todo:
        return
    cwd = Path.cwd()
    sources = {
        str(f): f.read_text(encoding="utf-8", errors="replace")
        for f in _source_files(cwd)
    }
    for d in todo:
        found = runaway.locate(
            runaway.runaway_text(d.stack) or "", runaway.macro_of(d.message), sources
        )
        if found:
            d.file, d.line = found.file, found.line
            d.hint = found.hint()


def _find_project_file(name: str, config: TexProjectConfig) -> Path | None:
    """A file named in a log: next to the project, in src_dir, or in a bib_dirs entry."""
    cwd = Path.cwd()
    for base in (cwd, cwd / config.src_dir, *(cwd / d for d in config.build.bib_dirs)):
        if (found := base / name).is_file():
            return found
    return None


def _resolve_biber_temp_files(
    diags: list[Diagnostic], config: TexProjectConfig, bcf: Path
) -> None:
    """Point biber's `<md5>` temp-file diagnostics at the real .bib, with a hint."""
    temp = [d for d in diags if logparse_bib.is_biber_temp_name(d.file)]
    if not temp or not bcf.is_file():
        return
    sources = logparse_bib.biber_temp_sources(bcf.read_text(encoding="utf-8", errors="replace"))
    for d in temp:
        if (name := sources.get(d.file or "")) and (real := _find_project_file(name, config)):
            d.file = str(real)
            lines = real.read_text(encoding="utf-8", errors="replace").splitlines()
            d.hint = logparse_bib.explain_syntax(d, lines)


# Off for now: a `file://` URL cannot carry the line number, so terminals open the file at the top.
FILE_LINKS = False


def _file_link(d: Diagnostic, config: TexProjectConfig) -> str | None:
    """`file://` URL of the diagnostic's file if it exists (for a clickable location)."""
    if not FILE_LINKS or not d.file:
        return None
    path = Path(d.file)
    if not path.is_absolute():
        path = Path.cwd() / config.src_dir / path
    return path.resolve().as_uri() if path.is_file() else None


def _linked(text: str, url: str | None) -> str:
    """Rich markup that makes `text` a terminal hyperlink to `url`."""
    return f"[link={url}]{escape(text)}[/link]" if url else escape(text)


def _location(d: Diagnostic, config: TexProjectConfig) -> str:
    if not d.file:
        return d.source if d.source != "tex" else ""
    path = Path(d.file)
    cwd = Path.cwd()
    if not path.is_absolute():
        path = (
            Path(config.src_dir) / path
        )  # the log is relative to src_dir (latexmk $do_cd)
    else:
        rel = Path(os.path.relpath(os.path.realpath(path), os.path.realpath(cwd)))
        if not rel.parts or rel.parts[0] != "..":  # inside the project
            path = rel
    loc = os.path.normpath(path)
    if not d.line:
        return loc
    lines = f"{d.line}-{d.end_line}" if d.end_line else str(d.line)
    return f"{loc}:{'~' if d.guessed else ''}{lines}"


def _print_diagnostics(
    diags: list[Diagnostic], config: TexProjectConfig, all_warnings: bool = False
) -> None:
    errors = dedupe([d for d in diags if d.kind == "error"])
    if len(errors) > 1:  # TeX's follow-up fatal messages say nothing new
        errors = [
            e
            for e in errors
            if not e[0].message.lstrip().startswith(("Emergency stop", "==> Fatal"))
        ]
    warnings = dedupe([d for d in diags if d.kind == "warning"])
    boxes = [d for d in diags if d.kind == "box"]
    for d, n in errors:
        loc = _location(d, config)
        console.print(
            f"[red]✖[/red] [bold]{_linked(loc, _file_link(d, config))}[/bold]  {escape(d.message)}"
            if loc
            else f"[red]✖[/red] {escape(d.message)}"
        )
        if d.hint:
            console.print(f"    [cyan]→ {escape(d.hint)}[/cyan]")
        for c in d.context:
            console.print(f"    [dim]{escape(c)}[/dim]")
        if pkg := missing_package(d):
            console.print(f"    [cyan]→ texi add {escape(pkg)}[/cyan]")
    shown = warnings if all_warnings else warnings[:15]
    for d, n in shown:
        loc = _location(d, config)
        times = f" [dim](x{n})[/dim]" if n > 1 else ""
        console.print(
            f"[yellow]⚠[/yellow] [bold]{_linked(loc, _file_link(d, config))}[/bold]  {escape(d.message)}{times}"
            if loc
            else f"[yellow]⚠[/yellow] {escape(d.message)}{times}"
        )
        if d.hint:
            console.print(f"    [cyan]→ {escape(d.hint)}[/cyan]")
        if d.source == "tex-fmt":
            for c in d.context:
                console.print(f"    [dim]{escape(c[:100])}[/dim]")
    if len(warnings) > len(shown):
        console.print(
            f"[dim]  … and {len(warnings) - len(shown)} more warnings (--all-warnings lists them)[/dim]"
        )
    parts = [
        f"{len(errors)} {plural_word('error', len(errors))}",
        f"{len(warnings)} {plural_word('warning', len(warnings))}",
    ]
    shown_boxes = boxes if all_warnings else boxes[:5]
    for d in shown_boxes:
        loc = _location(d, config)
        console.print(
            f"[dim]▫ {escape(loc + '  ' if loc else '')}{escape(d.message)}[/dim]"
        )
    if len(boxes) > len(shown_boxes):
        console.print(
            f"[dim]  … and {len(boxes) - len(shown_boxes)} more boxes "
            "(--all-warnings lists them)[/dim]"
        )
    if boxes:
        parts.append(
            f"{len(boxes)} over/underfull {plural_word('box', len(boxes), 'boxes')}"
        )
    console.print(f"[dim]{', '.join(parts)}[/dim]")


def plural_word(word: str, n: int, plural: str | None = None) -> str:
    return word if n == 1 else (plural or word + "s")


def _report(
    config: TexProjectConfig,
    since: float,
    output: str,
    guess: bool,
    returncode: int | None = None,
    all_warnings: bool = False,
) -> bool:
    """Print the diagnostics of one latexmk run; returns whether any error was found."""
    diags = _collect_diagnostics(config, since)
    if returncode is not None and not any(d.kind == "error" for d in diags):
        # the logs hold no error: latexmk's own output may say what went wrong
        diags += [
            d
            for d in parse_latexmk(output, exit_code=returncode).diagnostics
            if d.kind == "error"
            and not d.message.startswith(
                "latexmk reported a failure"
            )  # generic fallback
        ]
    if guess:
        guess_lines(diags)
    _print_diagnostics(diags, config, all_warnings)
    if "gave an error in previous invocation" in output:
        console.print(
            "[yellow]latexmk skipped recompiling after an earlier failure; "
            "rerun with --full.[/yellow]"
        )
    return any(d.kind == "error" for d in diags)


def _pdf_path(config: TexProjectConfig) -> Path:
    """Where latexmk leaves the final PDF."""
    final_dir = config.build.out2_dir or config.build.out_dir
    return Path.cwd() / final_dir / f"{config.jobname}.pdf"


def _warn_type3(pdf: Path) -> None:
    """Orange warning if the PDF has Type 3 fonts. Never raises: a broken PDF must not fail a build."""
    try:
        if not pdf.is_file():
            return
        fonts = pdffonts.find_type3(pdf)
    except Exception as e:
        console.print(
            f"[dim]Could not check {escape(pdf.name)} for Type 3 fonts: {escape(str(e))}[/dim]"
        )
        return
    if not fonts:
        return
    names = ", ".join(f.name for f in fonts)
    console.print(
        f"[orange1]⚠ {len(fonts)} Type 3 font(s) in {escape(str(pdf.name))}: "
        f"{escape(names)}. ACM does not accept Type 3 fonts.[/orange1]"
    )
    console.print(
        '[dim]  matplotlib: plt.rcParams["pdf.fonttype"] = 42 (then re-export the figure)[/dim]'
    )
    console.print("[dim]  Run `texi fix-pdf` for options.[/dim]")


def _check_type3(config: TexProjectConfig) -> None:
    if config.build.check_type3:
        _warn_type3(_pdf_path(config))


def _watch_captured(
    cmd: list[str],
    env: dict[str, str],
    config: TexProjectConfig,
    guess: bool,
    rc_input: str | None = None,
    all_warnings: bool = False,
) -> None:
    """Run `latexmk -pvc`, printing a diagnostics summary after every rebuild."""
    proc = _popen_captured(cmd, env, rc_input)
    assert proc.stdout is not None
    buf: list[str] = []
    started = time.time()
    try:
        for line in proc.stdout:
            if not buf:
                console.print("[bold blue]Compiling…[/bold blue]")
            buf.append(line)
            if "Watching for updated files" in line:  # latexmk is idle: a cycle ended
                output = "".join(buf)
                failed = _report(
                    config, started, output, guess, all_warnings=all_warnings
                ) or (
                    "Errors, in force_mode" in output or "Failure" in output
                )
                if not failed:
                    _check_type3(config)
                console.print(
                    "[red]✖ Build failed.[/red]"
                    if failed
                    else "[green]✔ Built.[/green]"
                )
                console.print("[dim]Watching for changes (Ctrl-C to stop)…[/dim]\n")
                buf.clear()
                started = time.time()
    except KeyboardInterrupt:
        pass
    finally:
        proc.terminate()
        proc.wait()


def _popen_captured(
    cmd: list[str], env: dict[str, str], rc_input: str | None
) -> subprocess.Popen[str]:
    """Start latexmk with merged, piped output; feed rc_input on stdin if given."""
    proc = subprocess.Popen(
        cmd,
        env=env,
        stdin=subprocess.PIPE if rc_input is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
    )
    if rc_input is not None:
        assert proc.stdin is not None
        proc.stdin.write(rc_input)
        proc.stdin.close()  # latexmk reads the rc until EOF
    return proc


def _run_captured(
    cmd: list[str], env: dict[str, str], rc_input: str | None = None
) -> tuple[int, str]:
    """Run latexmk, hiding its output behind a progress line; returns (exit code, output)."""
    lines: list[str] = []
    with ShimmerProgress(console) as prog:
        prog.on_progress("Compiling")
        proc = _popen_captured(cmd, env, rc_input)
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.append(line)
            if m := _RUN_RE.search(line):
                prog.on_progress(f"{m.group(2).split()[0]} (run {m.group(1)})")
        return proc.wait(), "".join(lines)


def _run_latexmk(
    *,
    strict: bool,
    preview: bool,
    full: bool = False,
    clean: str | None = None,
    silent: bool = False,
    diagnostics: bool = False,
    raw: bool = True,
    guess: bool = False,
    all_warnings: bool = False,
) -> None:
    """Generate .latexmkrc (with CLI overrides) and run latexmk in the project."""
    cwd = Path.cwd()
    env_dir = cwd / ".texenv"

    config = load_project_config(cwd / "texproject.toml")
    execution = config.build
    execution.halt_on_error = execution.halt_on_error or strict
    if full:
        execution.go_mode = GoMode.FULL_REBUILD
    execution.silent = execution.silent or silent
    execution.diagnostics = execution.diagnostics or diagnostics
    parse_output = not (raw or clean)
    if parse_output:
        execution.file_line_error = True  # lets the log parser report file:line
    execution.force_mode = execution.force_mode and not strict
    execution.preview_mode = False
    execution.preview_continuous_mode = preview
    rc_text = render_latexmkrc(config)
    rc_input: str | None = None
    if execution.write_latexmkrc or not PIPE_RC_SUPPORTED:
        rc_path = cwd / ".latexmkrc"
        rc_path.write_text(rc_text, encoding="utf-8")
        rc_arg = str(rc_path)
        if not execution.silent:
            console.print("[dim]Generated .latexmkrc from texproject.toml[/dim]")
    else:
        rc_input = rc_text
        rc_arg = "/dev/stdin"
        if not execution.silent:
            console.print("[dim]Piping generated latexmkrc to latexmk[/dim]")

    if MAX_PRINT_LINE > BIBTEX_LINE_LIMIT and config.build.bibtex_use != "never":
        console.print(
            f"[yellow]Warning:[/yellow] max_print_line is {MAX_PRINT_LINE}, above "
            f"BibTeX's line limit ({BIBTEX_LINE_LIMIT}); bibtex may fail with "
            "buffer-size errors."
        )

    env = os.environ.copy()
    if env_dir.exists():
        if _standalone(env_dir):
            for name in texenv.SCRUBBED:
                env.pop(name, None)
        env.update(_texenv_vars(env_dir))
        env["max_print_line"] = str(MAX_PRINT_LINE)  # Avoid line wrapping in logs
        env["error_line"] = "254"  # longest allowed, keeps error context whole
        env["half_error_line"] = "238"
        if not execution.silent:
            console.print("[dim]Using local .texenv for compilation[/dim]")
    else:
        console.print(
            "[yellow]Warning: .texenv not found. Run `texi sync` for isolated dependencies.[/yellow]"
        )

    if not _standalone(env_dir):
        _ensure_latexmk()
    elif not (
        (b := texenv.env_bin(env_dir)) and (b / "latexmk").exists()
    ):  # never fall back to the system latexmk
        console.print(
            "[red]latexmk not found in .texenv/. Run `texi sync` to install it.[/red]"
        )
        raise typer.Exit(code=1)
    if DEBUG:
        console.print(f"[dim]latexmk: {escape(str(shutil.which('latexmk')))}[/dim]")
        changed = {k: v for k, v in env.items() if os.environ.get(k) != v}
        if config.src_dir not in ("", "."):  # set by the generated .latexmkrc
            src = f"{cwd}/{config.src_dir.strip('/')}//:"
            for k in ("BIBINPUTS", "TEXINPUTS"):
                changed[f"{k} (in .latexmkrc)"] = src + os.environ.get(k, "")
        for k, v in changed.items():
            console.print(f"[dim]env {escape(k)}={escape(v)}[/dim]")
    cmd = ["latexmk", *(["-norc", "-r", rc_arg] if EXPLICIT_RC else [])]
    targets = [str(Path(config.src_dir) / f) for f in config.main_files]
    if missing := [t for t in targets if not (cwd / t).is_file()]:
        console.print(
            f"[red]Error:[/red] source file(s) not found: {', '.join(missing)}"
        )
        raise typer.Exit(code=1)
    if clean:
        cmd.append(clean)
    elif (
        not strict and not execution.silent
    ):  # silent mode runs the engines in batchmode
        cmd.append("-interaction=nonstopmode")
    _debug_cmd(cmd)
    if not execution.silent:
        console.print(f"[bold blue]Running latexmk {clean or ''}...[/bold blue]\n")
    if parse_output and preview:
        _watch_captured(cmd, env, config, guess, rc_input, all_warnings)
        return
    if parse_output:
        started = time.time()
        returncode, output = _run_captured(cmd, env, rc_input)
        if (
            returncode != 0
            and not full
            and not clean
            and "gave an error in previous invocation" in output
        ):
            # latexmk refuses to retry a rule that failed last time; that is
            # rarely what the user wants (the cause, e.g. a missing package, is fixed)
            console.print(
                "[yellow]latexmk skipped recompiling after an earlier failure; "
                "rebuilding with --full.[/yellow]"
            )
            _run_latexmk(
                strict=strict,
                preview=preview,
                full=True,
                clean=clean,
                silent=silent,
                diagnostics=diagnostics,
                raw=raw,
                guess=guess,
                all_warnings=all_warnings,
            )
            return
        had_errors = _report(
            config, started, output, guess, returncode, all_warnings
        )
        if returncode != 0:
            if not had_errors:  # nothing parsed: show the tail
                tail = "".join(output.splitlines(keepends=True)[-25:])
                console.print(f"[dim]{escape(tail)}[/dim]")
                console.print("[dim]Run with --raw for the full latexmk output.[/dim]")
            console.print("\n[red]✖ Build failed.[/red]")
            raise typer.Exit(code=1)
        _check_type3(config)
        outro("[green]✔ Build completed successfully![/green]")
        return
    try:
        subprocess.run(cmd, env=env, check=True, input=rc_input, text=True)
    except subprocess.CalledProcessError as e:
        what = "Clean" if clean else "Build"
        console.print(f"\n[red]✖ {what} failed. Check the logs above.[/red]")
        raise typer.Exit(code=1) from e
    except KeyboardInterrupt:  # normal way to leave -pvc
        if not preview:
            raise
        return
    if not clean:
        _check_type3(config)
    outro(
        "[green]✔ Cleaned[/green]"
        if clean
        else "[green]✔ Build completed successfully![/green]"
    )


StrictOpt = Annotated[
    bool,
    typer.Option(
        "--strict",
        help="Stop at the first error instead of pressing on to produce a PDF.",
    ),
]


FullOpt = Annotated[
    bool,
    typer.Option(
        "--full",
        help="Rebuild everything, even if latexmk thinks nothing changed (go_mode = full-rebuild).",
    ),
]
SilentOpt = Annotated[
    bool,
    typer.Option(
        "--silent", "-s", help="Reduce output to errors and the final status."
    ),
]
DiagnosticsOpt = Annotated[
    bool,
    typer.Option(
        "--diagnostics", help="Show latexmk's internal decisions (very verbose)."
    ),
]
RawOpt = Annotated[
    bool,
    typer.Option(
        "--raw", help="Show latexmk's full output instead of a cleaned-up summary."
    ),
]
GuessOpt = Annotated[
    bool,
    typer.Option(
        "--guess-lines",
        help="Estimate a line (marked ~) for warnings that TeX gives none.",
    ),
]
AllWarningsOpt = Annotated[
    bool,
    typer.Option(
        "--all-warnings",
        help="List every warning and over/underfull box, not just the first few.",
    ),
]
RmPdfOpt = Annotated[
    bool,
    typer.Option(
        "--rm-pdf",
        help="Delete the old PDF first, so a failed build leaves none behind.",
    ),
]


def _remove_pdf() -> None:
    config = load_project_config(Path.cwd() / "texproject.toml")
    output_file = _pdf_path(config)
    if output_file.exists():
        console.print(f"[dim]Removing existing output file: {output_file}[/dim]")
        output_file.unlink()


@app.command(
    help="Compile the project to PDF with latexmk, using the settings in texproject.toml.",
    rich_help_panel="Build",
)
def build(
    strict: StrictOpt = False,
    full: FullOpt = False,
    silent: SilentOpt = False,
    diagnostics: DiagnosticsOpt = False,
    raw: RawOpt = False,
    guess: GuessOpt = False,
    all_warnings: AllWarningsOpt = False,
    rm_pdf: RmPdfOpt = False,
    debug: DebugOpt = False,
) -> None:
    """Generate .latexmkrc and compile the project."""
    _intro("texi build")
    if rm_pdf:
        _remove_pdf()
    _run_latexmk(
        strict=strict,
        preview=False,
        full=full,
        silent=silent,
        diagnostics=diagnostics,
        raw=raw,
        guess=guess,
        all_warnings=all_warnings,
    )


def texfmt_config(cfg: TexFmtConfig) -> str:
    """Render `[tex-fmt]` as a tex-fmt config (flat, hyphenated keys)."""
    lines = []
    for key, value in cfg.model_dump().items():
        if value is None or value == []:
            continue
        if isinstance(value, bool):
            value = str(value).lower()
        elif isinstance(value, list):
            value = "[" + ", ".join(json.dumps(v) for v in value) + "]"
        elif isinstance(value, str):
            value = json.dumps(value)
        lines.append(f"{key.replace('_', '-')} = {value}")
    return "\n".join(lines) + "\n"


def _tex_fmt_binary(env_dir: Path) -> Path | None:
    """The project's own tex-fmt (installed on demand into a standalone tree), else the one on PATH."""
    if (b := texenv.env_bin(env_dir)) and (own := b / texfmt.BINARY).is_file():
        return own
    if _standalone(env_dir) and (own := _install_tex_fmt(env_dir, update=False)):
        return own
    return Path(found) if (found := shutil.which("tex-fmt")) else None


@app.command(
    help="Tidy your .tex files with tex-fmt, using the \\[tex-fmt] settings in texproject.toml.",
    rich_help_panel="Build",
)
def fmt(
    files: Annotated[
        list[Path] | None,
        typer.Argument(help="Files to format. Default: every .tex file under src_dir."),
    ] = None,
    check: bool = typer.Option(
        False,
        "--check",
        "-c",
        help="Only report files that need formatting; change nothing.",
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Show tex-fmt's info messages."
    ),
    quiet: bool = typer.Option(
        False,
        "--quiet",
        "-q",
        help="Print nothing, not even tex-fmt's output; only the exit code reports failure. Not with --check.",
    ),
    trace: bool = typer.Option(False, "--trace", help="Show tex-fmt's trace messages."),
    error: bool = typer.Option(
        False, "--error", help="Show only errors."
    ),
    warn: bool = typer.Option(
        False, "--warn", help="Show errors and warnings (tex-fmt's default)."
    ),
    info: bool = typer.Option(
        False, "--info", help="Show info messages too (same as --verbose)."
    ),
    raw: bool = typer.Option(
        False,
        "--raw",
        help="Print tex-fmt's output as is, without parsing it. --debug does the same.",
    ),
    debug: DebugOpt = False,
) -> None:
    """Format .tex files with tex-fmt, using settings from [tex-fmt] in texproject.toml."""
    levels = [
        level
        for level, on in (
            ("info", verbose or info),
            ("error", error),
            ("trace", trace),
            ("warn", warn),
        )
        if on
    ]
    if len(levels) > 1:
        raise typer.BadParameter(
            "Use only one of --error, --warn, --info/--verbose and --trace."
        )
    if quiet and check:
        raise typer.BadParameter("--quiet cannot be combined with --check.")
    if not quiet:
        _intro("texi fmt")
    cwd = Path.cwd()
    config = load_project_config(cwd / "texproject.toml")
    if levels:
        config.tex_fmt.verbosity = levels[0]
    tex_fmt = _tex_fmt_binary(cwd / ".texenv")
    if tex_fmt is None:
        console.print(
            "[red]tex-fmt not found.[/red] Run `texi sync` to install it into .texenv/, "
            "or `brew install tex-fmt` / `cargo install tex-fmt`."
        )
        raise typer.Exit(code=1)
    skip_dirs = [
        cwd / d
        for d in (config.build.out_dir, config.build.aux_dir, config.build.out2_dir)
        if d
    ]
    targets = files or sorted(
        f
        for f in (cwd / config.src_dir).rglob("*.tex")
        # never the packages in .texenv/ or other hidden dirs, nor build output
        if not any(
            p.startswith(".") for p in f.relative_to(cwd / config.src_dir).parts[:-1]
        )
        and not any(d in f.parents for d in skip_dirs)
    )
    if not targets:
        if not quiet:
            console.print("[yellow]No .tex files found.[/yellow]")
        return
    # The config goes in over stdin, so no temp file. --config alone is enough: it
    # takes precedence over any tex-fmt.toml on disk, while --noconfig would
    # ignore it too.
    cmd = [
        str(tex_fmt),
        "--config",
        "/dev/stdin",
        *(["--check"] if check else []),
        *map(str, targets),
    ]
    _debug_cmd(cmd)
    run = subprocess.run(
        cmd,
        input=texfmt_config(config.tex_fmt),
        text=True,
        stdout=subprocess.DEVNULL if quiet else None,
        # tex-fmt logs to stderr: captured and shown in texi's own style, unless --raw/--debug
        stderr=subprocess.DEVNULL if quiet else None if (raw or DEBUG) else subprocess.PIPE,
    )
    if not quiet and not (raw or DEBUG):
        diags = parse_texfmt(run.stderr or "", formatted=not check)  # the file changed unless --check
        for d in diags:
            if d.file and not Path(d.file).is_absolute():
                d.file = str(cwd / d.file)  # tex-fmt echoes the path as given
        for d in diags:
            if d.kind == "info":
                loc = _location(d, config)
                where = f"{_linked(loc, _file_link(d, config))}  " if loc else ""
                console.print(f"[dim]· {where}{escape(d.message)}[/dim]")
                for c in d.context:
                    console.print(f"    [dim]{escape(c[:100])}[/dim]")
        _print_diagnostics(diags, config, all_warnings=True)
    if run.returncode != 0:
        if not quiet:
            console.print("[red]✖ tex-fmt reported problems.[/red]")
        raise typer.Exit(code=1)
    if quiet:
        return
    outro(
        "[green]✔ Formatting is up to date[/green]"
        if check
        else "[green]✔ Formatted[/green]"
    )


class ExportFormat(str, Enum):
    MARKDOWN = "markdown"  # pandoc
    HTML = "html"  # pandoc
    TEX4HT = "tex4ht"  # make4ht
    LATEXML = "latexml"  # latexmlc
    EXPAND = "expand"  # latexpand: one .tex file with every \input inlined


_EXPORT_HINTS = {
    "pandoc": "brew install pandoc",
    "make4ht": "texi add make4ht tex4ht luaxml",
    "latexmlc": "brew install latexml",
    "latexpand": "texi add latexpand",
}


@app.command(
    help="Convert the document to Markdown or HTML (pandoc, TeX4ht or LaTeXML), or expand it into one .tex file (latexpand), in build/export/.",
    rich_help_panel="Build",
)
def export(
    fmt: Annotated[
        ExportFormat,
        typer.Argument(
            help="markdown or html (pandoc), tex4ht (make4ht), latexml (latexmlc), expand (latexpand)."
        ),
    ],
    debug: DebugOpt = False,
) -> None:
    """Export every file in `targets` to Markdown/HTML via pandoc, TeX4ht (make4ht) or LaTeXML, or expand it with latexpand."""
    _intro(f"texi export {fmt.value}")
    cwd = Path.cwd()
    config = load_project_config(cwd / "texproject.toml")
    src = (cwd / config.src_dir).resolve()
    mains = config.main_files
    for main in mains:
        if not (src / main).is_file():
            console.print(f"[red]Error:[/red] source file not found: {src / main}")
            raise typer.Exit(code=1)
    out_dir = cwd / "build" / "export" / fmt.value
    aux_dir = (cwd / config.build.aux_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    bibs = sorted(src.rglob("*.bib"))

    tool = {
        ExportFormat.MARKDOWN: "pandoc",
        ExportFormat.HTML: "pandoc",
        ExportFormat.TEX4HT: "make4ht",
        ExportFormat.LATEXML: "latexmlc",
        ExportFormat.EXPAND: "latexpand",
    }[fmt]
    if fmt is ExportFormat.TEX4HT:
        _ensure_make4ht()
    env_dir = cwd / ".texenv"
    if fmt is ExportFormat.EXPAND and not shutil.which(tool) and env_dir.exists():
        console.print("[yellow]latexpand not found; installing it into .texenv/[/yellow]")
        _archive_tools([tool], tool)
    if not shutil.which(tool):
        console.print(f"[red]{tool} not found.[/red] Try `{_EXPORT_HINTS[tool]}`.")
        raise typer.Exit(code=1)

    env = os.environ.copy()
    if env_dir.exists():
        env.update(_texenv_vars(env_dir))
    targets: list[Path] = []
    for main in mains:
        name = Path(main).stem  # one output per target
        match fmt:
            case ExportFormat.MARKDOWN | ExportFormat.HTML:
                cmd = [tool, main, "-f", "latex"]
                if fmt is ExportFormat.HTML:
                    cmd += ["-t", "html5", "-s", "--math-method=mathjax"]
                    target = out_dir / f"{name}.html"
                else:
                    cmd += ["-t", "markdown"]
                    target = out_dir / f"{name}.md"
                if bibs:
                    cmd += ["--citeproc", *(f"--bibliography={b}" for b in bibs)]
                cmd += ["-o", str(target)]
            case ExportFormat.TEX4HT:
                target = out_dir
                # make4ht mangles absolute -B/-d paths (nests them under the build dir
                # and leaves output in the source dir), so pass them relative to src.
                rel = lambda p: os.path.relpath(p, src)  # noqa: E731
                cmd = [tool, "-u", "-B", rel(aux_dir), "-d", rel(target), main, "mathml"]
            case ExportFormat.LATEXML:
                target = out_dir / f"{name}.html"
                cmd = [tool, main, f"--dest={target}"]
            case ExportFormat.EXPAND:
                target = out_dir / f"{name}.tex"
                cmd = [tool, main, "-o", str(target)]
        _debug_cmd(cmd)
        try:
            subprocess.run(
                cmd, cwd=src, env=env, check=True
            )  # run in src_dir so \input/.bib resolve
        except subprocess.CalledProcessError as e:
            console.print(f"\n[red]✖ {tool} failed on {escape(main)}. Check the logs above.[/red]")
            raise typer.Exit(code=1) from e
        targets.append(target)
    shown = ", ".join(escape(str(t.relative_to(cwd))) for t in dict.fromkeys(targets))
    outro(f"[green]✔ Exported to {shown}[/green]")


@app.command(
    help="Delete generated build files (latexmk -c). Add --all to delete the PDF too (-C).",
    rich_help_panel="Build",
)
def clean(
    all_files: bool = typer.Option(
        False, "--all", help="Also delete the PDF and other final outputs (latexmk -C)."
    ),
    debug: DebugOpt = False,
) -> None:
    """Delete generated files with latexmk."""
    _intro("texi clean")
    _run_latexmk(strict=False, preview=False, clean="-C" if all_files else "-c")


@app.command(
    help="Build, open the PDF viewer, and rebuild automatically whenever you save. Ctrl-C to stop.",
    rich_help_panel="Build",
)
def preview(
    strict: StrictOpt = False,
    full: FullOpt = False,
    silent: SilentOpt = False,
    diagnostics: DiagnosticsOpt = False,
    raw: RawOpt = False,
    guess: GuessOpt = False,
    all_warnings: AllWarningsOpt = False,
    rm_pdf: RmPdfOpt = False,
    debug: DebugOpt = False,
) -> None:
    """Build, open the viewer, and rebuild on every change (latexmk -pvc). Ctrl-C to stop."""
    _intro("texi preview")
    if rm_pdf:
        _remove_pdf()
    _run_latexmk(
        strict=strict,
        preview=True,
        full=full,
        silent=silent,
        diagnostics=diagnostics,
        raw=raw,
        guess=guess,
        all_warnings=all_warnings,
    )


class FixMethod(str, Enum):
    ADVISE = "advise"
    OUTLINE = "outline"
    REBUILD = "rebuild"


def _print_type3_report(fonts: list[pdffonts.Type3Font]) -> None:
    for f in fonts:
        pages = ", ".join(map(str, f.pages[:15])) + (" …" if len(f.pages) > 15 else "")
        extra = f"; {f.origin}" if f.origin else ""
        console.print(
            f"[orange1]•[/orange1] [bold]{escape(f.name)}[/bold]  "
            f"{f.kind or 'unclassified'}{escape(extra)}  [dim](pages {pages})[/dim]"
        )


def _fix_outline(pdf: Path, output: Path | None, in_place: bool) -> None:
    out = pdf.with_suffix(".fixed.pdf") if output is None else output
    if in_place:
        out = pdf.with_name(pdf.stem + ".texi-tmp.pdf")
    report = pdffonts.convert_outlines(pdf, out)
    if in_place:
        shutil.copy2(pdf, pdf.with_suffix(pdf.suffix + ".bak"))
        out.replace(pdf)
        report.output = pdf
    if report.converted:
        console.print(
            f"[green]✔ Converted {len(report.converted)} vector font(s) to TrueType: "
            f"{escape(', '.join(f.name for f in report.converted))}[/green]"
        )
    for f in report.skipped:
        why = report.errors.get(f.xref) or f.kind
        console.print(
            f"[yellow]Left alone: {escape(f.name)} ({escape(why)}); "
            "bitmap fonts cannot be converted.[/yellow]"
        )
    console.print(f"[dim]Wrote {escape(str(report.output))}[/dim]")
    if report.remaining:
        console.print(f"[red]✖ {len(report.remaining)} Type 3 font(s) remain:[/red]")
        _print_type3_report(report.remaining)
        for line in pdffonts.suggestion_text():
            console.print(f"[dim]{escape(line)}[/dim]")
        raise typer.Exit(code=1)
    outro("[green]✔ No Type 3 fonts left[/green]")


def _fix_rebuild(config: TexProjectConfig, pdf: Path) -> None:
    try:
        have_t1 = bool(tools.kpsewhich("sfrm1000.pfb"))  # cm-super's Type 1 fonts
    except sh.ErrorReturnCode, sh.CommandNotFound:
        have_t1 = False
    if have_t1:
        console.print("[green]✔ cm-super Type 1 fonts found[/green]")
    else:
        console.print(
            "[yellow]cm-super Type 1 fonts not found. Install with: "
            "texi add cm-super, and use "
            f"{escape(chr(92) + 'usepackage[T1]{fontenc}')}.[/yellow]"
        )
    console.print(f"[dim]pdf_mode is {config.build.pdf_mode.value}[/dim]")
    _run_latexmk(strict=False, preview=False, raw=False)
    fonts = pdffonts.classify(pdf) if pdf.is_file() else []
    if fonts:
        console.print(f"[red]✖ {len(fonts)} Type 3 font(s) remain:[/red]")
        _print_type3_report(fonts)
        raise typer.Exit(code=1)
    console.print("[green]✔ No Type 3 fonts after rebuild[/green]")


@app.command(
    name="fix-pdf",
    help="Find Type 3 fonts in a PDF (ACM rejects them) and fix or explain them.",
    rich_help_panel="Build",
)
def fix_pdf(
    pdf: Annotated[
        Path | None,
        typer.Argument(help="PDF to check. Default: the project's built PDF."),
    ] = None,
    method: Annotated[
        FixMethod,
        typer.Option(
            "--method",
            "-m",
            help="advise: report and explain (default). outline: convert vector "
            "Type 3 fonts to TrueType. rebuild: check TeX fonts and rebuild.",
        ),
    ] = FixMethod.ADVISE,
    output: Annotated[
        Path | None,
        typer.Option(
            "--output", "-o", help="Output for outline (default: <name>.fixed.pdf)."
        ),
    ] = None,
    in_place: Annotated[
        bool,
        typer.Option(
            "--in-place", help="outline: overwrite the PDF, keeping a .bak copy."
        ),
    ] = False,
    debug: DebugOpt = False,
) -> None:
    """Detect Type 3 fonts and suggest or apply a fix."""
    _intro("texi fix-pdf")
    config = None
    if pdf is None or method is FixMethod.REBUILD:
        config = load_project_config(Path.cwd() / "texproject.toml")
    if pdf is None:
        assert config is not None
        pdf = _pdf_path(config)
    if not pdf.is_file():
        console.print(f"[red]Error:[/red] PDF not found: {escape(str(pdf))}")
        raise typer.Exit(code=1)
    if method is FixMethod.REBUILD:
        assert config is not None
        _fix_rebuild(config, pdf)
        return
    try:
        fonts = pdffonts.classify(pdf)
    except Exception as e:
        console.print(
            f"[red]Error:[/red] cannot read {escape(str(pdf))}: {escape(str(e))}"
        )
        raise typer.Exit(code=1) from e
    if not fonts:
        outro("[green]✔ No Type 3 fonts[/green]")
        return
    console.print(
        f"[orange1]⚠ {len(fonts)} Type 3 font(s) in {escape(pdf.name)}:[/orange1]"
    )
    _print_type3_report(fonts)
    if method is FixMethod.ADVISE:
        for line in pdffonts.suggestion_text():
            console.print(f"[dim]{escape(line)}[/dim]")
        return
    _fix_outline(pdf, output, in_place)


@app.command(
    name="build-tree",
    help="Show which files the last build actually read, as a directory tree.",
    rich_help_panel="Build",
)
def build_tree(
    project_dir: Path | None = None, fls_filename: str | None = None
) -> None:
    if project_dir is None:
        project_dir = Path.cwd()
    config = load_project_config(project_dir / "texproject.toml")
    fls_filename = fls_filename or f"{config.jobname}.fls"
    fls_path = project_dir / config.build.aux_dir / fls_filename
    if not fls_path.exists():
        console.print(
            f"Error: {fls_filename} not found. Is your project built? Did you specify the correct filename (default: main.fls)?"
        )
        return

    # Parse the .fls file for used inputs
    used_files: set[Path] = set()
    used_folders: set[Path] = set()
    with fls_path.open("r", encoding="utf-8") as f:
        for line in f:
            if line.startswith("INPUT "):
                raw_path = line.removeprefix("INPUT ").strip()
                # Resolve creates an absolute path, normalizing relative dots
                used_files.add(Path(raw_path).resolve())
                used_folders.add(Path(raw_path).parent.resolve())

    def walk_directory(current_dir: Path, tree: Tree) -> None:
        # Sort dirs first then by filename
        paths = sorted(
            Path(current_dir).iterdir(),
            key=lambda path: (path.is_file(), path.name.lower()),
        )
        for path in paths:
            # Remove hidden files
            if path.name.startswith("."):
                continue
            if path.is_dir() and path.resolve() in used_folders:
                style = "dim" if path.name.startswith("__") else ""
                branch = tree.add(
                    f"[bold magenta]:open_file_folder: [link file://{path}]{escape(path.name)}",
                    style=style,
                    guide_style=style,
                )
                walk_directory(path, branch)
            elif path.is_file() and path.resolve() in used_files:
                text_filename = Text(path.name, "green")
                text_filename.highlight_regex(r"\..*$", "bold red")
                text_filename.stylize(f"link file://{path}")
                file_size = path.stat().st_size
                text_filename.append(f" ({decimal(file_size)})", "blue")
                icon = "🐍 " if path.suffix == ".py" else "📄 "
                tree.add(Text(icon) + text_filename)

    tree = Tree(
        f":open_file_folder: [link file://{project_dir.resolve()}]{project_dir}",
        guide_style="bold bright_blue",
    )
    walk_directory(project_dir, tree)
    console.print(tree)


@app.command(
    help="Show where texi keeps this project's local TeX tree and which tools it found.",
    rich_help_panel="Project",
)
def info():
    """Displays information about the current texi environment."""
    cwd = Path.cwd()
    env_dir = cwd / ".texenv"
    tlpdb_path = env_dir / "tlpkg" / "texlive.tlpdb"

    if not tlpdb_path.exists():
        console.print(
            "[yellow]Warning: .texenv not found. Run `texi sync` to initialize the environment.[/yellow]"
        )
        return

    _note(
        title="Current texi environment",
        content=[
            escape(f"{k}: {v}")
            for k, v in {
                "Environment directory": env_dir.resolve(),
                "TLPDB path": tlpdb_path.resolve(),
                "Latexmk path": latexmk_path,
                "Tex-fmt path": texfmt_path,
            }.items()
        ],
    )


@app.command(
    help="Check your TeX install and warn about stale user-level formats.",
    rich_help_panel="TeX Live maintenance",
)
def doctor(engine: str = "pdflatex") -> None:
    """Check the TeX install and flag stale user-level formats."""
    try:
        report = tools.diagnose(engine)
    except (sh.ErrorReturnCode, sh.CommandNotFound) as e:
        console.print(f"[red]{escape(str(e))}[/red]")
        raise typer.Exit(1) from e
    _note(
        title=f"{engine} environment",
        content=[
            escape(f"{k}: {', '.join(v) if isinstance(v, list) else v}")
            for k, v in report.items()
        ],
    )
    if len(report["formats"]) > 1:
        console.print(
            "[yellow]Multiple formats found; a stale user format may shadow the "
            "system one. Run `texi formats --user`.[/yellow]"
        )


@app.command(
    help="Rebuild LaTeX format files (fmtutil). Offers to fix TeX Live permissions if sudo would be needed.",
    rich_help_panel="TeX Live maintenance",
)
def formats(
    user: bool = typer.Option(
        False, "--user", help="Rebuild user formats (fmtutil-user)."
    ),
    missing: bool = typer.Option(
        False, "--missing", help="Only build formats that don't exist yet."
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Don't ask; use sudo if the TeX Live tree isn't writable.",
    ),
) -> None:
    """Rebuild LaTeX formats (system via sudo by default)."""
    flag = "--missing" if missing else "--all"
    sudo = False if user else _needs_sudo(f"fmtutil-sys {flag}", yes=yes)
    try:
        console.print(
            escape(
                tools.fmtutil_user(flag) if user else tools.fmtutil_sys(flag, sudo=sudo)
            )
        )
    except sh.ErrorReturnCode as e:
        console.print(f"[red]{escape(str(e))}[/red]")
        raise typer.Exit(1) from e


@app.command(
    help="Rebuild font maps (updmap). Run this after installing fonts.",
    rich_help_panel="TeX Live maintenance",
)
def updmap(
    user: bool = typer.Option(
        False, "--user", help="Regenerate user font maps (updmap-user)."
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Don't ask; use sudo if the TeX Live tree isn't writable.",
    ),
) -> None:
    """Regenerate font maps (system via sudo by default); run after installing fonts."""
    sudo = False if user else _needs_sudo("updmap-sys", yes=yes)
    try:
        console.print(
            escape(tools.updmap_user() if user else tools.updmap_sys(sudo=sudo))
        )
    except sh.ErrorReturnCode as e:
        console.print(f"[red]{escape(str(e))}[/red]")
        raise typer.Exit(1) from e


@app.command(
    help="Rebuild TeX's file-name databases (ls-R) of .texenv/, or of the given directories.",
    rich_help_panel="TeX Live maintenance",
)
def mktexlsr(
    dirs: Annotated[
        list[Path] | None,
        typer.Argument(help="Directories to index. Default: the trees of .texenv/."),
    ] = None,
) -> None:
    """Write ls-R files with texi's own implementation (no mktexlsr program)."""
    env_dir = Path.cwd() / ".texenv"
    targets = dirs or archive.mktexlsr_targets(env_dir, standalone=_standalone(env_dir))
    if not any(t.is_dir() for t in targets):
        console.print("[red]Nothing to index. Run `texi sync` first.[/red]")
        raise typer.Exit(code=1)
    try:
        for path in lsr.write_all(targets):
            console.print(f"[green]✔[/green] {escape(str(path))}")
    except lsr.LsrError as e:
        console.print(f"[red]{escape(str(e))}[/red]")
        raise typer.Exit(code=1) from e


@app.command(
    help="Rebuild the LuaTeX font database, e.g. when lualatex can't find a font.",
    rich_help_panel="TeX Live maintenance",
)
def luaotfload(
    force: bool = typer.Option(
        False, "--force", help="Rebuild the whole database (--update --force)."
    ),
) -> None:
    """Rebuild the LuaTeX font name database (luaotfload-tool --update)."""
    try:
        console.print(
            escape(
                tools.luaotfload_update("--update", "--force")
                if force
                else tools.luaotfload_update()
            )
        )
    except (sh.ErrorReturnCode, sh.CommandNotFound) as e:
        console.print(f"[red]{escape(str(e))}[/red]")
        raise typer.Exit(1) from e


@app.command(
    help="List packages in .texenv/ that have a newer revision in the repository. Installs nothing.",
    rich_help_panel="Project",
)
def outdated(debug: DebugOpt = False) -> None:
    """Fetch the latest package index and list what `texi sync` would update."""
    _intro("texi outdated")
    config = load_project_config(Path.cwd() / "texproject.toml")
    _apply_project(config)
    env_dir = Path.cwd() / ".texenv"
    if not env_dir.is_dir():
        console.print("[red]No .texenv/ yet. Run `texi sync`.[/red]")
        raise typer.Exit(code=1)
    db = _resolver_tlpdb(console, refresh=True)
    if db is None:
        console.print("[red]Package index unavailable (offline, no cached tlpdb).[/red]")
        raise typer.Exit(code=1)
    standalone = _standalone(env_dir)
    roots = [*(texenv.CLONE_ROOTS if standalone else []), *config.dependencies]
    pl = archive.plan(
        db,
        roots,
        env_dir,
        standalone=standalone,
        system_installed=None if standalone else tlpdb_mod.system_installed,
        update=True,
        arch=_target_arch(env_dir),
        infrastructure=standalone,
    )
    rows = [
        (n, archive.installed_revision(env_dir, n), db.get(n).revision)
        for n in sorted(pl.updates)
    ]
    for name, have, new in rows:
        console.print(f"{escape(name)}  [dim]r{have}[/dim] → [green]r{new}[/green]")
    tex_fmt_row = None
    if standalone and (arch := _target_arch(env_dir)):
        tex_fmt_row = texfmt.outdated(env_dir, arch)
    if tex_fmt_row:
        console.print(
            f"tex-fmt  [dim]{tex_fmt_row[0]}[/dim] → [green]{tex_fmt_row[1]}[/green]"
        )
    if not rows and not tex_fmt_row:
        outro("[green]✔ Everything is up to date[/green]")
        return
    n = len(rows) + (1 if tex_fmt_row else 0)
    outro(
        f"[yellow]{n} {inflect_engine.plural('package', n)} can be updated; "
        "run `texi sync`[/yellow]"
    )


@app.command(
    name="list",
    help="List the packages installed in .texenv/ with their revision, one per line.",
    rich_help_panel="Project",
)
def list_packages(
    explicit: Annotated[
        bool,
        typer.Option("--explicit", help="Only the dependencies in texproject.toml."),
    ] = False,
    debug: DebugOpt = False,
) -> None:
    """Plain `name rREVISION` lines (no banner), so the output can be piped."""
    env_dir = Path.cwd() / ".texenv"
    if not (env_dir / "tlpkg" / "texlive.tlpdb").is_file():
        typer.echo("texi list: no .texenv/ here. Run `texi sync`.", err=True)
        raise typer.Exit(code=1)
    g = deptree.read(env_dir, _target_arch(env_dir))
    names = sorted(g.revisions)
    if explicit:
        wanted = set(load_project_config(Path.cwd() / "texproject.toml").dependencies)
        names = [n for n in names if n in wanted]
    width = max((len(n) for n in names), default=0)
    for n in names:
        typer.echo(f"{n:<{width}}  r{g.revisions[n]}")


@app.command(
    help="Show the packages installed in .texenv/ and what depends on what, as a tree.",
    rich_help_panel="Project",
)
def tree(
    depth: Annotated[
        int | None,
        typer.Option("--depth", "-d", min=0, help="Show at most this many levels below each root."),
    ] = None,
    explicit: Annotated[
        bool,
        typer.Option(
            "--explicit",
            help="Only the dependencies in texproject.toml and what they depend on.",
        ),
    ] = False,
    package: Annotated[
        str | None,
        typer.Option("--package", "-p", help="Show only this package and its dependencies."),
    ] = None,
    invert: Annotated[
        bool,
        typer.Option(
            "--invert",
            "-i",
            help="With --package: show the packages that depend on it instead.",
        ),
    ] = False,
    project_only: Annotated[
        bool,
        typer.Option(
            "--project-only/--no-project-only",
            help="With --package --invert: only packages listed in texproject.toml "
            "(links that run through other packages are kept).",
        ),
    ] = True,
    debug: DebugOpt = False,
) -> None:
    """List installed packages as a dependency tree."""
    _intro("texi tree")
    if invert and package is None:
        console.print("[red]--invert needs --package.[/red]")
        raise typer.Exit(code=1)
    env_dir = Path.cwd() / ".texenv"
    if not (env_dir / "tlpkg" / "texlive.tlpdb").is_file():
        console.print("[red]No .texenv/ yet. Run `texi sync`.[/red]")
        raise typer.Exit(code=1)
    config = load_project_config(Path.cwd() / "texproject.toml")
    g = deptree.read(env_dir, _target_arch(env_dir))
    installed_count = len(g.revisions)
    only: set[str] | None = None
    if package is not None:
        if package not in g.revisions:
            console.print(f"[red]{escape(package)} is not installed in .texenv/.[/red]")
            raise typer.Exit(code=1)
        roots = [package]
    elif explicit:
        roots = sorted(d for d in config.dependencies if d in g.revisions)
        for d in sorted(set(config.dependencies) - set(g.revisions)):
            console.print(f"[yellow]{escape(d)} is in texproject.toml but not installed.[/yellow]")
    else:
        roots = g.roots()
    if explicit and package is None:
        only = deptree.closure(g, roots)
    if invert and project_only:
        listed = {d for d in config.dependencies if d in g.revisions}
        g = deptree.contract(g, listed | {package})
    nodes = deptree.build(g, roots, depth=depth, invert=invert, only=only)

    def label(n: deptree.Node) -> str:
        text = f"{escape(n.name)} [dim]r{escape(n.revision)}[/dim]"
        if n.seen:
            text += " [dim](*)[/dim]"
        elif n.cut:
            text += " [dim]…[/dim]"
        return text

    def add(parent: Tree, n: deptree.Node) -> None:
        branch = parent.add(label(n))
        for c in n.children:
            add(branch, c)

    heading = {
        (True, True): f"packages that depend on {package}"
        + (" (in texproject.toml)" if project_only else ""),
        (True, False): f"{package} and its dependencies",
    }.get((package is not None, invert), "explicit dependencies" if explicit else "installed packages")
    root_tree = Tree(f"[bold]{escape(heading)}[/bold]", guide_style="bright_blue")
    for n in nodes:
        add(root_tree, n)
    console.print(root_tree)
    if any(_has_seen(n) for n in nodes):
        console.print("[dim](*) already shown above[/dim]")
    outro(f"{installed_count} packages installed")


def _has_seen(n: deptree.Node) -> bool:
    return n.seen or any(_has_seen(c) for c in n.children)


@app.command(
    help="List packages in texproject.toml that the last build and your sources do not use.",
    rich_help_panel="Project",
)
def unused(debug: DebugOpt = False) -> None:
    """Packages you can remove from texproject.toml without changing the build."""
    _intro("texi unused")
    cwd = Path.cwd()
    env_dir = cwd / ".texenv"
    config = load_project_config(cwd / "texproject.toml")
    if not (env_dir / "tlpkg" / "texlive.tlpdb").is_file():
        console.print("[red]No .texenv/ yet. Run `texi sync`.[/red]")
        raise typer.Exit(code=1)
    aux = cwd / config.build.aux_dir
    names = (
        [config.set_jobname]
        if config.set_jobname
        else [Path(f).stem for f in config.main_files]
    )
    roots = [str(env_dir), str(env_dir.resolve())]
    paths: set[str] = set()
    built = False
    for name in dict.fromkeys(names):
        files = latexmk_files.ProjectFiles.from_build(aux, name)
        if files.fls is not None:
            built = True
            paths.update(files.fls.resolved_inputs())
        if (log := aux / f"{name}.log").is_file():
            paths |= unused_mod.log_paths(log.read_text(encoding="utf-8", errors="replace"), roots)
    if not built:
        console.print(
            "[red]No build record found. Run `texi build` first: without it every package "
            "would look unused.[/red]"
        )
        raise typer.Exit(code=1)
    owners = unused_mod.file_owners(env_dir)
    used = unused_mod.owners_of(paths, env_dir, owners)
    if not used:
        console.print(
            "[red]The build record does not mention this .texenv/ (copied from another "
            "directory, or built before .texenv/ moved?). Run `texi build --full` first.[/red]"
        )
        raise typer.Exit(code=1)
    if any(aux.glob("*.bcf")):  # biber runs from latexmk and leaves no input behind
        used |= {"biber", "biblatex"}
    # what the sources name, even if the build did not need a file of it
    found = extract_packages_from_tex(cwd, _source_files(cwd))
    if found:
        used |= set(resolve_dependencies(found, console))
    g = deptree.read(env_dir, _target_arch(env_dir))
    cats = _categories(env_dir)
    rep = unused_mod.analyze(g, config.dependencies, used, cats)
    _print_unused(rep)


def _categories(env_dir: Path) -> dict[str, str]:
    """`name -> category` of the packages in the tree's tlpdb."""
    out: dict[str, str] = {}
    name = None
    for line in (env_dir / "tlpkg" / "texlive.tlpdb").read_text(encoding="utf-8").splitlines():
        if line.startswith("name "):
            name = line[5:].strip()
        elif line.startswith("category ") and name:
            out[name] = line[9:].strip()
    return out


def _print_unused(rep: unused_mod.Report) -> None:
    if rep.unused:
        _note(
            title=f"{len(rep.unused)} unused: likely safe to remove from texproject.toml",
            content=[escape(", ".join(rep.unused))],
        )
    if rep.needed:
        _note(
            title=f"{len(rep.needed)} only needed by other packages (redundant entries)",
            content=[
                escape(f"{n}  ← {', '.join(by[:3])}{' …' if len(by) > 3 else ''}")
                for n, by in sorted(rep.needed.items())
            ],
        )
    if rep.not_installed:
        console.print(
            f"[yellow]Not installed: {escape(', '.join(rep.not_installed))}. Run `texi sync`.[/yellow]"
        )
    kept = len(rep.used) + len(rep.via_collection)
    if not rep.unused and not rep.needed:
        outro(f"[green]✔ All {kept} dependencies are used[/green]")
    else:
        outro(
            f"{kept} used, {len(rep.unused)} unused, {len(rep.needed)} redundant. "
            "Packages the build needs but you did not list are installed anyway."
        )


class ShellName(str, Enum):
    BASH = "bash"
    ZSH = "zsh"
    SH = "sh"
    FISH = "fish"


def _shell_commands(
    shell: ShellName, env_dir: Path, variables: dict[str, str], *, off: bool
) -> str:
    """Shell code that puts `env_dir`'s tools first on PATH (or takes them out again)."""
    bin_dir = texenv.env_bin(env_dir)
    root = str(env_dir.resolve())
    bin_s = str(bin_dir.resolve()) if bin_dir else ""
    # this process already has bin first (the callback activated it); the user's own PATH is the rest
    others = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p and p != bin_s]
    names = sorted(variables)
    lines: list[str] = []
    if shell is ShellName.FISH:
        q = lambda v: "'" + v.replace("\\", "\\\\").replace("'", "\\'") + "'"  # noqa: E731
        if off:
            lines += [f"set -gx PATH {' '.join(q(p) for p in others)}"]
            lines += [f"set -e {n}" for n in [*names, "TEXI_ENV"]]
        else:
            lines += [f"set -gx PATH {q(bin_s)} {' '.join(q(p) for p in others)}".rstrip()]
            lines += [f"set -gx {n} {q(variables[n])}" for n in names]
            lines += [f"set -e {n}" for n in texenv.SCRUBBED] if bin_dir else []
            lines += [f"set -gx TEXI_ENV {q(root)}"]
    else:
        q = shlex.quote
        if off:
            lines += [f"export PATH={q(os.pathsep.join(others))}"]
            lines += [f"unset {n}" for n in [*names, "TEXI_ENV"]]
        else:
            lines += [f"export PATH={q(os.pathsep.join([bin_s, *others]))}"]
            lines += [f"export {n}={q(variables[n])}" for n in names]
            lines += [f"unset {' '.join(texenv.SCRUBBED)}"] if bin_dir else []
            lines += [f"export TEXI_ENV={q(root)}"]
    return "\n".join(lines) + "\n"


@app.command(
    help='Print shell commands that put .texenv/ first, for: eval "$(texi activate)".',
    rich_help_panel="Project",
)
def activate(
    shell: Annotated[
        ShellName | None,
        typer.Option("--shell", help="Shell syntax. Default: from $SHELL (fish, else POSIX)."),
    ] = None,
    off: Annotated[
        bool,
        typer.Option("--off", help='Undo it: eval "$(texi activate --off)".'),
    ] = False,
) -> None:
    """Print export lines only: stdout is meant to be eval'd, so nothing else goes there."""
    env_dir = Path.cwd() / ".texenv"
    if texenv.env_bin(env_dir) is None and not texenv.is_usertree(env_dir):
        typer.echo("texi activate: no .texenv/ here. Run `texi sync`.", err=True)
        raise typer.Exit(code=1)
    if shell is None:
        shell = ShellName.FISH if Path(os.environ.get("SHELL", "")).name == "fish" else ShellName.BASH
    variables = _texenv_vars(env_dir)
    variables.pop("PATH", None)
    typer.echo(_shell_commands(shell, env_dir, variables, off=off), nl=False)


_ORDER = [
    "init",
    "add",
    "install",
    "sync",
    "outdated",
    "scan",
    "info",
    "build",
    "clean",
    "preview",
    "fix_pdf",
    "fmt",
    "export",
    "unused",
    "list_packages",
    "tree",
    "build_tree",
    "activate",
    "doctor",
    "formats",
    "updmap",
    "mktexlsr",
    "luaotfload",
]
app.registered_commands.sort(
    key=lambda c: (
        _ORDER.index(c.callback.__name__)
        if c.callback.__name__ in _ORDER
        else len(_ORDER)
    )
)

from texi.tlpdb_cli import cache_app  # noqa: E402


@cache_app.command("prune")
def cache_prune(
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", "-n", help="Only list what would be removed."),
    ] = False,
    debug: DebugOpt = False,
) -> None:
    """Remove cached packages (and incomplete downloads) this project's .texenv/ does not use, and old revisions of those it does.

    The cache is shared by all projects: packages another project needs are
    downloaded again on its next `texi sync`.
    """
    _intro("texi cache prune")
    env_dir = Path.cwd() / ".texenv"
    managed = archive.managed(env_dir)
    if not managed:
        console.print("[red]No packages from the cache in .texenv/. Run `texi sync` first.[/red]")
        raise typer.Exit(code=1)
    keep = {v["id"] for v in managed.values()}
    removed = archive.prune(keep, dry_run=dry_run)
    for entry_id, size in removed:
        console.print(f"  {entry_id}  {size / 1e6:.1f} MB")
    total = sum(size for _, size in removed) / 1e6
    verb = "Would remove" if dry_run else "Removed"
    outro(f"{verb} {len(removed)} {inflect_engine.plural('entry', len(removed))}, {total:.1f} MB")



app.add_typer(cache_app, name="cache", rich_help_panel="Cache")


if __name__ == "__main__":
    app()
