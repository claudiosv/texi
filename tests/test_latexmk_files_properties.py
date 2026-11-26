"""Property tests of the .fls and .fdb_latexmk parsers (no disk access)."""

import os.path
from pathlib import Path

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from texi import latexmk_files as lf

# What ends a line. A path may hold anything else, including the other characters
# str.splitlines() breaks on ("\r" is left out as it belongs to a "\r\n" ending).
BREAKS = "\n\r"

fragments = st.sampled_from([
    "PWD ",
    "INPUT ",
    "OUTPUT ",
    "# Fdb version ",
    '["',
    '"]',
    '"',
    "(generated)",
    "(rewritten before read)",
    "(source)",
    " ",
    "\n",
    "\r\n",
    "/",
    ".",
    "..",
    "0",
    "-1",
    "nan",
    "inf",
    "1e999",
    chr(0x2028),
    chr(12),
    "9" * 5000,  # more digits than int() accepts
])
garbage = st.lists(st.one_of(fragments, st.text(max_size=8)), max_size=40).map("".join)


@given(garbage)
def test_parse_fls_never_raises(text: str) -> None:
    fls = lf.parse_fls(text)
    fls.resolved_inputs()
    fls.project_inputs()
    fls.external_inputs()
    keys = [fls.resolve(p) for p in fls.inputs]
    assert all(keys)


@given(garbage)
def test_parse_fdb_never_raises(text: str) -> None:
    fdb = lf.parse_fdb(text)
    fdb.all_deps(include_generated=True)
    fdb.generated_files()
    assert fdb.changed_since(fdb) == [
        d
        for d in fdb.all_deps()
        if d.mtime != d.mtime  # NaN differs from itself
    ]
    assert fdb.removed_since(fdb) == []


segments = st.one_of(
    st.sampled_from([
        ".",
        "..",
        "a",
        "b.tex",
        "texmf-dist",
        "x y",
        f"a{chr(12)}b",
        f"c{chr(0x2028)}d",
    ]),
    st.text(
        st.characters(exclude_characters=BREAKS + "/", exclude_categories=["Cs"]),
        min_size=1,
        max_size=6,
    ),
)
paths = st.builds(
    lambda root, parts: root + "/".join(parts),
    st.sampled_from(["", "/", "/p/", "./"]),
    st.lists(segments, min_size=1, max_size=4),
)
records = st.lists(st.tuples(st.sampled_from(["INPUT", "OUTPUT"]), paths), max_size=10)


@given(
    pwd=st.one_of(st.none(), st.just("/p"), paths),
    rows=records,
    eol=st.sampled_from(["\n", "\r\n"]),
)
def test_fls_round_trip(pwd: str | None, rows: list[tuple[str, str]], eol: str) -> None:
    lines = [] if pwd is None else [f"PWD {pwd}"]
    lines += [f"{k} {p}" for k, p in rows]
    fls = lf.parse_fls(eol.join(lines))

    def first_seen(kind: str) -> list[str]:
        out: dict[str, str] = {}
        for k, p in rows:
            if k == kind:
                full = p if p.startswith("/") or pwd is None else os.path.join(pwd, p)
                out.setdefault(os.path.normpath(full), p)
        return list(out.values())

    assert fls.pwd == pwd
    assert fls.inputs == first_seen("INPUT")
    assert fls.outputs == first_seen("OUTPUT")


quoted = st.text(
    st.characters(exclude_characters=BREAKS + '"', exclude_categories=["Cs"]),
    max_size=10,
)
numbers = st.integers(0, 2**40)
deps = st.builds(
    lf.Dep,
    path=quoted,
    mtime=numbers.map(float),
    size=st.integers(-1, 2**40),
    md5=st.one_of(st.none(), st.text("0123456789abcdef", min_size=32, max_size=32)),
    from_rule=quoted,
    generated=st.just(False),
)
generated = deps.map(
    lambda d: lf.Dep(d.path, d.mtime, d.size, d.md5, d.from_rule, True)
)
rules = st.builds(
    lambda name, t, src, dst, base, check, status, plain, gen, again: lf.Rule(
        name, float(t), src, dst, base, float(check), status, [], plain + gen, again
    ),
    quoted,
    numbers,
    quoted,
    quoted,
    quoted,
    numbers,
    st.integers(0, 255),
    st.lists(deps, max_size=4),
    st.lists(generated, max_size=3),
    st.lists(quoted, max_size=3),
)


def _dep_line(d: lf.Dep) -> str:
    return f'  "{d.path}" {int(d.mtime or 0)} {d.size} {d.md5 or 0} "{d.from_rule}"'


def _render(version: int, blocks: list[lf.Rule]) -> str:
    out = [f"# Fdb version {version}"]
    for r in blocks:
        out.append(
            f'["{r.name}"] {int(r.run_time or 0)} "{r.source}" "{r.dest}" '
            f'"{r.base}" {int(r.check_time or 0)} {r.status}'
        )
        out += [_dep_line(d) for d in r.inputs]
        out.append("  (generated)")
        out += [_dep_line(d) for d in r.generated]
        out.append("  (rewritten before read)")
        out += [f'  "{p}"' for p in r.rewritten]
    return "\n".join(out) + "\n"


@given(version=st.integers(0, 10**9), blocks=st.lists(rules, max_size=4))
def test_fdb_round_trip(version: int, blocks: list[lf.Rule]) -> None:
    assert lf.parse_fdb(_render(version, blocks)) == lf.Fdb(version, blocks)


# Few names, so that inputs, outputs and both files often name the same file.
parts = st.lists(
    st.sampled_from(["a", "b.tex", "..", "texmf-dist", "sub"]), min_size=1, max_size=3
)
used = st.builds(
    lambda root, p: root + "/".join(p), st.sampled_from(["", "/p/", "/other/"]), parts
)


@settings(max_examples=60)
@given(
    inputs=st.lists(used, max_size=6),
    outputs=st.lists(used, max_size=3),
    fdb_inputs=st.lists(used, max_size=4),
    fdb_outputs=st.lists(used, max_size=3),
    root=st.sampled_from([None, "/p", "/p/sub", "sub", "/"]),
    with_fls=st.booleans(),
)
def test_source_and_external_files_partition_what_was_read(
    inputs: list[str],
    outputs: list[str],
    fdb_inputs: list[str],
    fdb_outputs: list[str],
    root: str | None,
    with_fls: bool,
) -> None:
    # A relative root needs the PWD of a .fls to mean something under /p.
    assume(with_fls or root != "sub")
    fls = lf.Fls("/p", inputs, outputs) if with_fls else None
    rule = lf.Rule(
        "pdflatex",
        deps=[lf.Dep(p) for p in fdb_inputs]
        + [lf.Dep(p, generated=True) for p in fdb_outputs],
    )
    pf = lf.ProjectFiles(Path("/p/build"), "main", fls, lf.Fdb(4, [rule]))
    source, external = pf.source_files(root), pf.external_files(root)
    assert not set(source) & set(external)
    # Without a PWD, relative paths are relative to the root (default: aux_dir).
    default = "/p" if with_fls else os.fspath(Path("/p/build").resolve())
    base = Path(os.path.normpath(os.path.join(default, root or default)))
    cwd = "/p" if with_fls else os.fspath(base)

    def absolute(files: list[Path]) -> set[Path]:
        return {Path(os.path.normpath(os.path.join(cwd, f))) for f in files}

    assert set(source) | set(external) == absolute(pf.all_inputs()) - absolute(
        pf.generated_files()
    )
    for path in source:
        assert path.is_relative_to(base)
        assert not lf.is_texmf_path(str(path))
    for path in external:
        assert not path.is_relative_to(base) or lf.is_texmf_path(str(path))


@given(inputs=st.lists(used, max_size=6), outputs=st.lists(used, max_size=4))
def test_fls_views_are_consistent(inputs: list[str], outputs: list[str]) -> None:
    fls = lf.Fls("/p", inputs, outputs)
    everything = fls.resolved_inputs()
    inside, outside = fls.inputs_in("/p"), fls.external_inputs()
    assert sorted(inside + outside) == sorted(everything)
    assert set(fls.project_inputs()) <= set(inside)
    assert not set(fls.project_inputs()) & set(fls.resolved_outputs())
    assert set(fls.read_then_written()) == set(everything) & set(fls.resolved_outputs())


def test_fdb_huge_version_number_does_not_raise() -> None:
    lf.parse_fdb("# Fdb version " + "9" * 5000)


separators = pytest.mark.parametrize(
    "char", [chr(12), chr(0x2028)], ids=["form-feed", "line-separator"]
)


@separators
def test_fls_path_keeps_unicode_line_breaks(char: str) -> None:
    assert lf.parse_fls(f"INPUT a{char}b.tex\n").inputs == [f"a{char}b.tex"]


@separators
def test_fdb_path_keeps_unicode_line_breaks(char: str) -> None:
    fdb = lf.parse_fdb(f'["pdflatex"] 1\n  "a{char}b.tex" 1 1 0 ""\n')
    assert [d.path for d in fdb.rules[0].deps] == [f"a{char}b.tex"]


def test_source_files_without_fls_keeps_relative_deps() -> None:
    fdb = lf.parse_fdb('["pdflatex"] 1\n  "main.tex" 1 1 abc ""\n')
    pf = lf.ProjectFiles(Path("/p"), "main", fls=None, fdb=fdb)
    assert [p.name for p in pf.source_files("/p")] == ["main.tex"]
    assert pf.external_files("/p") == []


def test_relative_root_without_fls() -> None:
    fdb = lf.parse_fdb('["pdflatex"] 1\n  "main.tex" 1 1 abc ""\n')
    pf = lf.ProjectFiles(Path("/p"), "main", fls=None, fdb=fdb)
    assert pf.source_files(".") == [Path.cwd() / "main.tex"]
    assert pf.external_files(".") == []
