"""Property tests of pure helpers in texi.cli and texi.config (no external tools)."""

import json
import os
import shlex
import string
import tempfile
import tomllib
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

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


# format_perl_value does not escape (see the xfail tests below), so leave out
# the values that need it: a quote, a double backslash or a final backslash.
unescaped_ok = hostile.filter(
    lambda v: "'" not in v and "\\\\" not in v and not v.endswith("\\")
)
scalar_keys = st.sampled_from([
    "jobname",
    "pdf_previewer",
    "pdf_update_command",
    "out_dir",
])


@given(key=scalar_keys, value=unescaped_ok)
def test_format_perl_value_string_decodes_back(key: str, value: str) -> None:
    quoted = cli.format_perl_value(key, value)
    assert read_perl_literal(quoted) == (value, len(quoted))


@given(values=st.lists(unescaped_ok, max_size=5))
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
    jobname=unescaped_ok.filter(bool),
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


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="format_perl_value does not escape quotes in pdf_previewer",
)
def test_quote_in_pdf_previewer() -> None:
    rc = render(build={"pdf_previewer": "x 'y'"})
    assert assigned(rc, "$pdf_previewer") == "x 'y'"


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="format_perl_value does not escape quotes in the jobname",
)
def test_quote_in_jobname() -> None:
    rc = render(project={"name": "o'brien"})
    assert assigned(rc, "$jobname") == "o'brien"


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="format_perl_value does not escape a trailing backslash",
)
def test_trailing_backslash_in_scalar() -> None:
    quoted = cli.format_perl_value("jobname", "a\\")
    assert read_perl_literal(quoted) == ("a\\", len(quoted))


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="format_perl_value does not escape quotes in list items",
)
def test_quote_in_list_item() -> None:
    text = cli.format_perl_value("generated_exts", ["a'b"])
    assert read_perl_literal(text, 1)[0] == "a'b"


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason='out_dir is put into a Perl "..." string without escaping',
)
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
# no leading space (see the xfail test below); "" selects the default template
bases = st.one_of(st.none(), st.lists(words, max_size=5).map(" ".join))


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
    exe, *rest = (base or "pdflatex %O %S").split()
    assert got.split() == [exe, *added, *rest]
    again = cfg.model_copy(update={"pdflatex": got})
    assert cli.engine_command("pdflatex", again) == got


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="a leading space puts the flags in front of the program name",
)
def test_engine_command_leading_space() -> None:
    got = cli.engine_command("pdflatex", BuildConfig(pdflatex=" pdflatex %O %S"))
    assert got is not None
    assert got.split()[0] == "pdflatex"


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


@pytest.mark.xfail(
    strict=True,
    raises=IndexError,
    reason="jobname indexes main_files[0] although targets = [] validates",
)
def test_jobname_without_targets() -> None:
    config = TexProjectConfig.model_validate({"project": {"targets": []}})
    assert isinstance(config.jobname, str)


# --- _add_to_toml -------------------------------------------------------------

packages = st.from_regex(r"[a-z][a-z0-9.-]{0,8}", fullmatch=True)


@st.composite
def project_files(draw: st.DrawFn) -> tuple[str, list[str]]:
    # Draw a texproject.toml and its dependencies, in layouts texi can append to.
    #
    # Left out, as _add_to_toml corrupts them (xfail tests below): a comment after
    # a last item that has no comma, "]" in a comment inside the array, an indented
    # `dependencies` key or [project] header, and a [project] header without a
    # newline.
    old = draw(st.lists(packages, max_size=4, unique=True))
    layout = draw(st.sampled_from(["absent", "inline", "multiline", "no-header"]))
    if layout in {"absent", "no-header"}:
        old = []
    lines = []
    if layout != "no-header":
        lines.append(draw(st.sampled_from(["[project]", "[project]  # main"])))
        if draw(st.booleans()):
            lines.append('name = "x"')
    comma = "," if old and draw(st.booleans()) else ""
    if layout == "inline":
        lines.append(f"dependencies = [{', '.join(map(json.dumps, old))}{comma}]")
    elif layout == "multiline":
        lines.append("dependencies = [")
        for i, name in enumerate(old):
            sep = "," if i < len(old) - 1 else comma
            note = "  # note" if sep and draw(st.booleans()) else ""
            lines.append(f"  {json.dumps(name)}{sep}{note}")
        lines.append("]")
    if draw(st.booleans()):
        lines += ["", "[build]", "synctex = false  # keep"]
    return "\n".join(lines) + "\n", old


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


@pytest.mark.xfail(
    strict=True,
    raises=tomllib.TOMLDecodeError,
    reason="_add_to_toml writes invalid TOML for this valid layout",
)
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
    tomllib.loads(text)  # the input is valid
    tomllib.loads(add_to_toml(text, ["new"]))


# --- texi.lock ----------------------------------------------------------------

# package names: ASCII, including ones that need a quoted TOML key
lock_names = st.text(
    string.ascii_letters + string.digits + "-_. \"'\\=[]#", max_size=12
)


@FEW
@given(
    revs=st.dictionaries(lock_names, st.integers(0, 10**9), max_size=6),
    year=st.sampled_from([None, "2026"]),
    repository=st.sampled_from([None, "https://r.example/tlnet", 'a "b" \\c']),
)
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


# valid UTF-8 whose `packages`, if any, is a table (other inputs: xfail below)
lock_texts = st.one_of(
    st.text().filter(lambda s: "packages" not in s and "\\" not in s),
    st.sampled_from(["", "[packages]\na = 1\n", "[packages", "x = 1\n"]),
)


@FEW
@given(text=lock_texts)
def test_read_lock_tolerates_corrupt_text(text: str) -> None:
    assert isinstance(read_lock(text.encode()), dict)


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(
            b"packages = 'ab'",
            marks=pytest.mark.xfail(
                strict=True,
                raises=ValueError,
                reason="_read_lock passes a string to dict()",
            ),
            id="string",
        ),
        pytest.param(
            b"packages = 1",
            marks=pytest.mark.xfail(
                strict=True,
                raises=TypeError,
                reason="_read_lock passes an integer to dict()",
            ),
            id="integer",
        ),
        pytest.param(
            b"\xff",
            marks=pytest.mark.xfail(
                strict=True,
                raises=UnicodeDecodeError,
                reason="_read_lock does not catch decoding errors",
            ),
            id="not-utf8",
        ),
    ],
)
def test_read_lock_corrupt_file(content: bytes) -> None:
    assert read_lock(content) == {}


# --- tex-fmt config -----------------------------------------------------------

# no characters above U+FFFF: json.dumps writes surrogate pairs (xfail below)
toml_safe = st.text(
    st.characters(max_codepoint=0xFFFF, exclude_categories=["Cs"]), max_size=10
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


@pytest.mark.xfail(
    strict=True,
    raises=tomllib.TOMLDecodeError,
    reason="json.dumps writes surrogate pair escapes, which TOML rejects",
)
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
@given(text=st.one_of(st.text(max_size=50), st.sampled_from(["{", "[]", "null"])))
def test_sty_cache_load_never_raises_on_text(text: str) -> None:
    with cache_home() as path:
        path.write_text(text, encoding="utf-8")
        cli.load_sty_cache()  # the result type is checked below


@pytest.mark.xfail(
    strict=True,
    raises=UnicodeDecodeError,
    reason="load_sty_cache does not tolerate a file that is not UTF-8",
)
def test_sty_cache_not_utf8() -> None:
    with cache_home() as path:
        path.write_bytes(b"\xff")
        assert cli.load_sty_cache() == {}


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="load_sty_cache returns JSON that is not an object",
)
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
)
def test_shell_commands_round_trip(
    shell: cli.ShellName,
    name: str,
    path: list[str],
    variables: dict[str, str],
    off: bool,
) -> None:
    with tempfile.TemporaryDirectory() as tmp, pytest.MonkeyPatch.context() as mp:
        env_dir = Path(tmp) / name
        # always with bin/<arch>: without it PATH gets an empty entry (xfail below)
        (env_dir / "bin" / "arch").mkdir(parents=True)
        bin_s = str((env_dir / "bin" / "arch").resolve())
        mp.setenv("PATH", os.pathsep.join([bin_s, *path]))
        out = cli._shell_commands(shell, env_dir, variables, off=off)
        root = str(env_dir.resolve())
    names = sorted(variables)
    entries = path if off else [bin_s, *path]
    assert "" not in entries
    scrubbed = list(cli.texenv.SCRUBBED)
    if shell is cli.ShellName.FISH:
        expected = [["set", "-gx", "PATH", *entries]]
        if off:
            expected += [["set", "-e", n] for n in [*names, "TEXI_ENV"]]
        else:
            expected += [["set", "-gx", n, variables[n]] for n in names]
            expected += [["set", "-e", n] for n in scrubbed]
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
        words += ["unset", *scrubbed, "export", f"TEXI_ENV={root}"]
    assert shlex.split(out) == words


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="activating a usertree without bin/ puts an empty entry (cwd) on PATH",
)
def test_activate_usertree_without_bin() -> None:
    with tempfile.TemporaryDirectory() as tmp, pytest.MonkeyPatch.context() as mp:
        env_dir = Path(tmp) / ".texenv"
        cli.texenv.init_usertree(env_dir)
        mp.setenv("PATH", "/usr/bin")
        out = cli._shell_commands(cli.ShellName.BASH, env_dir, {}, off=False)
    path = shlex.split(out)[1].removeprefix("PATH=")
    assert "" not in path.split(os.pathsep)
