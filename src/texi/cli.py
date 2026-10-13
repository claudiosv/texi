import asyncio
import json
import getpass
import os
import re
import textwrap
import shutil
import subprocess
import sys
import tomllib
from enum import Enum
from collections.abc import Callable, Generator
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

from texi import tools
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


def _select(message: str, choices: list[str]) -> str | None:
    answer = asyncio.run(
        select(message=message, options=[Option(c, c) for c in choices])
    )
    return None if is_cancel(answer) else answer


def _texenv_vars(env_dir: Path) -> dict[str, str]:
    """TeX env vars that point TeX (and updmap's generated font maps) at .texenv/."""
    root = env_dir.resolve()
    return {
        "TEXMFHOME": str(root),
        "TEXMFVAR": str(root / "texmf-var"),
        "TEXMFCONFIG": str(root / "texmf-config"),
    }


def _updmap_local(env_dir: Path) -> None:
    """Regenerate font maps for packages in .texenv/ (tlmgr usermode doesn't)."""
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
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if DEBUG:
        console.print(
            f"[dim]exit {result.returncode}\n--- stdout ---\n{escape(result.stdout)}"
            f"--- stderr ---\n{escape(result.stderr)}[/dim]"
        )
    return result


# Pass the generated .latexmkrc to latexmk via `-norc -r` so it is read exactly once.
EXPLICIT_RC = True

tlmgr_path = shutil.which("tlmgr")
TLMGR_REPOSITORY: str | None = None  # set from [project] repository


def _tl() -> list[str]:
    """tlmgr command head, honoring the project's `repository`."""
    return [
        tlmgr_path,
        *(["--repository", TLMGR_REPOSITORY] if TLMGR_REPOSITORY else []),
    ]


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


def generate_latexmkrc(config: TexProjectConfig, rc_path: Path) -> None:
    """Generates a .latexmkrc file from the validated Pydantic model."""
    rc_lines: list[str] = ["# Auto-generated by texi.", "# Do not edit manually.", ""]

    # mode='json' ensures all enums are reduced to their primitive values
    settings = config.build.model_dump(mode="json")
    settings["jobname"] = config.set_jobname

    in_subdir = config.src_dir not in ("", ".")
    rc_lines += [
        "use Cwd qw(abs_path);",
        "use File::Basename qw(dirname);",
        "my $root = dirname(abs_path(__FILE__));  # project root, wherever latexmk runs",
        'if (-d "$root/.texenv") {',
        '  $ENV{TEXMFHOME} = "$root/.texenv";',
        '  $ENV{TEXMFVAR} = "$root/.texenv/texmf-var";  # font maps from updmap-user',
        '  $ENV{TEXMFCONFIG} = "$root/.texenv/texmf-config";',
        "}",
        '$ENV{max_print_line} = "100000";  # avoid line wrapping in logs',
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

    rc_path.write_text("\n".join(rc_lines), encoding="utf-8")


def _tlmgr_info(*cmd_input: list[str]) -> subprocess.CompletedProcess:
    """Runs tlmgr info with the given input and returns the completed process."""
    cmd: list[str] = [
        *_tl(),
        "info",
        *cmd_input,
    ]
    return _run(cmd)


def _parse_tlmgr_info_output(
    result: subprocess.CompletedProcess, fields: list[str]
) -> Generator[dict[str, Any | str] | dict[str, Any], Any, None]:
    expected_seperators = len(fields) - 1
    output_lines = result.stdout.splitlines() + result.stderr.splitlines()
    for line in output_lines:
        # trailer tlmgr prints on stderr whenever any requested package is missing
        if line.startswith((
            "tlmgr: action info returned an error",
            "tlmgr: An error has occurred",
        )):
            continue
        if "not found neither locally nor remote" in line:
            parts = line.split()
            if len(parts) >= 3:
                pkg_name = parts[2]
                yield {
                    "name": pkg_name,
                    "status": "missing",
                }
        elif "not locally!" in line:  # tlmgr: package acmart not locally!
            parts = line.split()
            if len(parts) >= 3:
                pkg_name = parts[2]
                yield {"name": pkg_name, "status": "not_locally"}
        elif (
            line.count(",") == expected_seperators
        ):  # Expecting lines like "pkgname,installed,relocatable,size"
            parts = line.strip().split(",")
            if len(parts) != len(fields):
                yield {
                    "status": "unexpected",
                    "line": line,
                    "error": f"Expected {len(fields)} fields but got {len(parts)}",
                }
                continue
            parsed_fields = dict(zip(fields, parts, strict=True))
            if "installed" in parsed_fields:
                parsed_fields["installed"] = parsed_fields["installed"] == "1"
            if "relocatable" in parsed_fields:
                parsed_fields["relocatable"] = parsed_fields["relocatable"] == "1"
            yield parsed_fields | {"status": "found"}

        else:
            yield {"status": "unexpected", "line": line}


def _get_tlmgr_info_output(
    *cmd_input: list[str], data_fields: list[str], env_dir: Path
) -> Generator[dict[str, Any | str] | dict[str, Any], Any, None]:
    tlmgr_result = _tlmgr_info(
        "--usertree",
        str(env_dir),
        "--data",
        ",".join(data_fields),
        *cmd_input,
    )
    return _parse_tlmgr_info_output(tlmgr_result, data_fields)


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


def filter_packages(
    packages: list[str],
    env_dir: Path,
    prog: ShimmerProgress,
) -> tuple[list[str], list[str], list[str]]:
    """
    Queries tlmgr via JSON to categorize packages into valid (to install),
    skipped (system binaries), and already installed.
    """
    if not packages:
        return [], [], []

    packages = list(set(packages))  # Deduplicate
    done = 0

    def advance() -> None:
        nonlocal done
        done += 1
        prog.on_progress("Fetching metadata", done, len(packages))

    prog.on_progress("Fetching metadata", 0, len(packages))
    installable_local_packages: list[str] = []
    installable_system_packages: list[str] = []
    already_installed: list[str] = []
    remote_packages: list[str] = []
    missing_packages: list[str] = []

    data_fields = ["name", "installed", "relocatable"]

    # TODO: should the "--usermode" flag be included?
    installed_result = _get_tlmgr_info_output(
        "--only-installed", *packages, data_fields=data_fields, env_dir=env_dir
    )
    for result in installed_result:
        if result["status"] == "found":
            if result["installed"]:
                already_installed.append(result["name"])
            elif not result["installed"] and result["relocatable"]:
                installable_local_packages.append(result["name"])
            elif not result["installed"] and not result["relocatable"]:
                installable_system_packages.append(result["name"])
            advance()
        elif result["status"] == "missing":
            console.print(
                f"[yellow]Warning: Package '{result['name']}' is not available in the current TeX Live repository.[/yellow]"
            )
            missing_packages.append(result["name"])
            advance()
        elif result["status"] == "not_locally":
            remote_packages.append(result["name"])
            advance()
        elif result["status"] == "unexpected":
            console.print(
                f"[red]Unexpected output from tlmgr info: {result.get('line', '')}[/red]"
            )

    remaining_packages = list(set(missing_packages + remote_packages))

    # With no package args, `tlmgr info` lists *every* package, so never call it empty.
    remote_result = (
        _get_tlmgr_info_output(
            "--only-remote",
            *remaining_packages,
            data_fields=data_fields,
            env_dir=env_dir,
        )
        if remaining_packages
        else iter(())
    )
    for result in remote_result:
        if result["status"] == "found":
            if result["installed"]:
                already_installed.append(result["name"])
            elif not result["installed"] and result["relocatable"]:
                installable_local_packages.append(result["name"])
            elif not result["installed"] and not result["relocatable"]:
                installable_system_packages.append(result["name"])
            advance()
        elif result["status"] == "missing":
            console.print(
                f"[yellow]Warning: Package '{result['name']}' is not available in the current TeX Live repository.[/yellow]"
            )
            missing_packages.append(result["name"])
            advance()
        elif result["status"] == "not_locally":
            remote_packages.append(result["name"])
            advance()
        elif result["status"] == "unexpected":
            console.print(
                f"[red]Unexpected output from tlmgr info: {result.get('line', '')}[/red]"
            )

    # `tlmgr info` only sees the system tree; packages already in .texenv count as installed.
    in_usertree = _usertree_packages(env_dir)
    already_installed += [p for p in installable_local_packages if p in in_usertree]
    installable_local_packages = [
        p for p in set(installable_local_packages) if p not in in_usertree
    ]
    installable_system_packages = list(set(installable_system_packages))
    already_installed = list(set(already_installed))

    return installable_local_packages, installable_system_packages, already_installed


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


def find_bundle_for_sty(
    sty_name: str,
    console: Console,
    cache: dict[str, list[str]] | None = None,
    exts: tuple[str, ...] = (".sty", ".cls"),
) -> list[str]:
    """
    Searches CTAN via tlmgr to find which package bundles
    provide the exact {sty_name}.sty file.
    """
    if cache is not None and sty_name in cache:
        return cache[sty_name]

    cmd: list[str] = [
        "tlmgr",
        "search",
        sty_name,
        "--json",
        "--word",
        "--global",
        "--file",
    ]

    try:
        # We drop check=True so we can handle tlmgr's exit codes manually if needed,
        # but here we just want to suppress outright crashes.
        result = _run(cmd)
        # if result.returncode != 0 and not result.stdout.strip():
        #     return []
        # console.print(f"[dim]tlmgr search output: {result.stdout}[/dim]")
        output = result.stdout.strip()

        if not output.startswith("{"):
            # Find the index of the first opening curly brace
            start_index = output.find("{")

            if start_index != -1:
                # Slice the string from the first '{' to the end
                output = output[start_index:]
        data = json.loads(output)
    except subprocess.CalledProcessError, json.JSONDecodeError:
        console.print_exception()
        console.print(output)
        return []

    matched_bundles: list[str] = []

    # We look for paths ending in /name.sty to avoid false positives
    # classes (.cls) are packages too, e.g. \\documentclass{acmart}
    suffixes = tuple(f"{sty_name}{e}" for e in exts)

    files_dict: dict[str, list[str]] = data.get("files", {})

    for bundle_name, file_paths in files_dict.items():
        for path in file_paths:
            if path.rpartition("/")[2] in suffixes:
                matched_bundles.append(bundle_name)
                break  # Found it in this bundle, skip remaining files in this bundle

    if cache is not None:
        cache[sty_name] = matched_bundles

    return matched_bundles


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
    r"""Resolves raw \usepackage names to tlmgr bundle names, using a persistent cache."""
    if tlmgr_path is None:
        console.print(
            "[yellow]Warning: tlmgr not found; using raw package names as dependencies. "
            "They may need manual correction for `texi sync` to work.[/yellow]"
        )
        return sorted(set(packages))

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
        True, "--resolve/--no-resolve", help="Resolve names to tlmgr bundles."
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
                    "Resolving to tlmgr dependencies", done, total
                ),
            )
        _note(title="tlmgr dependencies", content=[escape(", ".join(deps))])
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
                    "Resolving to tlmgr dependencies", done, total
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
            project=ProjectConfig(name=cwd.name, dependencies=dependencies)
        )
    except ValidationError as e:
        console.print("[red]Configuration Validation Error:[/red]")
        console.print(e)
        raise typer.Exit(code=1) from e

    lines = [
        "[project]",
        f'name = "{config.name}"',
        f'src_dir = "{config.src_dir}"',
        f'main = "{config.project.main}"',
        "",
        "dependencies = [",
    ]
    lines += [f'  "{dep}",' for dep in config.dependencies]
    lines.append("]")
    lines.append("")
    config_path.write_text("\n".join(lines), encoding="utf-8")

    outro(
        f"[green]✔ Created {config_path.name} with {len(dependencies)} dependencies.[/green]"
    )


LOCK_NAME = "texi.lock"


def _apply_project_tlmgr(config: TexProjectConfig) -> None:
    """Apply [project] repository / texlive: tlmgr source and a year sanity check."""
    global TLMGR_REPOSITORY
    TLMGR_REPOSITORY = config.project.repository
    if config.project.texlive and tlmgr_path:
        m = re.search(r"version (\d{4})", _run([tlmgr_path, "--version"]).stdout)
        if m and m.group(1) != config.project.texlive:
            console.print(
                f"[yellow]Warning: texproject.toml expects TeX Live "
                f"{config.project.texlive}, but {m.group(1)} is installed.[/yellow]"
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
    if rest and tlmgr_path:
        for line in _tlmgr_info("--data", "name,localrev", *rest).stdout.splitlines():
            n, _, rev = line.partition(",")
            if n in rest and rev.strip().isdigit():
                revs[n] = int(rev)
    return revs


def _read_lock(path: Path) -> dict[str, int]:
    try:
        return dict(tomllib.loads(path.read_text(encoding="utf-8")).get("packages", {}))
    except OSError, tomllib.TOMLDecodeError:
        return {}


def _write_lock(path: Path, revs: dict[str, int]) -> None:
    year = re.search(r"version (\d{4})", _run([tlmgr_path, "--version"]).stdout)
    lines = [
        "# Generated by `texi sync`. Commit this file to record resolved package revisions.",
        f'texlive = "{year.group(1)}"' if year else "",
    ]
    if TLMGR_REPOSITORY:
        lines.append(f"repository = {json.dumps(TLMGR_REPOSITORY)}")
    lines += ["", "[packages]"]
    for name, rev in sorted(revs.items()):
        key = name if re.fullmatch(r"[A-Za-z0-9_-]+", name) else json.dumps(name)
        lines.append(f"{key} = {rev}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _update_lock(cwd: Path, installed: set[str], keep: list[str]) -> None:
    """Record installed revisions in texi.lock and report what changed since last time."""
    if not tlmgr_path:
        return
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


def _install(prog: ShimmerProgress, phase: str, cmd: list[str]) -> None:
    """Run `tlmgr install --machine-readable`, feeding byte counts to the progress bar."""
    _debug_cmd(cmd)
    process = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    done = total = 0
    post_install = False
    prog.on_progress(phase)
    for line in process.stdout:
        if DEBUG:
            console.print(f"[dim]{escape(line.rstrip())}[/dim]")
        stripped = line.strip()
        if stripped.startswith("total-bytes"):
            parts = stripped.split("\t")
            if len(parts) >= 2:
                total = int(parts[1])
                prog.on_progress(phase, done, total)
        elif "is not relocatable, cannot install it in user mode!" in stripped:
            parts = stripped.split()
            if len(parts) >= 3:
                console.print(
                    f"[yellow]Warning: Package '{parts[2]}' is not relocatable and "
                    "cannot be installed in user mode. Skipping.[/yellow]"
                )
        elif stripped == "end-of-updates":
            post_install = True
            prog.on_progress("Running post-installation scripts")
        elif not post_install and stripped.count("\t") == 9:
            done += int(stripped.split("\t")[4])
            prog.on_progress(phase, done, total)
    process.wait()


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


def _ensure_tl_writable() -> None:
    """tlmgr needs a writable TeX Live tree; offer to chown it to the current user."""
    if (target := _tl_target()) is None:
        return
    if not _confirm(
        f"{target} is not writable, so tlmgr can't install packages. "
        f"Run `sudo chown -R {getpass.getuser()} {target}`?",
        default=True,
    ):
        raise typer.Exit(code=1)
    _chown_tl(target)


def _needs_sudo(what: str, *, yes: bool = False) -> bool:
    """Decide how to run a system TeX Live tool: True = prefix with sudo.

    Writable tree: no sudo. Otherwise offer a one-time chown (so this and tlmgr
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


def _update_tlmgr() -> None:
    """Run `tlmgr update --self` and log whether it updated tlmgr."""
    console.print("[dim]Updating tlmgr...[/dim]")
    result = _run([*_tl(), "update", "--self"])
    out = result.stdout + result.stderr
    if result.returncode != 0:
        console.print(
            "[yellow]tlmgr update failed (run with --debug for details).[/yellow]"
        )
    elif "no self-updates" in out:
        console.print("[green]✔ tlmgr already up to date[/green]")
    else:
        console.print("[green]✔ tlmgr updated[/green]")


def _ensure_latexmk() -> None:
    """Install latexmk into the system TeX Live if it's not on PATH."""
    if shutil.which("latexmk"):
        return
    if not tlmgr_path:
        console.print("[red]latexmk and tlmgr not found; install TeX Live.[/red]")
        raise typer.Exit(code=1)
    _ensure_tl_writable()
    install = [*_tl(), "install", "latexmk", "--machine-readable"]
    for attempt in range(2):
        with ShimmerProgress(console) as prog:
            _install(prog, "Installing latexmk", install)
        if shutil.which("latexmk"):
            return
        if attempt == 0:  # tlmgr refuses to install anything while it is outdated
            console.print(
                "[yellow]Install failed; updating tlmgr and retrying.[/yellow]"
            )
            _update_tlmgr()
    console.print("[red]Could not install latexmk. Try `tlmgr install latexmk`.[/red]")
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
    """Offer to install make4ht and its luaxml dependency into the system TeX Live."""
    missing = _make4ht_missing()
    if not missing:
        return
    if not tlmgr_path:
        console.print("[red]make4ht needs TeX Live (tlmgr not found).[/red]")
        raise typer.Exit(code=1)
    console.print(
        f"[yellow]make4ht is not usable; missing: {', '.join(missing)}[/yellow]"
    )
    if not (
        sys.stdin.isatty()
        and _confirm(f"Install {', '.join(missing)} with tlmgr?", default=True)
    ):
        console.print(f"Install with: tlmgr install {' '.join(missing)}")
        raise typer.Exit(code=1)
    _ensure_tl_writable()
    install = [*_tl(), "install", *missing, "--machine-readable"]
    for attempt in range(2):
        with ShimmerProgress(console) as prog:
            _install(prog, "Installing make4ht", install)
        if not _make4ht_missing():
            return
        if attempt == 0:  # tlmgr refuses to install while it is outdated
            console.print(
                "[yellow]Install failed; updating tlmgr and retrying.[/yellow]"
            )
            _update_tlmgr()
    console.print(
        f"[red]Could not install. Try `tlmgr install {' '.join(missing)}`.[/red]"
    )
    raise typer.Exit(code=1)


@app.command(
    help="Install and update every package listed in texproject.toml into the project's own .texenv/.",
    rich_help_panel="Project",
)
def sync(
    no_update: bool = typer.Option(
        False,
        "--no-update",
        help="Install only; skip updating packages that are already installed.",
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
    _apply_project_tlmgr(config)
    _sync(config.dependencies, no_update=no_update)


def _sync(
    packages: list[str], *, no_update: bool, keep: list[str] | None = None
) -> None:
    """Install `packages` into .texenv/ (and update them unless no_update)."""
    env_dir = Path.cwd() / ".texenv"

    if tlmgr_path:
        _ensure_tl_writable()
        _update_tlmgr()
    _ensure_latexmk()
    if not packages:
        console.print(
            "[yellow]No dependencies found in texproject.toml. Nothing to sync.[/yellow]"
        )
        return

    env_dir.mkdir(exist_ok=True)
    tlpdb_path = env_dir / "tlpkg" / "texlive.tlpdb"
    plural = inflect_engine.plural

    with ShimmerProgress(console) as prog:
        if tlpdb_path.exists():
            console.print("[green]Local usertree already initialized.[/green]")
        else:
            prog.on_progress("Initializing local usertree")
            init_cmd = [*_tl(), "init-usertree", "--usertree", str(env_dir)]
            _debug_cmd(init_cmd)
            init_process = subprocess.Popen(
                init_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            for line in init_process.stdout:
                console.print(escape(line.strip()))
            if init_process.wait() != 0:
                console.print("[red]Initialization failed![/red]")
                raise typer.Exit(code=1)

        n = len(packages)
        console.print(
            f"[blue]Fetching metadata for {n} {plural('package', n)}:[/blue]\n"
            f"[dim]{', '.join(sorted(packages))}[/dim]"
        )
        valid_dependencies, system_dependencies, already_installed = filter_packages(
            packages, env_dir, prog
        )
        all_dependencies = set(
            valid_dependencies + system_dependencies + already_installed
        )
        if already_installed:
            console.print(
                f"[green]✔ Skipping {len(already_installed)} already installed "
                f"{plural('package', len(already_installed))}:[/green]\n"
                f"[dim]{', '.join(sorted(already_installed))}[/dim]"
            )

        if valid_dependencies:
            console.print(
                f"[blue]Installing {len(valid_dependencies)} local "
                f"{plural('package', len(valid_dependencies))}:[/blue]\n"
                f"[dim]{', '.join(sorted(valid_dependencies))}[/dim]"
            )
            _install(
                prog,
                "Installing local packages",
                [
                    *_tl(),
                    "install",
                    "--usertree",
                    str(env_dir),
                    "--usermode",
                    *valid_dependencies,
                    "--machine-readable",
                ],
            )

        if system_dependencies:
            _ensure_tl_writable()
            console.print(
                f"[blue]Installing {len(system_dependencies)} system "
                f"{plural('package', len(system_dependencies))}:[/blue]\n"
                f"[dim]{', '.join(sorted(system_dependencies))}[/dim]"
            )
            _install(
                prog,
                "Installing system packages",
                [*_tl(), "install", *system_dependencies, "--machine-readable"],
            )

    if not no_update:
        in_usertree = _usertree_packages(env_dir)
        local = sorted(all_dependencies & in_usertree)
        system = sorted(all_dependencies - in_usertree)
        with ShimmerProgress(console) as prog:
            if local:
                _install(
                    prog,
                    "Updating local packages",
                    [
                        *_tl(),
                        "update",
                        "--usertree",
                        str(env_dir),
                        "--usermode",
                        "--machine-readable",
                        *local,
                    ],
                )
            if system:
                _install(
                    prog,
                    "Updating system packages",
                    [*_tl(), "update", "--machine-readable", *system],
                )

    _updmap_local(env_dir)
    _update_lock(Path.cwd(), all_dependencies, keep if keep is not None else packages)
    _note(
        title=f"Synced {len(all_dependencies)} {plural('dependency', len(all_dependencies))} into .texenv/",
        content=[escape(", ".join(sorted(all_dependencies)))],
    )
    outro("[green]✔ Sync complete[/green]")


def _tl_package_exists(name: str) -> bool:
    """True if tlmgr knows `name` (locally or in the repository)."""
    result = _tlmgr_info("--data", "name", name)
    return any(line.strip() == name for line in result.stdout.splitlines())


def _lookup_packages(names: list[str]) -> dict[str, list[str] | None]:
    """Map each name to None if it's a TeX Live package, else the packages providing a file by that name."""
    found: dict[str, list[str] | None] = {}
    with ShimmerProgress(console) as prog:
        prog.on_progress("Checking tlmgr info", 0, len(names))
        for i, name in enumerate(names, 1):
            if _tl_package_exists(name):
                found[name] = None
            else:
                prog.on_progress(f"Searching for {name}", i - 1, len(names))
                found[name] = find_bundle_for_sty(
                    name, console, exts=(".sty", ".cls", ".tex")
                )
            prog.on_progress("Checking tlmgr info", i, len(names))
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
        ok = _confirm(
            f"'{name}' is provided by '{pkg}'. Add '{pkg}' instead?", default=True
        )
        return pkg if ok else None
    return _select(f"{name} is provided by several packages. Which one?", candidates)


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
    if not tlmgr_path:
        console.print("[red]tlmgr not found; install TeX Live.[/red]")
        raise typer.Exit(code=1)
    _apply_project_tlmgr(config)
    lookup = _lookup_packages(list(dict.fromkeys(packages)))
    resolved = [n if c is None else _choose_package(n, c) for n, c in lookup.items()]
    packages = list(dict.fromkeys(r for r in resolved if r))
    if not packages:
        outro("[red]Nothing to add.[/red]")
        raise typer.Exit(code=1)
    new = [p for p in packages if p not in config.dependencies]
    if new:
        _add_to_toml(config_path, new)
        console.print(f"[green]✔ Added to {config_path.name}:[/green] {', '.join(new)}")
    else:
        console.print("[dim]Already in dependencies.[/dim]")
    if sync_all:
        _sync([*config.dependencies, *new], no_update=False)
    else:
        _sync(
            list(dict.fromkeys(packages)),
            no_update=True,
            keep=[*config.dependencies, *new],
        )


def _run_latexmk(
    *,
    strict: bool,
    preview: bool,
    full: bool = False,
    clean: str | None = None,
    silent: bool = False,
) -> None:
    """Generate .latexmkrc (with CLI overrides) and run latexmk in the project."""
    cwd = Path.cwd()
    env_dir = cwd / ".texenv"

    config = load_project_config(cwd / "texproject.toml")
    execution = config.build
    execution.halt_on_error = strict
    if full:
        execution.go_mode = GoMode.FULL_REBUILD
    execution.silent = execution.silent or silent
    execution.force_mode = not strict
    execution.preview_mode = False
    execution.preview_continuous_mode = preview
    rc_path = cwd / ".latexmkrc"
    generate_latexmkrc(config, rc_path)
    if not execution.silent:
        console.print("[dim]Generated .latexmkrc from texproject.toml[/dim]")

    env = os.environ.copy()
    if env_dir.exists():
        env.update(_texenv_vars(env_dir))
        env["max_print_line"] = "100000"  # Avoid line wrapping in logs
        if not execution.silent:
            console.print("[dim]Using local .texenv for compilation[/dim]")
    else:
        console.print(
            "[yellow]Warning: .texenv not found. Run `texi sync` for isolated dependencies.[/yellow]"
        )

    _ensure_latexmk()
    if DEBUG:
        console.print(f"[dim]latexmk: {escape(str(shutil.which('latexmk')))}[/dim]")
        changed = {k: v for k, v in env.items() if os.environ.get(k) != v}
        if config.src_dir not in ("", "."):  # set by the generated .latexmkrc
            src = f"{cwd}/{config.src_dir.strip('/')}//:"
            for k in ("BIBINPUTS", "TEXINPUTS"):
                changed[f"{k} (in .latexmkrc)"] = src + os.environ.get(k, "")
        for k, v in changed.items():
            console.print(f"[dim]env {escape(k)}={escape(v)}[/dim]")
    cmd = ["latexmk", *(["-norc", "-r", str(rc_path)] if EXPLICIT_RC else [])]
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
    try:
        subprocess.run(cmd, env=env, check=True)
    except subprocess.CalledProcessError as e:
        what = "Clean" if clean else "Build"
        console.print(f"\n[red]✖ {what} failed. Check the logs above.[/red]")
        raise typer.Exit(code=1) from e
    except KeyboardInterrupt:  # normal way to leave -pvc
        if not preview:
            raise
        return
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


@app.command(
    help="Compile the project to PDF with latexmk, using the settings in texproject.toml.",
    rich_help_panel="Build",
)
def build(
    strict: StrictOpt = False,
    full: bool = typer.Option(
        False,
        "--full",
        help="Rebuild everything, even if latexmk thinks nothing changed (go_mode = full-rebuild).",
    ),
    silent: bool = typer.Option(
        False, "--silent", "-s", help="Reduce output to errors and the final status."
    ),
    rm_pdf: bool = typer.Option(
        False,
        "--rm-pdf",
        help="Delete the old PDF first, so a failed build leaves none behind.",
    ),
    debug: DebugOpt = False,
) -> None:
    """Generate .latexmkrc and compile the project."""
    _intro("texi build")
    if rm_pdf:
        config = load_project_config(Path.cwd() / "texproject.toml")
        final_dir = config.build.out2_dir or config.build.out_dir
        output_file = Path.cwd() / final_dir / f"{config.jobname}.pdf"
        if output_file.exists():
            console.print(f"[dim]Removing existing output file: {output_file}[/dim]")
            output_file.unlink()
    _run_latexmk(strict=strict, preview=False, full=full, silent=silent)


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
    debug: DebugOpt = False,
) -> None:
    """Format .tex files with tex-fmt, using settings from [tex-fmt] in texproject.toml."""
    _intro("texi fmt")
    cwd = Path.cwd()
    config = load_project_config(cwd / "texproject.toml")
    if not shutil.which("tex-fmt"):
        console.print(
            "[red]tex-fmt not found.[/red] Try `brew install tex-fmt` or `cargo install tex-fmt`."
        )
        raise typer.Exit(code=1)
    targets = files or sorted((cwd / config.src_dir).rglob("*.tex"))
    if not targets:
        console.print("[yellow]No .tex files found.[/yellow]")
        return
    # The config goes in over stdin, so no temp file. --config alone is enough: it
    # takes precedence over any tex-fmt.toml on disk, while --noconfig would
    # ignore it too.
    cmd = [
        "tex-fmt",
        "--config",
        "/dev/stdin",
        *(["--check"] if check else []),
        *map(str, targets),
    ]
    _debug_cmd(cmd)
    run = subprocess.run(cmd, input=texfmt_config(config.tex_fmt), text=True)
    if run.returncode != 0:
        console.print("[red]✖ tex-fmt reported problems.[/red]")
        raise typer.Exit(code=1)
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


_EXPORT_HINTS = {
    "pandoc": "brew install pandoc",
    "make4ht": "tlmgr install make4ht tex4ht luaxml",
    "latexmlc": "brew install latexml",
}


@app.command(
    help="Convert the document to Markdown or HTML (pandoc, TeX4ht or LaTeXML) in build/export/.",
    rich_help_panel="Build",
)
def export(
    fmt: Annotated[
        ExportFormat,
        typer.Argument(
            help="markdown or html (pandoc), tex4ht (make4ht), latexml (latexmlc)."
        ),
    ],
    debug: DebugOpt = False,
) -> None:
    """Export to Markdown/HTML via pandoc, or HTML via TeX4ht (make4ht) or LaTeXML."""
    _intro(f"texi export {fmt.value}")
    cwd = Path.cwd()
    config = load_project_config(cwd / "texproject.toml")
    src = (cwd / config.src_dir).resolve()
    main = config.main_files[0]
    if not (src / main).is_file():
        console.print(f"[red]Error:[/red] source file not found: {src / main}")
        raise typer.Exit(code=1)
    out_dir = cwd / "build" / "export" / fmt.value
    aux_dir = (cwd / config.build.aux_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    name = config.jobname

    match fmt:
        case ExportFormat.MARKDOWN | ExportFormat.HTML:
            tool = "pandoc"
            bibs = sorted(src.rglob("*.bib"))
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
            tool = "make4ht"
            target = out_dir
            # make4ht mangles absolute -B/-d paths (nests them under the build dir
            # and leaves output in the source dir), so pass them relative to src.
            rel = lambda p: os.path.relpath(p, src)  # noqa: E731
            cmd = [tool, "-u", "-B", rel(aux_dir), "-d", rel(target), main, "mathml"]
        case ExportFormat.LATEXML:
            tool = "latexmlc"
            target = out_dir / f"{name}.html"
            cmd = [tool, main, f"--dest={target}"]

    if fmt is ExportFormat.TEX4HT:
        _ensure_make4ht()
    if not shutil.which(tool):
        console.print(f"[red]{tool} not found.[/red] Try `{_EXPORT_HINTS[tool]}`.")
        raise typer.Exit(code=1)

    env = os.environ.copy()
    env_dir = cwd / ".texenv"
    if env_dir.exists():
        env.update(_texenv_vars(env_dir))
    _debug_cmd(cmd)
    try:
        subprocess.run(
            cmd, cwd=src, env=env, check=True
        )  # run in src_dir so \input/.bib resolve
    except subprocess.CalledProcessError as e:
        console.print(f"\n[red]✖ {tool} failed. Check the logs above.[/red]")
        raise typer.Exit(code=1) from e
    outro(f"[green]✔ Exported to {escape(str(target.relative_to(cwd)))}[/green]")


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
def preview(strict: StrictOpt = False, debug: DebugOpt = False) -> None:
    """Build, open the viewer, and rebuild on every change (latexmk -pvc). Ctrl-C to stop."""
    _intro("texi preview")
    _run_latexmk(strict=strict, preview=True)


@app.command(
    help="Show which files the last build actually read, as a directory tree.",
    rich_help_panel="Build",
)
def tree(project_dir: Path | None = None, fls_filename: str | None = None) -> None:
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
                "Tlmgr path": tlmgr_path,
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
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Don't ask; use sudo if the TeX Live tree isn't writable.",
    ),
) -> None:
    """Rebuild LaTeX formats (system via sudo by default)."""
    sudo = False if user else _needs_sudo("fmtutil-sys --all", yes=yes)
    try:
        console.print(
            escape(tools.fmtutil_user() if user else tools.fmtutil_sys(sudo=sudo))
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
    help="Rebuild TeX's file-name databases (ls-R). Use --local for just .texenv/.",
    rich_help_panel="TeX Live maintenance",
)
def mktexlsr(
    local: bool = typer.Option(False, "--local", help="Only index .texenv/ (no sudo)."),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Don't ask; use sudo if the TeX Live tree isn't writable.",
    ),
) -> None:
    """Rebuild the ls-R filename databases (system via sudo by default)."""
    sudo = False if local else _needs_sudo("mktexlsr", yes=yes)
    try:
        out = (
            tools.mktexlsr(str(Path.cwd() / ".texenv"))
            if local
            else tools.mktexlsr(sudo=sudo)
        )
        console.print(escape(out))
    except sh.ErrorReturnCode as e:
        console.print(f"[red]{escape(str(e))}[/red]")
        raise typer.Exit(1) from e


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
    help="Update TeX Live with tlmgr, then rebuild formats and font maps.",
    rich_help_panel="TeX Live maintenance",
)
def update(
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Don't ask; use sudo if the TeX Live tree isn't writable.",
    ),
) -> None:
    """Update TeX Live (sudo tlmgr update --self --all), then rebuild formats and font maps."""
    sudo = _needs_sudo("tlmgr update --self --all", yes=yes)
    try:
        console.print(escape(tools.tlmgr_update(sudo=sudo)))
        console.print(escape(tools.fmtutil_sys(sudo=sudo)))
        console.print(escape(tools.updmap_sys(sudo=sudo)))
    except sh.ErrorReturnCode as e:
        console.print(f"[red]{escape(str(e))}[/red]")
        raise typer.Exit(1) from e


_ORDER = [
    "init",
    "add",
    "sync",
    "scan",
    "info",
    "build",
    "clean",
    "preview",
    "fmt",
    "export",
    "tree",
    "doctor",
    "formats",
    "updmap",
    "mktexlsr",
    "luaotfload",
    "update",
]
app.registered_commands.sort(
    key=lambda c: (
        _ORDER.index(c.callback.__name__)
        if c.callback.__name__ in _ORDER
        else len(_ORDER)
    )
)


if __name__ == "__main__":
    app()
