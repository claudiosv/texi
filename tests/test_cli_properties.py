"""Property tests of pure helpers in texi.cli and texi.config (no external tools
but perl, used where installed to check the generated rc file)."""

import json
import os
import shlex
import shutil
import string
import subprocess
import tempfile
import tomllib
from collections.abc import Generator
from contextlib import contextmanager, suppress
from pathlib import Path

import pytest
import typer
from hypothesis import example, given, settings
from hypothesis import strategies as st
from typer.testing import CliRunner

from texi import cli
from texi.config import BuildConfig, LatexmkDirectories, TexFmtConfig, TexProjectConfig

FEW = settings(max_examples=50, deadline=None)

hostile = st.text(
    st.one_of(st.sampled_from("'\\ ab$@\"\n#%{}[]"), st.characters(codec="utf-8")),
    max_size=20,
)


def read_perl_literal(text: str, pos: int = 0) -> tuple[str, int]:
    # Decode the Perl single-quoted literal at `pos`; return it and the end offset.
    # Inside such a literal only \\ and \' are escapes.
    assert text[pos] == "'", text[pos : pos + 20]
    out: list[str] = []
    pos += 1
    while pos < len(text):
        c = text[pos]
        if c == "'":
            return "".join(out), pos + 1
        if c == "\\" and text[pos + 1 : pos + 2] in {"\\", "'"}:
            pos += 1
            c = text[pos]
        out.append(c)
        pos += 1
    msg = "unterminated Perl literal"
    raise AssertionError(msg)


def assigned(rc: str, target: str) -> str:
    # Value of the single-quoted assignment `target = '...';` that starts a line.
    marker = f"\n{target} = "
    value, end = read_perl_literal(rc, rc.index(marker) + len(marker))
    assert rc.startswith(";\n", end)
    return value


@contextmanager
def no_skim() -> Generator[None]:
    """Keep the rendered rc independent of the machine's installed viewers."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(cli, "_skim_app", lambda: False)
        yield


def render(**tables: object) -> str:
    with no_skim():
        return cli.render_latexmkrc(TexProjectConfig.model_validate(tables))


# --- Perl quoting -------------------------------------------------------------


@given(value=hostile)
@example("\\")
@example("'")
@example("a\\'b")
def test_perl_str_decodes_back(value: str) -> None:
    quoted = cli._perl_str(value)
    assert read_perl_literal(quoted) == (value, len(quoted))


# format_perl_value escapes every string, so the values that need it (a quote, a
# double backslash or a final backslash) are part of `hostile` here too.
scalar_keys = st.sampled_from([
    "jobname",
    "pdf_previewer",
    "pdf_update_command",
    "out_dir",
])


@given(key=scalar_keys, value=hostile)
@example("jobname", "a\\\\'")
def test_format_perl_value_string_decodes_back(key: str, value: str) -> None:
    quoted = cli.format_perl_value(key, value)
    assert read_perl_literal(quoted) == (value, len(quoted))


@given(values=st.lists(hostile, max_size=5))
def test_format_perl_value_list_decodes_back(values: list[str]) -> None:
    text = cli.format_perl_value("generated_exts", values)
    assert text[0] == "("
    assert text[-1] == ")"
    got: list[str] = []
    pos = 1
    while pos < len(text) - 1:
        value, pos = read_perl_literal(text, pos)
        got.append(value)
        if pos < len(text) - 1:
            assert text.startswith(", ", pos)
            pos += 2
    assert got == values


@given(value=st.one_of(st.booleans(), st.integers()))
def test_format_perl_value_numbers(value: int) -> None:
    assert int(cli.format_perl_value("max_repeat", value)) == value


env_names = st.from_regex(r"TEXI_P[A-Z0-9_]{0,8}", fullmatch=True)
file_names = hostile.filter(lambda s: s.strip("./") and "/" not in s)


@FEW
@given(
    env=st.dictionaries(env_names, hostile, max_size=3),
    files=st.lists(file_names, min_size=1, max_size=3),
    command=hostile.filter(bool),
    jobname=hostile.filter(bool),
)
def test_render_latexmkrc_literals_decode_back(
    env: dict[str, str], files: list[str], command: str, jobname: str
) -> None:
    rc = render(
        project={"src_dir": "."},
        build={
            "env": env,
            "default_files": files,
            "bibtex": "unused",
            "pdflatex": command,
            "jobname": jobname,
            "synctex": False,
            "file_line_error": False,
        },
    )
    for name, value in env.items():
        assert assigned(rc, f"$ENV{{{name}}}") == value
    assert assigned(rc, "$jobname") == jobname
    assert assigned(rc, "$pdflatex") == command
    marker = "\n@default_files = ("
    pos = rc.index(marker) + len(marker)
    got = []
    while rc[pos] != ")":
        value, pos = read_perl_literal(rc, pos)
        got.append(value)
        if rc.startswith(", ", pos):
            pos += 2
    assert got == [str(Path(f)) for f in files]


def test_quote_in_pdf_previewer() -> None:
    rc = render(build={"pdf_previewer": "x 'y'"})
    assert assigned(rc, "$pdf_previewer") == "x 'y'"


def test_quote_in_jobname() -> None:
    rc = render(project={"name": "o'brien"})
    assert assigned(rc, "$jobname") == "o'brien"


def test_trailing_backslash_in_scalar() -> None:
    quoted = cli.format_perl_value("jobname", "a\\")
    assert read_perl_literal(quoted) == ("a\\", len(quoted))


def test_quote_in_list_item() -> None:
    text = cli.format_perl_value("generated_exts", ["a'b"])
    assert read_perl_literal(text, 1)[0] == "a'b"


def test_double_quote_in_out_dir() -> None:
    rc = render(build={"out_dir": 'a"b/'})
    assert '$out_dir = "$root/a\\"b/";' in rc


# --- engine_command -----------------------------------------------------------

words = st.sampled_from([
    "pdflatex",
    "%O",
    "%S",
    "-synctex=1",
    "-shell-escape",
    "-file-line-error",
    "-draftmode",
    "x=y",
])
# any whitespace may lead, trail or separate; "" or blank selects the default template
spaces = st.sampled_from(["", " ", "  ", "\t", "\n"])


@st.composite
def commands(draw: st.DrawFn) -> str:
    parts = draw(st.lists(words, max_size=5))
    seps = [draw(spaces) or " " for _ in parts[1:]]
    body = "".join(p + s for p, s in zip(parts, [*seps, ""], strict=False))
    return draw(spaces) + body + draw(spaces)


bases = st.one_of(st.none(), commands())


@given(
    base=bases,
    synctex=st.booleans(),
    shell=st.booleans(),
    fle=st.booleans(),
    extra=st.lists(words.filter(lambda w: w.startswith("-")), max_size=3),
)
def test_engine_command(
    base: str | None, synctex: bool, shell: bool, fle: bool, extra: list[str]
) -> None:
    cfg = BuildConfig(
        pdflatex=base,
        synctex=synctex,
        shell_escape=shell,
        file_line_error=fle,
        extra_args=extra,
    )
    wanted = [
        flag
        for flag, on in (
            ("-synctex=1", synctex),
            ("-shell-escape", shell),
            ("-file-line-error", fle),
        )
        if on
    ] + extra
    added = [f for f in wanted if f not in (base or "").split()]
    got = cli.engine_command("pdflatex", cfg)
    if not added:
        assert got == base
        return
    assert got is not None
    exe, *rest = ((base or "").strip() or "pdflatex %O %S").split()
    assert got.split() == [exe, *added, *rest]
    again = cfg.model_copy(update={"pdflatex": got})
    assert cli.engine_command("pdflatex", again) == got


def test_engine_command_leading_space() -> None:
    got = cli.engine_command("pdflatex", BuildConfig(pdflatex=" pdflatex %O %S"))
    assert got is not None
    assert got.split()[0] == "pdflatex"


@given(
    program=st.text("ab /.-", max_size=12).map(lambda s: f'"/{s}"'),
    lead=spaces,
    rest=st.lists(words, max_size=3).map(" ".join),
)
@example('"/a b/pdflatex"', "", "%O %S")
def test_engine_command_quoted_program(program: str, lead: str, rest: str) -> None:
    cfg = BuildConfig(
        pdflatex=f"{lead}{program} {rest}", synctex=False, file_line_error=False
    ).model_copy(update={"shell_escape": True})
    got = cli.engine_command("pdflatex", cfg)
    if "-shell-escape" in rest.split():
        return
    assert got == f"{program} -shell-escape {rest}".rstrip()


# --- config helpers -----------------------------------------------------------


@given(value=st.one_of(st.none(), hostile))
def test_ensure_trailing_slash(value: str | None) -> None:
    once = LatexmkDirectories.ensure_trailing_slash(value)
    assert LatexmkDirectories.ensure_trailing_slash(once) == once
    if value:
        assert once in {value, value + "/"}
        assert once.endswith("/")
    else:
        assert once == value


def test_jobname_without_targets() -> None:
    config = TexProjectConfig.model_validate({"project": {"targets": []}})
    assert isinstance(config.jobname, str)


@given(
    targets=st.lists(st.sampled_from(["main.tex", "a/b.tex"]), max_size=2),
    name=st.one_of(st.none(), st.just(""), st.just("n")),
)
def test_jobname_always_names_a_file(targets: list[str], name: str | None) -> None:
    config = TexProjectConfig.model_validate({
        "project": {"name": name, "targets": targets}
    })
    stem = Path(targets[0]).stem if targets else "main"
    assert config.jobname == (name or stem)


@given(content=st.binary(max_size=40))
@example(b"\xff")
def test_load_project_config_exits_cleanly(content: bytes) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "texproject.toml"
        path.write_bytes(content)
        with suppress(typer.Exit):
            cli.load_project_config(path)


# --- _add_to_toml -------------------------------------------------------------

packages = st.from_regex(r"[a-z][a-z0-9.-]{0,8}", fullmatch=True)


@st.composite
def project_files(draw: st.DrawFn) -> tuple[str, list[str]]:
    # Draw a texproject.toml and its dependencies, in layouts texi can append to.
    #
    # Included, as _add_to_toml once corrupted them: a comment after a last item
    # that has no comma, "]" in a comment inside the array, an indented
    # `dependencies` key or [project] header, and a [project] header without a
    # newline. Also dotted `project.` keys and brackets inside strings.
    old = draw(st.lists(packages, max_size=4, unique=True))
    layout = draw(
        st.sampled_from(["absent", "inline", "multiline", "no-header", "dotted"])
    )
    if layout in {"absent", "no-header"}:
        old = []
    indent = st.sampled_from(["", "  ", "\t"])
    comment = st.sampled_from(["", "  # note", "  # see [1]", "  # ]", "  # '\""])
    prefix = "project." if layout == "dotted" else ""
    lines = []
    if layout not in {"no-header", "dotted"}:
        header = draw(st.sampled_from(["[project]", "[ project ]"]))
        lines.append(draw(indent) + header + draw(comment))
    if layout != "no-header" and draw(st.booleans()):
        value = draw(st.sampled_from(['"x"', '"a ] # [b"', "'''\ndependencies = ['''"]))
        lines.append(f"{draw(indent)}{prefix}name = {value}")
    comma = "," if old and draw(st.booleans()) else ""
    key = f"{draw(indent)}{prefix}dependencies = ["
    if layout == "inline" or (layout == "dotted" and draw(st.booleans())):
        lines.append(f"{key}{', '.join(map(json.dumps, old))}{comma}]{draw(comment)}")
    elif layout == "multiline":
        lines.append(key + draw(comment))
        for i, name in enumerate(old):
            sep = "," if i < len(old) - 1 else comma
            lines.append(f"  {json.dumps(name)}{sep}{draw(comment)}")
        lines.append("]" + draw(comment))
    else:
        old = []
    if draw(st.booleans()):
        lines += ["", "[build]", "synctex = false  # keep"]
    return "\n".join(lines) + draw(st.sampled_from(["", "\n"])), old


def add_to_toml(text: str, new: list[str]) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "texproject.toml"
        path.write_text(text, encoding="utf-8")
        cli._add_to_toml(path, new)
        return path.read_text(encoding="utf-8")


@FEW
@given(file=project_files(), new=st.lists(packages, min_size=1, max_size=3))
def test_add_to_toml_appends(file: tuple[str, list[str]], new: list[str]) -> None:
    text, old = file
    before = tomllib.loads(text)
    after = tomllib.loads(add_to_toml(text, new))
    project = before.setdefault("project", {})
    project["dependencies"] = old + new
    assert after == before


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(
            '[project]\ndependencies = [\n  "a" # c\n]\n',
            id="comment-after-last-item",
        ),
        pytest.param(
            '[project]\ndependencies = [\n  # see [1]\n  "a",\n]\n',
            id="bracket-in-comment",
        ),
        pytest.param("[project]", id="header-without-newline"),
        pytest.param('[project]\n  dependencies = ["a"]\n', id="indented-key"),
        pytest.param('  [project]\nname = "x"\n', id="indented-header"),
    ],
)
def test_add_to_toml_keeps_valid_toml(text: str) -> None:
    old = tomllib.loads(text).get("project", {}).get("dependencies", [])  # valid
    got = tomllib.loads(add_to_toml(text, ["new"]))
    assert got["project"]["dependencies"] == [*old, "new"]


def test_add_to_toml_leaves_an_unsupported_layout_alone() -> None:
    text = 'project = { name = "x" }\n'
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "texproject.toml"
        path.write_text(text, encoding="utf-8")
        with pytest.raises(typer.Exit):
            cli._add_to_toml(path, ["new"])
        assert path.read_text(encoding="utf-8") == text


# --- texi.lock ----------------------------------------------------------------

# package names: ASCII, including ones that need a quoted TOML key, and any
# other character
lock_names = st.text(
    st.one_of(
        st.sampled_from(string.ascii_letters + string.digits + "-_. \"'\\=[]#\x7f"),
        st.characters(codec="utf-8"),
    ),
    max_size=12,
)


@FEW
@given(
    revs=st.dictionaries(lock_names, st.integers(0, 10**9), max_size=6),
    year=st.sampled_from([None, "2026"]),
    repository=st.one_of(
        st.sampled_from([None, "https://r.example/tlnet", 'a "b" \\c']),
        st.text(st.characters(codec="utf-8"), min_size=1, max_size=10),
    ),
)
@example({"\U0001f600": 1}, None, "\U0001f600\x7f")
def test_lock_round_trip(
    revs: dict[str, int], year: str | None, repository: str | None
) -> None:
    with tempfile.TemporaryDirectory() as tmp, pytest.MonkeyPatch.context() as mp:
        # the real one may ask the system TeX Live
        mp.setattr(cli, "_texlive_year", lambda _env_dir: year)
        mp.setattr(cli, "PROJECT_REPOSITORY", repository, raising=False)
        path = Path(tmp) / "texi.lock"
        cli._write_lock(path, revs)
        assert cli._read_lock(path) == revs
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        assert data.get("texlive") == year
        assert data.get("repository") == repository


def read_lock(content: bytes) -> dict[str, int]:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "texi.lock"
        path.write_bytes(content)
        return cli._read_lock(path)


# any bytes, and valid TOML whose `packages`, or a revision in it, has the wrong type
lock_texts = st.one_of(
    st.text().map(str.encode),
    st.binary(max_size=40),
    st.sampled_from([
        b"",
        b"[packages]\na = 1\n",
        b"[packages",
        b"x = 1\n",
        b"packages = []",
        b"packages = [['a', 1]]",
        b"[packages]\na = 'x'\nb = true\n",
        b"packages.a = " + b"1" * 5000,
        b"x = " + b"[" * 100_000,
    ]),
)


@FEW
@given(content=lock_texts)
def test_read_lock_tolerates_corrupt_text(content: bytes) -> None:
    revs = read_lock(content)
    assert isinstance(revs, dict)
    assert all(type(rev) is int for rev in revs.values())


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(b"packages = 'ab'", id="string"),
        pytest.param(b"packages = 1", id="integer"),
        pytest.param(b"\xff", id="not-utf8"),
    ],
)
def test_read_lock_corrupt_file(content: bytes) -> None:
    assert read_lock(content) == {}


# characters that str.splitlines() splits at but a tlpdb does not
line_breaks = st.sampled_from("\x0b\x0c\x1c\x1d\x1e\x85" + chr(0x2028) + chr(0x2029))


@FEW
@given(
    sep=line_breaks,
    revision=st.sampled_from(["2", "", "x", chr(0xB2), "9" * 5000]),
)
@example(chr(0x2028), "9")
def test_installed_packages_come_from_real_tlpdb_lines(sep: str, revision: str) -> None:
    forged = sep.join(["", "name evil", "revision 7"])
    text = f"name good\nrevision {revision}\nlongdesc x{forged}\n\n"
    with tempfile.TemporaryDirectory() as tmp, pytest.MonkeyPatch.context() as mp:
        mp.setattr(cli.tlpdb_mod, "load_local_installed", lambda: None)
        env_dir = Path(tmp)
        (env_dir / "tlpkg").mkdir()
        (env_dir / "tlpkg" / "texlive.tlpdb").write_text(text, encoding="utf-8")
        assert cli._usertree_packages(env_dir) == {"good"}
        assert cli._categories(env_dir) == {}
        revs = cli._package_revisions(env_dir, ["good", "evil"])
    ok = revision.isascii() and revision.isdigit() and len(revision) < 19
    assert revs == ({"good": int(revision)} if ok else {})


# --- tex-fmt config -----------------------------------------------------------

# any character TOML can hold, including those above U+FFFF (json.dumps would
# write them as surrogate pairs) and the control characters
toml_safe = st.text(
    st.one_of(
        st.sampled_from('\x00\x1f\x7f"\\\U0001f600'),
        st.characters(exclude_categories=["Cs"]),
    ),
    max_size=10,
)
positive = st.integers(1, 1000)


@FEW
@given(
    cfg=st.builds(
        TexFmtConfig,
        wrap=st.booleans(),
        wraplen=positive,
        wrapmin=st.one_of(st.none(), positive),
        tabsize=positive,
        tabchar=st.sampled_from(["space", "tab"]),
        lists=st.lists(toml_safe, max_size=3),
        verbatims=st.lists(toml_safe, max_size=3),
        no_indent_envs=st.lists(toml_safe, max_size=3),
        wrap_chars=st.lists(toml_safe, max_size=3),
        format_tables=st.booleans(),
        verbosity=st.sampled_from(["error", "warn", "info", "trace"]),
    )
)
@example(TexFmtConfig(lists=["\x7f"]))
def test_texfmt_config_is_toml(cfg: TexFmtConfig) -> None:
    expected = {
        key.replace("_", "-"): value
        for key, value in cfg.model_dump().items()
        if value is not None and value != []
    }
    assert tomllib.loads(cli.texfmt_config(cfg)) == expected


def test_texfmt_config_astral_character() -> None:
    cfg = TexFmtConfig(lists=["\U0001f600"])
    assert tomllib.loads(cli.texfmt_config(cfg))["lists"] == ["\U0001f600"]


# --- sty cache ----------------------------------------------------------------


@contextmanager
def cache_home() -> Generator[Path]:
    with tempfile.TemporaryDirectory() as tmp, pytest.MonkeyPatch.context() as mp:
        mp.setenv("XDG_CACHE_HOME", tmp)
        path = cli.sty_cache_path()
        assert path.is_relative_to(tmp)
        path.parent.mkdir(parents=True)
        yield path


@FEW
@given(
    cache=st.dictionaries(
        st.text(max_size=10), st.lists(st.text(max_size=10), max_size=3), max_size=4
    )
)
def test_sty_cache_round_trip(cache: dict[str, list[str]]) -> None:
    with cache_home():
        cli.save_sty_cache(cache)
        assert cli.load_sty_cache() == cache


@FEW
@given(
    content=st.one_of(
        st.text(max_size=50).map(str.encode),
        st.binary(max_size=40),
        st.sampled_from([
            b"{",
            b"[]",
            b"null",
            b'{"a": "xy"}',
            b'{"a": [1], "b": []}',
            b"[" * 200_000,
        ]),
    )
)
def test_sty_cache_load_never_raises_on_text(content: bytes) -> None:
    with cache_home() as path:
        path.write_bytes(content)
        cache = cli.load_sty_cache()  # the result type is checked below
    assert isinstance(cache, dict)
    for names in cache.values():
        assert isinstance(names, list)
        assert all(isinstance(n, str) for n in names)


def test_sty_cache_not_utf8() -> None:
    with cache_home() as path:
        path.write_bytes(b"\xff")
        assert cli.load_sty_cache() == {}


def test_sty_cache_not_an_object() -> None:
    with cache_home() as path:
        path.write_text("[]", encoding="utf-8")
        assert cli.load_sty_cache() == {}


# --- texi activate ------------------------------------------------------------


def read_fish(line: str) -> list[str]:
    # Words of one fish command line made of bare and single-quoted words.
    words: list[str] = []
    word: str | None = None
    quoted = False
    i = 0
    while i < len(line):
        c = line[i]
        if quoted:
            if c == "\\" and line[i + 1 : i + 2] in {"\\", "'"}:
                i += 1
                word = (word or "") + line[i]
            elif c == "'":
                quoted = False
            else:
                word = (word or "") + c
        elif c == "'":
            quoted = True
            word = word or ""
        elif c == " ":
            if word is not None:
                words.append(word)
            word = None
        else:
            word = (word or "") + c
        i += 1
    assert not quoted
    return words if word is None else [*words, word]


# one line each, so that fish output can be read line by line
shell_text = st.text(
    st.one_of(
        st.sampled_from("'\\ $\"`;&|"),
        st.characters(codec="utf-8", exclude_characters="\0\n\r"),
    ),
    max_size=12,
).filter(lambda s: s.splitlines() in ([], [s]))
path_entries = shell_text.filter(lambda s: s and os.pathsep not in s)
dir_names = st.text("ab '$\\\"", min_size=1, max_size=6)


@FEW
@given(
    shell=st.sampled_from(list(cli.ShellName)),
    name=dir_names,
    path=st.lists(path_entries, min_size=1, max_size=3),
    variables=st.dictionaries(env_names, shell_text, max_size=3),
    off=st.booleans(),
    has_bin=st.booleans(),
)
def test_shell_commands_round_trip(
    shell: cli.ShellName,
    name: str,
    path: list[str],
    variables: dict[str, str],
    off: bool,
    has_bin: bool,
) -> None:
    with tempfile.TemporaryDirectory() as tmp, pytest.MonkeyPatch.context() as mp:
        env_dir = Path(tmp) / name
        # with and without bin/<arch>: without it PATH must not get an empty entry
        env_dir.mkdir()
        bin_s = str((env_dir / "bin" / "arch").resolve())
        if has_bin:
            (env_dir / "bin" / "arch").mkdir(parents=True)
        mp.setenv("PATH", os.pathsep.join([bin_s, *path] if has_bin else path))
        out = cli._shell_commands(shell, env_dir, variables, off=off)
        root = str(env_dir.resolve())
    names = sorted(variables)
    entries = path if off or not has_bin else [bin_s, *path]
    assert "" not in entries
    scrubbed = list(cli.texenv.SCRUBBED)
    if shell is cli.ShellName.FISH:
        expected = [["set", "-gx", "PATH", *entries]]
        if off:
            expected += [["set", "-e", n] for n in [*names, "TEXI_ENV"]]
        else:
            expected += [["set", "-gx", n, variables[n]] for n in names]
            expected += [["set", "-e", n] for n in scrubbed] if has_bin else []
            expected += [["set", "-gx", "TEXI_ENV", root]]
        assert [read_fish(line) for line in out.split("\n")[:-1]] == expected
        return
    words = ["export", f"PATH={os.pathsep.join(entries)}"]
    if off:
        for n in [*names, "TEXI_ENV"]:
            words += ["unset", n]
    else:
        for n in names:
            words += ["export", f"{n}={variables[n]}"]
        words += ["unset", *scrubbed] if has_bin else []
        words += ["export", f"TEXI_ENV={root}"]
    assert shlex.split(out) == words


def test_activate_usertree_without_bin() -> None:
    with tempfile.TemporaryDirectory() as tmp, pytest.MonkeyPatch.context() as mp:
        env_dir = Path(tmp) / ".texenv"
        cli.texenv.init_usertree(env_dir)
        mp.setenv("PATH", "/usr/bin")
        out = cli._shell_commands(cli.ShellName.BASH, env_dir, {}, off=False)
    path = shlex.split(out)[1].removeprefix("PATH=")
    assert "" not in path.split(os.pathsep)


@pytest.mark.parametrize("off", [False, True])
@pytest.mark.parametrize("shell", [cli.ShellName.BASH, cli.ShellName.ZSH])
def test_activate_never_sets_an_empty_path(shell: cli.ShellName, off: bool) -> None:
    with tempfile.TemporaryDirectory() as tmp, pytest.MonkeyPatch.context() as mp:
        env_dir = Path(tmp) / ".texenv"
        cli.texenv.init_usertree(env_dir)
        mp.setenv("PATH", "")
        out = cli._shell_commands(shell, env_dir, {}, off=off)
    assert "PATH=" not in out


@pytest.mark.parametrize("before", [None, "", "/usr/bin"])
def test_activate_env_adds_no_empty_path_entry(before: str | None) -> None:
    with tempfile.TemporaryDirectory() as tmp, pytest.MonkeyPatch.context() as mp:
        env_dir = Path(tmp) / ".texenv"
        cli.texenv.init_usertree(env_dir)
        (env_dir / "bin" / "arch").mkdir(parents=True)
        mp.setattr(cli, "latexmk_path", None)
        if before is None:
            mp.delenv("PATH", raising=False)
        else:
            mp.setenv("PATH", before)
        cli._activate_env(env_dir)
        cli._activate_env(env_dir)  # a second call adds nothing
        entries = os.environ["PATH"].split(os.pathsep)
    assert entries == [str(env_dir / "bin" / "arch"), *([before] if before else [])]


# --- texi init ----------------------------------------------------------------

# directory names become the project name
project_names = st.text(
    st.one_of(
        st.sampled_from("'\"\\ []#=\t\n\x7f\U0001f600"),
        st.characters(
            exclude_categories=["Cs", "Cn", "Co", "Mn"], exclude_characters="/\0"
        ),
    ),
    min_size=1,
    max_size=12,
).filter(lambda s: s not in {".", ".."})


@FEW
@given(name=project_names)
@example('a"b')
@example("\U0001f600")
def test_init_writes_valid_toml_for_any_directory_name(name: str) -> None:
    with tempfile.TemporaryDirectory() as tmp, pytest.MonkeyPatch.context() as mp:
        project = Path(tmp) / name
        project.mkdir()
        mp.chdir(project)
        mp.setattr(cli, "_prefetch_grammar", lambda: None)
        mp.setattr(cli, "_create_env", lambda repository=None: None)
        result = CliRunner().invoke(cli.app, ["init"])
        assert result.exit_code == 0, result.output
        config = tomllib.loads((project / "texproject.toml").read_text("utf-8"))
    assert config["project"]["name"] == name


# --- the rc file, as Perl reads it ---------------------------------------------

PERL = shutil.which("perl")
# Run the rc file, then print what it set, hex encoded: kind|name|value
DUMP_RC = r"""
my $rc = shift @ARGV;
do $rc;
die $@ if $@;
sub hx { unpack("H*", $_[0]) }
sub row { print join("|", $_[0], hx($_[1]), hx($_[2])), "\n" }
row("E", $_, $ENV{$_}) for keys %ENV;
my @scalars = qw(jobname pdf_previewer out_dir aux_dir clean_ext clean_full_ext);
row("S", $_, $$_) for @scalars;
row("L", "generated_exts", $_) for @generated_exts;
row("L", "default_files", $_) for @default_files;
"""


def run_rc(
    project: Path, rc: str, environ: dict[str, str]
) -> tuple[dict[str, str], dict[str, str], dict[str, list[str]]]:
    assert PERL
    path = project / ".latexmkrc"
    path.write_text(rc, encoding="utf-8")
    out = subprocess.run(
        [PERL, "-e", DUMP_RC, str(path)], env=environ, capture_output=True, check=True
    ).stdout.decode("ascii")
    env: dict[str, str] = {}
    scalars: dict[str, str] = {}
    lists: dict[str, list[str]] = {}
    for line in out.splitlines():
        kind, name, value = line.split("|")
        name, value = (bytes.fromhex(x).decode() for x in (name, value))
        if kind == "L":
            lists.setdefault(name, []).append(value)
        else:
            (env if kind == "E" else scalars)[name] = value
    return env, scalars, lists


# characters with a meaning in Perl strings and hash subscripts
perl_text = st.text(
    st.one_of(
        st.sampled_from("'\"\\$@%{}[]() ;->ab/\n"),
        st.characters(codec="utf-8", exclude_characters="\0"),
    ),
    max_size=12,
)
env_keys = perl_text.map(lambda s: "TEXI_P" + s.replace("=", ""))
directories = perl_text.filter(lambda s: s.strip("/"))


@pytest.mark.skipif(PERL is None, reason="needs perl")
@FEW
@given(
    env=st.dictionaries(env_keys, perl_text, max_size=3),
    value=perl_text.filter(bool),
    words_=st.lists(perl_text, min_size=1, max_size=3),
    out_dir=directories,
    dirs=st.lists(directories, min_size=1, max_size=3),
)
@example({"TEXI_P}; die 1; {": "v", "TEXI_P-x": "w"}, "o'b\\", ["a'b"], 'a"b', ["$x@y"])
def test_perl_reads_the_rc_file_back(
    env: dict[str, str],
    value: str,
    words_: list[str],
    out_dir: str,
    dirs: list[str],
) -> None:
    rc = render(
        build={
            "env": env,
            "jobname": value,
            "pdf_previewer": value,
            "generated_exts": words_,
            "clean_ext": words_,
            "clean_full_ext": words_,
            "out_dir": out_dir,
            "bib_dirs": dirs,
            "tex_inputs": dirs,
        }
    )
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        got_env, scalars, lists = run_rc(project, rc, {})
    root = str(project)
    for key, wanted in env.items():
        assert got_env[key] == wanted
    assert scalars["jobname"] == scalars["pdf_previewer"] == value
    assert scalars["clean_ext"] == scalars["clean_full_ext"] == " ".join(words_)
    assert lists["generated_exts"] == words_
    anchored = out_dir if out_dir.endswith("/") else out_dir + "/"
    if not Path(anchored).is_absolute():
        anchored = f"{root}/{anchored}"
    assert scalars["out_dir"] == anchored
    paths = [
        d if Path(d).is_absolute() else f"{root}/{d.strip('/')}" for d in ["src", *dirs]
    ]
    assert got_env["BIBINPUTS"] == got_env["TEXINPUTS"]
    assert got_env["BIBINPUTS"] == ":".join(f"{p}//" for p in paths) + ":"


@pytest.mark.skipif(PERL is None, reason="needs perl")
@pytest.mark.parametrize("name", ["plain", "my project", "paper [v2]", "a{b}*?~"])
@pytest.mark.parametrize("standalone", [False, True])
@pytest.mark.parametrize("before", [None, "", "/usr/bin"])
def test_rc_finds_the_tree_and_its_bin(
    name: str, standalone: bool, before: str | None
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve() / name
        (project / ".texenv").mkdir(parents=True)
        if standalone:
            (project / ".texenv" / "bin" / "arch").mkdir(parents=True)
        environ = {} if before is None else {"PATH": before}
        env, _, _ = run_rc(project, render(), environ)
    tree = f"{project}/.texenv"
    if standalone:
        assert env["TEXMFROOT"] == tree
        entries = [f"{tree}/bin/arch", *([before] if before else [])]
        assert env["PATH"] == os.pathsep.join(entries)
    else:
        assert env["TEXMFHOME"] == tree
        assert "TEXMFROOT" not in env
        assert env.get("PATH") == before
