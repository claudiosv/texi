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
from collections.abc import Callable, Generator
from pathlib import Path
from typing import Annotated, Any

import inflect
import sh
import typer
from pyclack.core import ShimmerProgress, is_cancel
from pyclack.prompts import confirm, note, outro
from pyclack.utils.console import console
from pyclack.utils.styling import S_BAR_START, Color
from pydantic import ValidationError
from rich.console import Console
from rich.filesize import decimal
from rich.markup import escape
from rich.text import Text
from rich.tree import Tree

from texi import tools
from texi.config import TexProjectConfig

app = typer.Typer(help="texi: LaTeX Project Workspace Manager")
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
        help="Print every tlmgr command and its full output.",
    ),
]


@app.callback()
def main(debug: DebugOpt = False) -> None:
    """texi: LaTeX Project Workspace Manager."""


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


tlmgr_path = shutil.which("tlmgr")
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


def generate_latexmkrc(config: TexProjectConfig, rc_path: Path) -> None:
    """Generates a .latexmkrc file from the validated Pydantic model."""
    rc_lines: list[str] = ["# Auto-generated by texi.", "# Do not edit manually.", ""]

    # mode='json' ensures all enums are reduced to their primitive values
    config_dict = config.latexmk.model_dump(mode="json")

    for section_name, settings in config_dict.items():
        rc_lines.append(f"# --- {section_name.capitalize()} ---")
        for key, value in settings.items():
            if key == "optimize_pdf" or value is None:
                continue
            if isinstance(value, list) and not value:
                continue

            perl_val = format_perl_value(key, value)
            prefix = "@" if isinstance(value, list) else "$"
            rc_lines.append(f"{prefix}{key} = {perl_val};")
        rc_lines.append("")

    if config.latexmk.execution.optimize_pdf:
        base_cmd = config.latexmk.programs.pdflatex or "pdflatex %O %S"
        pdflatex_cmd = inject_pdf_primitives(base_cmd)
        rc_lines.append("# --- Pdf compression (optimize_pdf) ---")
        rc_lines.append(f"$pdflatex = '{pdflatex_cmd}';")
        rc_lines.append("")

    rc_path.write_text("\n".join(rc_lines), encoding="utf-8")


def _tlmgr_info(*cmd_input: list[str]) -> subprocess.CompletedProcess:
    """Runs tlmgr info with the given input and returns the completed process."""
    cmd: list[str] = [
        tlmgr_path,
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
    sty_name: str, console: Console, cache: dict[str, list[str]] | None = None
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
    suffixes = (f"{sty_name}.sty", f"{sty_name}.cls")

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


@app.command()
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
    outro("[green]✔ Scan complete[/green]")


@app.command()
def init(
    force: bool = typer.Option(
        False, "--force", help="Overwrite an existing texproject.toml."
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
        config = TexProjectConfig(dependencies=dependencies)
    except ValidationError as e:
        console.print("[red]Configuration Validation Error:[/red]")
        console.print(e)
        raise typer.Exit(code=1) from e

    lines = ["dependencies = ["]
    lines += [f'  "{dep}",' for dep in config.dependencies]
    lines.append("]")
    lines.append("")
    config_path.write_text("\n".join(lines), encoding="utf-8")

    outro(
        f"[green]✔ Created {config_path.name} with {len(dependencies)} dependencies.[/green]"
    )


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


def _ensure_tl_writable() -> None:
    """tlmgr needs a writable TeX Live tree; offer to chown it to the current user."""
    root = _run(["kpsewhich", "-var-value=TEXMFROOT"]).stdout.strip()
    if not root or os.access(Path(root) / "tlpkg", os.W_OK):
        return
    target = Path(
        root
    ).parent  # e.g. /usr/local/texlive, so yearly upgrades keep working
    if not _confirm(
        f"{target} is not writable, so tlmgr can't install packages. "
        f"Run `sudo chown -R {getpass.getuser()} {target}`?",
        default=True,
    ):
        raise typer.Exit(code=1)
    cmd = ["sudo", "chown", "-R", getpass.getuser(), str(target)]
    _debug_cmd(cmd)
    if subprocess.run(cmd).returncode != 0:
        console.print("[red]chown failed.[/red]")
        raise typer.Exit(code=1)


def _update_tlmgr() -> None:
    """Run `tlmgr update --self` and log whether it updated tlmgr."""
    console.print("[dim]Updating tlmgr...[/dim]")
    result = _run([tlmgr_path, "update", "--self"])
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
    install = [tlmgr_path, "install", "latexmk", "--machine-readable"]
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


@app.command()
def sync(
    no_update: bool = typer.Option(
        False,
        "--no-update",
        help="Don't update the project's packages after installing.",
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
    cwd = Path.cwd()
    config = load_project_config(cwd / "texproject.toml")
    env_dir = cwd / ".texenv"

    if tlmgr_path:
        _ensure_tl_writable()
        _update_tlmgr()
    _ensure_latexmk()
    if not config.dependencies:
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
            init_cmd = [tlmgr_path, "init-usertree", "--usertree", str(env_dir)]
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

        n = len(config.dependencies)
        console.print(
            f"[blue]Fetching metadata for {n} {plural('package', n)}:[/blue]\n"
            f"[dim]{', '.join(sorted(config.dependencies))}[/dim]"
        )
        valid_dependencies, system_dependencies, already_installed = filter_packages(
            config.dependencies, env_dir, prog
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
                f"[blue]Installing {len(valid_dependencies)} relocatable "
                f"{plural('package', len(valid_dependencies))}:[/blue]\n"
                f"[dim]{', '.join(sorted(valid_dependencies))}[/dim]"
            )
            _install(
                prog,
                "Installing local packages",
                [
                    tlmgr_path,
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
                [tlmgr_path, "install", *system_dependencies, "--machine-readable"],
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
                        tlmgr_path,
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
                    [tlmgr_path, "update", "--machine-readable", *system],
                )

    _note(
        title=f"Synced {len(all_dependencies)} {plural('dependency', len(all_dependencies))} into .texenv/",
        content=[escape(", ".join(sorted(all_dependencies)))],
    )
    outro("[green]✔ Sync complete[/green]")


def _run_latexmk(*, strict: bool, preview: bool) -> None:
    """Generate .latexmkrc (with CLI overrides) and run latexmk in the project."""
    cwd = Path.cwd()
    env_dir = cwd / ".texenv"

    config = load_project_config(cwd / "texproject.toml")
    execution = config.latexmk.execution
    execution.halt_on_error = strict
    execution.force_mode = not strict
    execution.preview_mode = False
    execution.preview_continuous_mode = preview
    generate_latexmkrc(config, cwd / ".latexmkrc")
    console.print("[dim]Generated .latexmkrc from texproject.toml[/dim]")

    env = os.environ.copy()
    if env_dir.exists():
        env["TEXMFHOME"] = str(env_dir.resolve())
        env["max_print_line"] = "100000"  # Avoid line wrapping in logs
        console.print("[dim]Using local .texenv for compilation[/dim]")
    else:
        console.print(
            "[yellow]Warning: .texenv not found. Run `texi sync` for isolated dependencies.[/yellow]"
        )

    _ensure_latexmk()
    cmd = ["latexmk", "-g"]
    if not strict:
        cmd.append("-interaction=nonstopmode")
    _debug_cmd(cmd)
    console.print("[bold blue]Starting latexmk...[/bold blue]\n")
    try:
        subprocess.run(cmd, env=env, check=True)
    except subprocess.CalledProcessError as e:
        console.print("\n[red]✖ Build failed. Check the logs above.[/red]")
        raise typer.Exit(code=1) from e
    except KeyboardInterrupt:  # normal way to leave -pvc
        if not preview:
            raise
        return
    outro("[green]✔ Build completed successfully![/green]")


StrictOpt = Annotated[
    bool,
    typer.Option("--strict", help="Stop at the first error (halt_on_error)."),
]


@app.command()
def build(strict: StrictOpt = False, debug: DebugOpt = False) -> None:
    """Generate .latexmkrc and compile the project."""
    _intro("texi build")
    output_file = Path.cwd() / "build" / "main.pdf"
    if output_file.exists():
        console.print(f"[dim]Removing existing output file: {output_file}[/dim]")
        output_file.unlink()
    _run_latexmk(strict=strict, preview=False)


@app.command()
def preview(strict: StrictOpt = False, debug: DebugOpt = False) -> None:
    """Build, open the viewer, and rebuild on every change (latexmk -pvc). Ctrl-C to stop."""
    _intro("texi preview")
    _run_latexmk(strict=strict, preview=True)


@app.command()
def tree(project_dir: Path | None = None, fls_filename: str = "main.fls") -> None:
    if project_dir is None:
        project_dir = Path.cwd()
    fls_path = project_dir / "build" / "aux" / fls_filename
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


@app.command()
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


@app.command()
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


@app.command()
def formats(
    user: bool = typer.Option(
        False, "--user", help="Rebuild user formats (fmtutil-user)."
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the sudo confirmation."),
) -> None:
    """Rebuild LaTeX formats (system via sudo by default)."""
    if not (user or yes or _confirm("Run `sudo fmtutil-sys --all`?", default=True)):
        raise typer.Exit(1)
    try:
        console.print(escape(tools.fmtutil_user() if user else tools.fmtutil_sys()))
    except sh.ErrorReturnCode as e:
        console.print(f"[red]{escape(str(e))}[/red]")
        raise typer.Exit(1) from e


@app.command()
def update(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the sudo confirmation."),
) -> None:
    """Update TeX Live (sudo tlmgr update --self --all), then rebuild formats."""
    if not (
        yes
        or _confirm(
            "Run `sudo tlmgr update --self --all` and rebuild formats?", default=True
        )
    ):
        raise typer.Exit(1)
    try:
        console.print(escape(tools.tlmgr_update()))
        console.print(escape(tools.fmtutil_sys()))
    except sh.ErrorReturnCode as e:
        console.print(f"[red]{escape(str(e))}[/red]")
        raise typer.Exit(1) from e


if __name__ == "__main__":
    app()
