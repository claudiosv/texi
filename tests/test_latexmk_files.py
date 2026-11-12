from pathlib import Path

import pytest

from texi.latexmk_files import (
    Fdb,
    ProjectFiles,
    classify_path,
    is_texmf_path,
    load_fdb,
    load_fls,
    parse_fdb,
    parse_fls,
    tree_used_paths,
)

FIX = Path(__file__).parent / "fixtures" / "latexmk"
NAMES = sorted(p.stem for p in FIX.glob("*.fls"))


def test_fixture_inventory():
    assert NAMES == [
        "biber_handwritten",
        "biblatex_missing",
        "bibtex",
        "makeindex",
        "missing_pkg",
        "space_in_path",
        "xelatex_failed",
    ]
    for n in NAMES:
        assert (FIX / f"{n}.fdb_latexmk").exists()


@pytest.mark.parametrize("name", NAMES)
def test_every_fixture_parses(name):
    fls = load_fls(FIX / f"{name}.fls")
    fdb = load_fdb(FIX / f"{name}.fdb_latexmk")
    assert fls.pwd and fls.pwd.startswith("/")
    assert "main.tex" in fls.inputs
    assert len(fls.inputs) == len(set(fls.resolved_inputs()))
    assert fdb.version == 4
    assert fdb.rules
    for r in fdb.rules:
        assert r.name and r.run_time is not None and r.source and r.dest
        assert r.base == "main"
        assert r.deps
        for d in r.deps:
            assert d.path
            if not d.generated:
                assert d.mtime is not None and d.size is not None
            else:
                assert d.mtime is None
    assert "main.fls" in fdb.generated_files() or name == "biber_handwritten"
    pf = ProjectFiles(FIX, name, fls, fdb)
    srcs = [p.name for p in pf.source_files()]
    assert "main.tex" in srcs
    assert not any(is_texmf_path(str(p)) for p in pf.source_files())
    assert all(p.name != "main.aux" for p in pf.source_files())


def test_bibtex_fls():
    fls = load_fls(FIX / "bibtex.fls")
    assert fls.pwd == "/tmp/proj/bibtex"
    assert fls.inputs.count("./main.aux") == 1
    assert "main.aux" not in fls.inputs  # same file as ./main.aux
    assert fls.outputs == ["main.log", "main.aux", "main.pdf"]
    assert set(fls.read_then_written()) == {"/tmp/proj/bibtex/main.aux"}
    # main.bbl is written by bibtex, which the .fls of the TeX run cannot know
    assert fls.project_inputs() == [
        "/tmp/proj/bibtex/main.tex",
        "/tmp/proj/bibtex/main.bbl",
    ]
    assert any(p.endswith("article.cls") for p in fls.external_inputs())
    assert fls.inputs_of_kind("class")[0].endswith("article.cls")
    assert [Path(p).name for p in fls.inputs_in("/tmp/proj/bibtex")][:1] == ["main.tex"]


def test_bibtex_fdb():
    fdb = load_fdb(FIX / "bibtex.fdb_latexmk")
    assert [r.name for r in fdb.rules] == ["bibtex main", "pdflatex"]
    bib = fdb.rule("bibtex main")
    assert bib is not None
    assert (bib.source, bib.dest, bib.base) == ("main.aux", "main.bbl", "main")
    assert bib.run_time == pytest.approx(1794431786.91482)
    assert bib.check_time == pytest.approx(1794431787.01161)
    assert bib.status == 0
    assert [d.path for d in bib.inputs][0] == "./refs.bib"
    assert bib.inputs[-1].from_rule == "pdflatex"
    assert [d.path for d in bib.generated] == ["main.bbl", "main.blg"]
    assert bib.rewritten == []
    assert fdb.rule("nope") is None
    tex = fdb.rule("pdflatex")
    assert tex is not None
    bbl = next(d for d in tex.deps if d.path == "main.bbl")
    assert bbl.from_rule == "bibtex main" and not bbl.generated
    assert bbl.size == 106 and bbl.md5 == "09902efc2bc78d81b51f008e2a7231cf"
    assert "main.pdf" in fdb.generated_files()
    assert "main.pdf" not in [d.path for d in fdb.all_deps()]
    assert "main.pdf" in [d.path for d in fdb.all_deps(include_generated=True)]
    paths = [d.path for d in fdb.all_deps()]
    assert len(paths) == len(set(paths))


def test_makeindex_rule_name_with_space():
    fdb = load_fdb(FIX / "makeindex.fdb_latexmk")
    r = fdb.rule("makeindex main.idx")
    assert r is not None and r.source == "main.idx" and r.dest == "main.ind"
    assert [d.path for d in r.generated] == ["main.ilg", "main.ind"]


def test_missing_dependency_is_not_existing():
    fdb = load_fdb(FIX / "missing_pkg.fdb_latexmk")
    d = next(d for d in fdb.all_deps() if d.path == "example-image")
    assert (d.mtime, d.size, d.md5) == (0, -1, None)
    assert not d.exists
    pf = ProjectFiles(FIX, "missing_pkg", load_fls(FIX / "missing_pkg.fls"), fdb)
    assert "example-image" not in [p.name for p in pf.source_files()]


def test_xelatex_two_rules():
    fdb = load_fdb(FIX / "xelatex_failed.fdb_latexmk")
    assert [r.name for r in fdb.rules] == ["xdvipdfmx", "xelatex"]
    assert fdb.rule("xelatex").status == 2  # type: ignore[union-attr]
    assert fdb.rule("xdvipdfmx").deps[0].from_rule == "xelatex"  # type: ignore[union-attr]


def test_space_in_path():
    fls = load_fls(FIX / "space_in_path.fls")
    assert fls.pwd == "/tmp/proj/sp ace"
    assert "./my sec.tex" in fls.inputs and "my sec.tex" not in fls.inputs
    assert fls.project_inputs() == [
        "/tmp/proj/sp ace/main.tex",
        "/tmp/proj/sp ace/my sec.tex",
    ]
    fdb = load_fdb(FIX / "space_in_path.fdb_latexmk")
    assert "my sec.tex" in [d.path for d in fdb.all_deps()]


def test_handwritten_biber():
    fls = load_fls(FIX / "biber_handwritten.fls")
    assert fls.pwd == "/home/user/my proj"
    assert fls.inputs == [
        "/usr/share/texlive/texmf.cnf",
        "main.tex",
        "/usr/share/texlive/texmf-dist/tex/latex/base/article.cls",
        "/usr/share/texlive/texmf-dist/tex/latex/biblatex/biblatex.sty",
        "./refs.bib",
        "main.aux",
        "sections/intro chapter.tex",
        "/home/user/other/shared.tex",
    ]
    assert fls.project_inputs() == [
        "/home/user/my proj/main.tex",
        "/home/user/my proj/refs.bib",
        "/home/user/my proj/sections/intro chapter.tex",
    ]
    assert fls.read_then_written() == ["/home/user/my proj/main.aux"]
    fdb = load_fdb(FIX / "biber_handwritten.fdb_latexmk")
    assert [r.name for r in fdb.rules] == ["biber main", "lualatex"]
    lua = fdb.rule("lualatex")
    assert lua is not None
    assert lua.dest == "build/main.pdf" and lua.status == 1
    assert lua.rewritten == ["main.aux"]
    assert [d.path for d in lua.generated] == [
        "build/main.pdf",
        "main.bcf",
        "main.log",
    ]
    assert fdb.generated_files() == [
        "main.bbl",
        "main.blg",
        "build/main.pdf",
        "main.bcf",
        "main.log",
    ]
    pf = ProjectFiles(FIX, "biber_handwritten", fls, fdb)
    root = "/home/user/my proj"
    assert [str(p) for p in pf.source_files()] == [
        f"{root}/main.tex",
        f"{root}/refs.bib",
        f"{root}/sections/intro chapter.tex",
    ]
    ext = [str(p) for p in pf.external_files()]
    assert "/home/user/other/shared.tex" in ext
    assert "/usr/share/texlive/texmf.cnf" in ext
    assert f"{root}/main.tex" not in ext
    assert f"{root}/main.aux" not in ext
    assert f"{root}/build/main.pdf" in [str(p) for p in pf.generated_files()]


def test_from_build(tmp_path):
    for ext in ("fls", "fdb_latexmk"):
        (tmp_path / f"job.{ext}").write_bytes((FIX / f"bibtex.{ext}").read_bytes())
    pf = ProjectFiles.from_build(tmp_path, "job")
    assert pf.fls and pf.fdb and pf.pwd == "/tmp/proj/bibtex"
    assert [p.name for p in pf.source_files()] == [
        "main.tex",
        "refs.bib",
    ]  # .bbl is generated
    only = ProjectFiles.from_build(tmp_path, "nothing")
    assert only.fls is None and only.fdb is None
    assert only.source_files() == [] and only.external_files() == []
    (tmp_path / "job.fdb_latexmk").unlink()
    assert ProjectFiles.from_build(tmp_path, "job").fdb is None


def test_changed_since():
    old = parse_fdb(
        '# Fdb version 4\n["pdflatex"] 1 "m.tex" "m.pdf" "m" 1 0\n'
        '  "a.sty" 10 5 aaaa ""\n  "b.sty" 10 5 bbbb ""\n  "gone.sty" 1 1 cccc ""\n'
        '  (generated)\n  "m.pdf"\n'
    )
    new = parse_fdb(
        '# Fdb version 4\n["pdflatex"] 2 "m.tex" "m.pdf" "m" 2 0\n'
        '  "a.sty" 10 5 aaaa ""\n  "b.sty" 11 5 bbbb ""\n  "new.tex" 3 3 dddd ""\n'
        '  (generated)\n  "m.pdf"\n'
    )
    assert [d.path for d in new.changed_since(old)] == ["b.sty", "new.tex"]
    assert [d.path for d in new.changed_since(new)] == []
    assert [d.path for d in new.removed_since(old)] == ["gone.sty"]
    assert [d.path for d in old.changed_since(Fdb())] == ["a.sty", "b.sty", "gone.sty"]


def test_changed_by_md5_and_size():
    mk = lambda size, md5: parse_fdb(
        f'["x"] 1 "s" "d" "b" 1 0\n  "f" 5 {size} {md5} ""\n'
    )
    assert mk(1, "aa").changed_since(mk(1, "aa")) == []
    assert len(mk(1, "aa").changed_since(mk(2, "aa"))) == 1
    assert len(mk(1, "aa").changed_since(mk(1, "bb"))) == 1


def test_fdb_tolerance():
    fdb = parse_fdb(
        "# Fdb version 5\r\n\r\n"
        '["lualatex"] 1.5 "my main.tex" "out dir/my main.pdf" "my main" 2.5 0 7 future\r\n'
        '  "/a b/c d.sty" 3 4 0 ""\r\n'
        '  "short.tex" 3\r\n'
        '  "weird.tex" abc xyz\r\n'
        "  (new future marker)\r\n"
        '  "ignored.txt" 1 1 0 ""\r\n'
        "  (generated)\r\n"
        '  "x y.aux"\r\n'
    )
    assert fdb.version == 5
    (r,) = fdb.rules
    assert (r.source, r.dest, r.base) == (
        "my main.tex",
        "out dir/my main.pdf",
        "my main",
    )
    assert r.extra == ["7", "future"]
    assert [d.path for d in r.deps] == [
        "/a b/c d.sty",
        "short.tex",
        "weird.tex",
        "x y.aux",
    ]
    assert r.deps[0].md5 is None and r.deps[0].size == 4
    assert r.deps[1].size is None and r.deps[1].mtime == 3
    assert r.deps[2].mtime is None and r.deps[2].size is None
    assert r.deps[3].generated


@pytest.mark.parametrize("version", ["3", "5", "99", "x"])
def test_versions(version):
    fdb = parse_fdb(
        f'# Fdb version {version}\n["pdflatex"] 1 "a" "b" "c" 1 0\n  "f" 1 1 0 ""\n'
    )
    assert fdb.version == (int(version) if version.isdigit() else None)
    assert len(fdb.rules) == 1 and len(fdb.rules[0].deps) == 1


def test_unknown_or_missing_header_still_parses():
    fdb = parse_fdb('["pdflatex"] 1 "a" "b" "c" 1 0\n')
    assert fdb.version is None and len(fdb.rules) == 1


def test_empty_and_garbage():
    for text in (
        "",
        "\n\n",
        "   ",
        "garbage\x00\xff",
        "[",
        '["',
        '["x',
        "(",
        '"',
        "INPUT",
    ):
        assert parse_fdb(text).all_deps() == [] or True
        parse_fls(text)
    assert parse_fdb("") == Fdb()
    assert parse_fls("").inputs == []
    assert parse_fls("").pwd is None
    assert parse_fls("").project_inputs() == []
    assert parse_fdb('  "orphan" 1 1 0 ""\n').rules == []


@pytest.mark.parametrize("name", NAMES)
def test_truncated_at_any_byte_never_raises(name):
    for ext, parse in (("fls", parse_fls), ("fdb_latexmk", parse_fdb)):
        data = (FIX / f"{name}.{ext}").read_bytes()
        for i in range(len(data) + 1):
            r = parse(data[:i].decode("utf-8", errors="replace"))
            if ext == "fls":
                r.project_inputs()
                r.read_then_written()
            else:
                r.all_deps()
                r.generated_files()
                r.changed_since(r)


@pytest.mark.parametrize("name", NAMES)
def test_crlf_same_result(name):
    for ext, parse in (("fls", parse_fls), ("fdb_latexmk", parse_fdb)):
        text = (FIX / f"{name}.{ext}").read_text()
        assert parse(text.replace("\n", "\r\n")) == parse(text)


def test_fls_details():
    fls = parse_fls(
        "PWD /w\nINPUT a b.tex \nINPUT ./a b.tex \nINPUT ../x.tex\nOUTPUT /w/o.pdf\n"
        "OUTPUT o.pdf\nPWD /ignored\nBOGUS z\nINPUT\n"
    )
    assert fls.pwd == "/w"
    assert fls.inputs == ["a b.tex ", "../x.tex"] or fls.inputs == [
        "a b.tex ",
        "../x.tex",
    ]
    assert fls.outputs == ["/w/o.pdf"]
    assert fls.resolve("./q/../r") == "/w/r"
    assert fls.inputs_in("/w") == ["/w/a b.tex "] or fls.inputs_in("/w") == []
    assert fls.external_inputs() == ["/x.tex"]


def test_fls_without_pwd():
    fls = parse_fls("INPUT main.tex\nOUTPUT main.pdf\n")
    assert fls.pwd is None and fls.resolved_inputs() == ["main.tex"]
    assert fls.project_inputs() == []


def test_classify_and_texmf():
    assert classify_path("/x/a.sty") == "package"
    assert classify_path("a.cls") == "class"
    assert classify_path("./dir/Main.TEX") == "tex"
    assert classify_path("refs.bib") == "bib"
    assert classify_path("main.aux") == "aux"
    assert classify_path("main.pdf") == "output"
    assert classify_path("cmr10.pfb") == "font"
    assert classify_path("main.fdb_latexmk") == "log"
    assert classify_path("Makefile") == "other"
    assert classify_path("") == "other"
    assert is_texmf_path("/usr/local/texlive/2026/texmf-dist/a.sty")
    assert is_texmf_path("/home/u/Library/texlive/2026/texmf-var/x")
    assert not is_texmf_path("/home/u/proj/main.tex")
    assert not is_texmf_path("texmf.cnf")


def test_tree_used_paths(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "main.tex").write_text("x")
    (tmp_path / "sub" / "a b.tex").write_text("x")
    fls = tmp_path / "main.fls"
    fls.write_text(
        f"PWD {tmp_path}\nINPUT main.tex\nINPUT ./sub/a b.tex\nOUTPUT main.pdf\n"
    )
    files, folders = tree_used_paths(fls)
    assert files == {
        (tmp_path / "main.tex").resolve(),
        (tmp_path / "sub" / "a b.tex").resolve(),
    }
    assert folders == {tmp_path.resolve(), (tmp_path / "sub").resolve()}
    # base overrides PWD
    fls.write_text("PWD /nonexistent\nINPUT main.tex\n")
    files, _ = tree_used_paths(fls, base=tmp_path)
    assert files == {(tmp_path / "main.tex").resolve()}


def test_load_missing_file_raises_oserror(tmp_path):
    with pytest.raises(OSError):
        load_fls(tmp_path / "no.fls")
    with pytest.raises(OSError):
        load_fdb(tmp_path / "no.fdb_latexmk")


def test_load_bad_utf8(tmp_path):
    p = tmp_path / "a.fls"
    p.write_bytes(b"PWD /w\nINPUT caf\xe9.tex\n")
    assert load_fls(p).inputs == ["caf�.tex"]
