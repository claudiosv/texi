"""``texi tlpdb``: inspect the TeX Live package database."""

from __future__ import annotations

import difflib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

import typer
from pyclack.utils.console import console
from rich.console import Console
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from texi import tlpdb as tl

tlpdb_app = typer.Typer(
    help="Inspect the TeX Live package database.",
    no_args_is_help=False,
    rich_markup_mode="rich",
)

err_console = Console(stderr=True)
MAX_FILES = 200
HL = "bold yellow"


def _set_debug(value: bool) -> None:
    # Lazy: texi.cli imports this module, so it cannot be imported at load time.
    if value:
        from texi import cli

        cli._set_debug(True)


def _debug() -> bool:
    from texi import cli

    return cli.DEBUG


RepoOpt = Annotated[
    str | None,
    typer.Option(
        "--repository",
        "-r",
        help="TeX Live repository URL. Default: texproject.toml, then "
        "`tlmgr option repository`, then mirror.ctan.org.",
    ),
]
LocalOpt = Annotated[
    Path | None,
    typer.Option("--local", help="Read a local tlpdb file (plain or .xz) instead."),
]
RefreshOpt = Annotated[
    bool, typer.Option("--refresh", help="Ignore the cache age and check the server.")
]
DebugOpt = Annotated[
    bool,
    typer.Option(
        "--debug",
        callback=_set_debug,
        is_eager=True,
        help="Show the repository resolution and cache details.",
    ),
]


def _fail(msg: str) -> typer.Exit:
    console.print(f"[red]Error:[/red] {escape(msg)}")
    return typer.Exit(code=1)


def _project_repository() -> str | None:
    """``[project] repository`` of the texproject.toml in the cwd, if any."""
    cfg = Path.cwd() / "texproject.toml"
    if not cfg.is_file():
        return None
    from texi.cli import load_project_config

    try:
        return load_project_config(cfg).project.repository
    except typer.Exit:
        return None


def resolve_repository(repository: str | None) -> tuple[str, str]:
    """Pick the repository; returns it and where the choice came from."""
    if repository:
        return tl.normalize_repository(repository), "--repository"
    proj = _project_repository()
    if proj:
        return tl.normalize_repository(proj), "texproject.toml"
    tlmgr = tl.tlmgr_repository()
    if tlmgr:
        return tlmgr, "tlmgr option repository"
    return tl.DEFAULT_REPOSITORY, "default"


def _load(
    repository: str | None, local: Path | None, refresh: bool, *, quiet: bool = False
) -> tl.Tlpdb:
    """Load the database or exit with an error."""
    try:
        if local is not None:
            if _debug():
                err_console.print(f"[dim]local tlpdb: {escape(str(local))}[/dim]")
            return tl.load(local=local)
        repo, origin = resolve_repository(repository)
        if _debug():
            err_console.print(
                f"[dim]repository: {escape(repo)} (from {origin})\n"
                f"cache: {escape(str(tl.cache_dir()))}[/dim]"
            )
        if quiet:
            db = tl.load(repo, refresh=refresh)
        else:
            with console.status(f"Loading tlpdb from {repo} ..."):
                db = tl.load(repo, refresh=refresh)
    except tl.TlpdbError as e:
        raise _fail(str(e)) from e
    if db.offline:
        err_console.print(
            "[orange1]⚠ Could not reach the server; using the cached copy "
            f"({escape(str(db.cache_path))}).[/orange1]"
        )
    return db


def _fmt_bytes(n: int | None) -> str:
    if n is None:
        return "-"
    from rich.filesize import decimal

    return f"{decimal(n)} ({n:,} bytes)"


def _fmt_time(dt: datetime | None) -> str:
    if dt is None:
        return "unknown"
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def _age(dt: datetime | None) -> str:
    if dt is None:
        return ""
    secs = int((datetime.now(timezone.utc) - dt).total_seconds())
    if secs < 90:
        return f"{secs}s ago"
    if secs < 5400:
        return f"{secs // 60} min ago"
    if secs < 129600:
        return f"{secs // 3600} h ago"
    return f"{secs // 86400} d ago"


def _kv(rows: list[tuple[str, str]]) -> None:
    width = max(len(k) for k, _ in rows)
    for k, v in rows:
        console.print(f"[bold]{k:<{width}}[/bold]  {escape(v)}")


def _print_stats(db: tl.Tlpdb) -> None:
    st = db.stats()
    rows = [("URI", db.source or "(unknown)")]
    if db.cache_path:
        origin = "cache" if db.from_cache else "downloaded just now"
        rows.append(("Served from", f"{origin}, {db.cache_path}"))
    rows.append(("Fetched", f"{_fmt_time(db.fetched)} ({_age(db.fetched)})"))
    if db.compressed_bytes is not None:
        rows.append(("Compressed", _fmt_bytes(db.compressed_bytes)))
    rows.append(("Unpacked", _fmt_bytes(db.size_bytes)))
    rows.append(("Entries", f"{st.entries:,}"))
    if st.release:
        rel = f"TeX Live {st.release}"
        if st.minrelease:
            rel += f" (upgradable from {st.minrelease})"
        if st.frozen == "1":
            rel += ", frozen"
        elif st.frozen == "0":
            rel += ", not frozen"
        rows.append(("Release", rel))
    if st.revision:
        rows.append(("DB revision", st.revision))
    rows.append(("With binaries", f"{st.with_binfiles:,} packages"))
    if st.architectures:
        rows.append(("Architectures", ", ".join(st.architectures)))
    _kv(rows)

    table = Table(title="Entries per category", title_justify="left")
    table.add_column("category")
    table.add_column("entries", justify="right")
    for cat, n in st.categories.items():
        table.add_row(cat or "(none)", f"{n:,}")
    console.print(table)
    ftable = Table(title="Files listed", title_justify="left")
    ftable.add_column("section")
    ftable.add_column("files", justify="right")
    for sec, n in st.files.items():
        ftable.add_row(sec, f"{n:,}")
    console.print(ftable)


@tlpdb_app.callback(invoke_without_command=True)
def _default(ctx: typer.Context) -> None:
    """Inspect the TeX Live package database. Without a subcommand: `stats`."""
    if ctx.invoked_subcommand is None:
        stats()


@tlpdb_app.command()
def stats(
    repository: RepoOpt = None,
    local: LocalOpt = None,
    refresh: RefreshOpt = False,
    debug: DebugOpt = False,
) -> None:
    """Show where the database comes from, its size and what it contains."""
    _print_stats(_load(repository, local, refresh))


def _highlight(value: str, pats: list[re.Pattern[str]], style: str = "") -> Text:
    t = Text(value, style=style)
    for p in pats:
        t.highlight_regex(p, HL)
    return t


@tlpdb_app.command()
def search(
    query: Annotated[list[str], typer.Argument(help="Words (all must match).")],
    file: Annotated[
        bool, typer.Option("--file", "-f", help="Also search file names and paths.")
    ] = False,
    regex: Annotated[
        bool, typer.Option("--regex", "-e", help="Treat the query as a regex.")
    ] = False,
    category: Annotated[
        str | None,
        typer.Option("--category", "-c", help="Only this category (Package, ...)."),
    ] = None,
    limit: Annotated[
        int, typer.Option("--limit", "-n", help="Maximum rows (0 = all).")
    ] = 25,
    exact: Annotated[
        bool,
        typer.Option("--exact", help="Only exact package, CTAN or file name."),
    ] = False,
    repository: RepoOpt = None,
    local: LocalOpt = None,
    refresh: RefreshOpt = False,
    debug: DebugOpt = False,
) -> None:
    """Search package names, descriptions and (with --file) file names."""
    q = query[0] if regex and len(query) == 1 else " ".join(query)
    db = _load(repository, local, refresh)
    try:
        hits = db.search(q, regex=regex, in_files=file, category=category)
    except tl.TlpdbError as e:
        raise _fail(str(e)) from e
    if exact:
        ql = q.lower()
        hits = [
            h
            for h in hits
            if h.package.name.lower() == ql
            or h.package.catalogue.lower() == ql
            or any(f.rsplit("/", 1)[-1].lower() == ql for f in h.files)
        ]
    if not hits:
        console.print(f"[orange1]No match for[/orange1] {escape(q)}")
        raise typer.Exit(code=1)
    pats = (
        [re.compile(q, re.IGNORECASE)]
        if regex
        else [
            re.compile(re.escape(t), re.IGNORECASE)
            for word in query
            for t in word.split()
        ]
    )
    shown = hits if limit <= 0 else hits[:limit]
    table = Table(show_lines=False)
    table.add_column("name", no_wrap=True)
    table.add_column("category")
    table.add_column("rev", justify="right")
    table.add_column("description", overflow="fold")
    for h in shown:
        p = h.package
        name = _highlight(p.name, pats, "bold")
        if p.catalogue_key and p.catalogue_key != p.name:
            name.append(" (CTAN: ", "dim")
            name.append_text(_highlight(p.catalogue_key, pats, "dim"))
            name.append(")", "dim")
        desc = _highlight(p.shortdesc, pats)
        if h.matched_on == "longdesc":
            desc.append("  [match in long description]", "dim italic")
        for f in h.files[:3]:
            desc.append("\n")
            desc.append_text(_highlight(f, pats, "cyan"))
        if len(h.files) > 3:
            desc.append(f"\n... {len(h.files) - 3} more files", "dim")
        table.add_row(name, p.category, p.revision, desc)
    console.print(table)
    if len(shown) < len(hits):
        console.print(
            f"[dim]Showing {len(shown)} of {len(hits)} matches; "
            "use --limit to show more.[/dim]"
        )
    else:
        console.print(f"[dim]{len(hits)} match(es).[/dim]")


def _lookup(db: tl.Tlpdb, name: str) -> list[tl.TlPackage]:
    """Package by name, else by CTAN key; exit with suggestions if none."""
    pkg = db.get(name)
    if pkg:
        return [pkg]
    by_cat = db.by_catalogue(name)
    if by_cat:
        return by_cat
    lowered = {n.lower(): n for n in db.packages}
    if name.lower() in lowered:
        return [db.packages[lowered[name.lower()]]]
    close = difflib.get_close_matches(name, list(db.packages), n=5, cutoff=0.6)
    for p in db.packages.values():
        if p.catalogue_key and p.catalogue_key not in close:
            if (
                len(close) < 5
                and difflib.SequenceMatcher(
                    None, name.lower(), p.catalogue_key.lower()
                ).ratio()
                > 0.85
            ):
                close.append(p.name)
    msg = f"No TeX Live package or CTAN key named '{name}'."
    if close:
        msg += " Did you mean: " + ", ".join(close[:5]) + "?"
    raise _fail(msg)


_CONTAINER = (
    ("", "Container"),
    ("doc", "Doc container"),
    ("src", "Src container"),
)


def _print_info(
    db: tl.Tlpdb, p: tl.TlPackage, *, show_files: bool, all_files: bool
) -> None:
    console.print(
        Text.assemble((p.name, "bold cyan"), f"  {p.category}  r{p.revision}")
    )
    if p.shortdesc:
        console.print(Text(p.shortdesc, style="bold"))
    if p.longdesc:
        console.print()
        console.print(Text(p.longdesc))
    console.print()
    used = {"relocated", "execute", "postaction"}
    rows: list[tuple[str, str]] = []
    cat = p.catalogue + ("" if p.catalogue_key else " (same as name)")
    rows.append(("Catalogue", cat))
    for k in sorted(k for k in p.fields if k.startswith("catalogue-")):
        used.add(k)
        label = k.removeprefix("catalogue-").replace("-", " ")
        label = {"ctan": "CTAN path"}.get(label, label.capitalize())
        value = ", ".join(p.fields[k])
        if k == "catalogue-ctan":
            value += f"  (https://ctan.org/tex-archive{p.fields[k][0]})"
        rows.append((label, value))
    if p.first("relocated"):
        rows.append(("Relocated", p.first("relocated") or ""))
    _kv(rows)
    console.print()
    if p.depends:
        console.print(f"[bold]Depends[/bold] ({len(p.depends)})")
        console.print(Text("  " + ", ".join(p.depends)))
        console.print()
    crow: list[tuple[str, str]] = []
    for prefix, label in _CONTAINER:
        size, csum = f"{prefix}containersize", f"{prefix}containerchecksum"
        used.update((size, csum))
        if p.first(size):
            crow.append((label, f"{_fmt_size(p.first(size))}"))
            crow.append(("  checksum", p.first(csum) or "-"))
    if crow:
        _kv(crow)
        console.print()
    for key in ("execute", "postaction"):
        for v in p.fields.get(key, []):
            console.print(f"[bold]{key}[/bold]  {escape(v)}")
    if "execute" in p.fields or "postaction" in p.fields:
        console.print()
    other = [(k, v) for k, v in p.fields.items() if k not in used]
    if other:
        console.print("[bold]Other fields[/bold]")
        for k, vs in other:
            for v in vs:
                console.print(f"  {escape(k)}: {escape(v)}")
        console.print()
    total = sum(len(v) for v in p.files.values())
    if not total:
        console.print("[dim]No files listed.[/dim]")
        return
    for sec, files in p.files.items():
        size = p.sizes.get(sec)
        head = f"[bold]{sec}[/bold]: {len(files):,} file(s)"
        if size is not None:
            head += f", size={size:,}"
        console.print(head)
        if not show_files:
            continue
        cap = len(files) if all_files else MAX_FILES
        for f in files[:cap]:
            line = Text("  " + f.full_path)
            if f.attrs:
                line.append(
                    "  " + " ".join(f"{k}={v}" for k, v in f.attrs.items()), "dim"
                )
            console.print(line)
        if len(files) > cap:
            console.print(
                f"  [dim]... {len(files) - cap:,} more (use --all-files)[/dim]"
            )


def _fmt_size(value: str | None) -> str:
    if value and value.isdigit():
        from rich.filesize import decimal

        return f"{decimal(int(value))} ({int(value):,} bytes)"
    return value or "-"


@tlpdb_app.command()
def info(
    name: Annotated[str, typer.Argument(help="TeX Live package name or CTAN key.")],
    files: Annotated[
        bool, typer.Option("--files/--no-files", help="List the files.")
    ] = True,
    all_files: Annotated[
        bool,
        typer.Option("--all-files", help=f"Never truncate lists at {MAX_FILES} files."),
    ] = False,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print all fields as JSON.")
    ] = False,
    repository: RepoOpt = None,
    local: LocalOpt = None,
    refresh: RefreshOpt = False,
    debug: DebugOpt = False,
) -> None:
    """Show everything the database knows about a package."""
    db = _load(repository, local, refresh, quiet=as_json)
    pkgs = _lookup(db, name)
    if as_json:
        data = [p.to_dict() for p in pkgs]
        typer.echo(json.dumps(data[0] if len(data) == 1 else data, indent=2))
        return
    if len(pkgs) > 1:
        console.print(
            f"[dim]'{escape(name)}' is the CTAN key of {len(pkgs)} packages.[/dim]"
        )
    for i, p in enumerate(pkgs):
        if i:
            console.print("\n" + "-" * 40 + "\n")
        _print_info(db, p, show_files=files, all_files=all_files)
