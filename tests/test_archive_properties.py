"""Property tests of the package store, installed trees and stanza rendering."""

import os
import tempfile
from pathlib import Path

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from texi import archive, resolver, texenv
from texi import tlpdb as tl

# --- store ids and paths ------------------------------------------------------

package_names = st.from_regex(r"[a-z0-9][a-z0-9._-]{0,20}", fullmatch=True)
revisions = st.integers(0, 10**7).map(str)
checksums = st.text("0123456789abcdef", min_size=7, max_size=128)


@given(name=package_names, revision=revisions, checksum=checksums)
def test_archive_id_is_one_store_directory(
    name: str, revision: str, checksum: str
) -> None:
    ident = archive.archive_id(name, revision, checksum)
    assert ident == f"{name}-{revision}-{checksum[:7]}"
    assert Path(ident).parts == (ident,)
    assert ident != ".."


# Anything an index could put in a field, with path and URL syntax favoured.
hostile = st.one_of(
    st.text(max_size=20),
    st.text("a1./\\?#% \n\x00", max_size=12),
    st.sampled_from(["", ".", "..", "../../x", "/etc", "a/b", "1" * 300]),
)


@given(
    name=st.one_of(package_names, hostile),
    revision=st.one_of(revisions, hostile),
    checksum=st.one_of(checksums, hostile),
)
def test_archive_id_is_safe_or_refused(name: str, revision: str, checksum: str) -> None:
    try:
        ident = archive.archive_id(name, revision, checksum)
    except archive.ArchiveError:
        return
    assert Path(ident).parts == (ident,)
    assert not set(ident) & set("/\\?#% \n\x00")
    assert not name.startswith(".")
    assert revision.isascii() and revision.isdigit()
    assert set(checksum) <= set("0123456789abcdefABCDEF")


@given(name=hostile.filter(lambda n: not archive._NAME_RE.fullmatch(n)))
@example(name="../../x")
def test_an_unsafe_name_never_touches_a_tree(name: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        env = Path(tmp, "a", "b", "env")
        (env / "tlpkg" / "tlpobj").mkdir(parents=True)
        before = sorted(Path(tmp).rglob("*"))
        tree = archive.Tree(env, standalone=False)
        pkg = tl.TlPackage(name, "Collection", "1")
        entry = archive.Entry("e", name, "1", "r", "c", Path(tmp, "store"))
        for operation in (
            lambda: tree.record(pkg),
            lambda: tree.install(pkg, entry),
            lambda: tree.uninstall(name),
        ):
            with pytest.raises(archive.ArchiveError):
                operation()
        assert archive.installed_revision(env, name) is None
        assert sorted(Path(tmp).rglob("*")) == before


segments = st.sampled_from(["a", "b", "..", ".", "", "...", "..a", "a b", "~"])
relative_paths = st.one_of(
    st.lists(segments, max_size=5).map("/".join),
    st.text(st.characters(exclude_categories=("Cs",)), max_size=20),
)


@given(rel=relative_paths)
@example(rel="/etc/passwd")
@example(rel="a/../../b")
@example(rel="a//b/./c")
def test_checked_path_is_rejected_or_stays_lexically_inside(rel: str) -> None:
    env = Path("/nonexistent/env")
    try:
        path = archive._checked(env, rel)
    except archive.ArchiveError:
        parts = rel.split("/")
        assert (
            not Path(rel).parts
            or rel.startswith("/")
            or ".." in parts
            or "\x00" in rel
            or rel.splitlines() != [rel]
        )
        return
    assert ".." not in path.parts
    assert path.is_relative_to(env)
    assert path != env


# --- render_stanza ------------------------------------------------------------

long_words = st.text("abcXYZ019.,;:()'\"=-", min_size=1, max_size=100)
sentences = st.lists(long_words, max_size=30).map(" ".join)
words = st.from_regex(r"[A-Za-z0-9_./:-]{1,12}", fullmatch=True)
attr_values = st.lists(
    st.from_regex(r"[A-Za-z0-9_.-]{1,8}", fullmatch=True), max_size=3
).map(" ".join)
file_paths = st.builds(
    lambda root, parts: root + "/".join(parts),
    st.sampled_from(["RELOC/", "texmf-dist/"]),
    st.lists(
        st.from_regex(r"[A-Za-z0-9_-]{1,6}", fullmatch=True), min_size=1, max_size=3
    ),
)
tl_files = st.builds(
    tl.TlFile,
    file_paths,
    st.dictionaries(st.sampled_from(["details", "language"]), attr_values),
)
sizes = st.one_of(st.none(), st.integers(0, 10**9))
ARCH = "universal-darwin"


@st.composite
def tl_packages(draw: st.DrawFn) -> tl.TlPackage:
    pkg = tl.TlPackage(
        name=draw(package_names),
        category=draw(st.sampled_from(["Package", "TLCore", "Collection", "Scheme"])),
        revision=draw(revisions),
        shortdesc=draw(sentences),
        longdesc=draw(sentences),
        catalogue_key=draw(st.one_of(st.just(""), words)),
        depends=[
            d
            for d in draw(st.lists(package_names, max_size=3))
            if not d.startswith("00texlive.")
        ],
        fields=draw(
            st.dictionaries(
                st.sampled_from([
                    "execute",
                    "postaction",
                    "relocated",
                    "catalogue-ctan",
                    "catalogue-license",
                    "containersize",
                ]),
                st.lists(sentences.map(str.strip), min_size=1, max_size=2),
            )
        ),
    )
    if pkg.name.startswith("00texlive."):
        pkg.depends = []
    sections = ["runfiles", f"binfiles:{ARCH}", "binfiles:windows", "docfiles"]
    for sec in draw(st.lists(st.sampled_from(sections), unique=True)):
        pkg.files[sec] = draw(st.lists(tl_files, max_size=3))
        if (size := draw(sizes)) is not None:
            pkg.sizes[sec] = size
    return pkg


@settings(max_examples=75, deadline=None)
@given(pkg=tl_packages(), standalone=st.booleans())
def test_render_stanza_round_trips(pkg: tl.TlPackage, standalone: bool) -> None:
    stanza = archive.render_stanza(pkg, standalone, ARCH)
    with tl.parse_tlpdb(stanza) as db:
        assert db.names() == [pkg.name]
        got = db.get(pkg.name)
    assert got is not None
    assert (got.name, got.category, got.revision) == (
        pkg.name,
        pkg.category,
        pkg.revision,
    )
    # Trailing blanks of a line are not significant in a tlpdb.
    assert got.shortdesc == pkg.shortdesc.rstrip()
    assert got.longdesc.split() == pkg.longdesc.split()
    assert got.depends == pkg.depends
    assert got.catalogue_key == pkg.catalogue_key
    assert got.fields == {k: v for k, v in pkg.fields.items() if k != "relocated"}
    wanted: dict[str, list[tl.TlFile]] = {}
    run = pkg.files.get("runfiles", [])
    if run or pkg.category not in resolver.META_CATEGORIES:
        wanted["runfiles"] = [
            tl.TlFile(archive.target_path(f.path, standalone), f.attrs) for f in run
        ]
    if binaries := pkg.files.get(f"binfiles:{ARCH}"):
        wanted[f"binfiles:{ARCH}"] = [tl.TlFile(f.path) for f in binaries]
    assert got.files == wanted
    assert got.sizes == {k: v for k, v in pkg.sizes.items() if k in wanted}


# Every line break except "\n", which cannot be inside a parsed value.
broken_text = st.text(
    "ab -\r\x0b\x0c\x1c\x85\N{LINE SEPARATOR}\N{PARAGRAPH SEPARATOR}", max_size=90
)


@given(short=broken_text, long=broken_text, extra=broken_text)
@example(
    short="",
    long="x\N{LINE SEPARATOR}name evil",
    extra="x\N{LINE SEPARATOR}runfiles\N{LINE SEPARATOR} f",
)
def test_render_stanza_writes_no_hidden_lines(
    short: str, long: str, extra: str
) -> None:
    pkg = tl.TlPackage(
        "a",
        "Package",
        "1",
        shortdesc=short,
        longdesc=long,
        fields={"catalogue-license": [extra]},
    )
    stanza = archive.render_stanza(pkg, False)
    lines = stanza.removesuffix("\n").split("\n")
    assert stanza.splitlines() == lines
    assert [x for x in lines if x.startswith("name ")] == ["name a"]
    assert [x for x in lines if x.startswith("runfiles")] == ["runfiles"]


# --- installed trees ------------------------------------------------------------

POOL = ["alpha", "beta", "gamma"]
# Per-package files, in directories that the packages share or own.
LAYOUT = ["tex/shared/{}.sty", "tex/{}/deep/er/file.tex", "fonts/{} map"]


class TreeMachine(RuleBasedStateMachine):
    """Install, uninstall, flush and reopen a usertree or standalone tree."""

    standalone = False

    def __init__(self) -> None:
        super().__init__()
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.env = root / "env"
        self.store = root / "store"
        (self.env / "tlpkg").mkdir(parents=True)
        (self.env / "tlpkg" / "texlive.tlpdb").write_text(
            "name foreign\nrevision 1\n\n", encoding="utf-8"
        )
        self.user = self.env / "tex" / "mine" / "user.sty"
        self.user.parent.mkdir(parents=True)
        self.user.write_text("mine", encoding="utf-8")
        self.tree = self._open()
        self.model: dict[str, tuple[str, set[str]]] = {}  # name -> revision, files
        self.flushed = True

    def teardown(self) -> None:
        self.tmp.cleanup()

    def _open(self) -> archive.Tree:
        return archive.Tree(self.env, standalone=self.standalone)

    @rule(
        name=st.sampled_from(POOL),
        revision=st.integers(1, 3),
        layout=st.sets(st.sampled_from(LAYOUT)),
    )
    def install(self, name: str, revision: int, layout: set[str]) -> None:
        paths = sorted(p.format(name) for p in layout)
        entry_dir = self.store / f"{name}-{revision}"
        for rel in paths:
            src = entry_dir / name / rel
            src.parent.mkdir(parents=True, exist_ok=True)
            src.write_text(f"{name} {revision}", encoding="utf-8")
        (entry_dir / name).mkdir(parents=True, exist_ok=True)
        pkg = tl.TlPackage(
            name,
            "Package",
            str(revision),
            files={"runfiles": [tl.TlFile(f"RELOC/{p}") for p in paths]},
        )
        entry = archive.Entry(
            entry_dir.name, name, str(revision), "r", "c", entry_dir, paths
        )
        assert self.tree.install(pkg, entry) == len(paths)
        prefix = "texmf-dist/" if self.standalone else ""
        self.model[name] = (str(revision), {prefix + p for p in paths})
        self.flushed = False

    @rule(name=st.sampled_from(POOL))
    def uninstall(self, name: str) -> None:
        removed = self.tree.uninstall(name)
        _, files = self.model.pop(name, ("", set()))
        assert {p.relative_to(self.env).as_posix() for p in removed} == files
        self.flushed = False

    @rule(reopen=st.booleans())
    def flush(self, reopen: bool) -> None:
        self.tree.flush()
        self.flushed = True
        if reopen:
            self.tree = self._open()

    @invariant()
    def files_match_the_model(self) -> None:
        wanted = {f for _, files in self.model.values() for f in files}
        found = {
            p.relative_to(self.env).as_posix()
            for p in self.env.rglob("*")
            if p.is_file() and p.relative_to(self.env).parts[0] != "tlpkg"
        }
        assert found == wanted | {"tex/mine/user.sty"}
        assert self.user.read_text(encoding="utf-8") == "mine"
        for name, (revision, files) in self.model.items():
            assert archive.installed_revision(self.env, name) == int(revision)
            for rel in files:
                content = (self.env / rel).read_text(encoding="utf-8")
                assert content == f"{name} {revision}"
        for name in set(POOL) - set(self.model):
            assert archive.installed_revision(self.env, name) is None

    @invariant()
    def no_empty_directory_is_left(self) -> None:
        for path in self.env.rglob("*"):
            if path.is_dir() and path.relative_to(self.env).parts[0] != "tlpkg":
                assert any(path.iterdir()), path

    @invariant()
    def flushed_records_match_the_model(self) -> None:
        if not self.flushed:
            return
        manifest = archive.managed(self.env)
        assert {n: m["revision"] for n, m in manifest.items()} == {
            n: rev for n, (rev, _) in self.model.items()
        }
        text = (self.env / "tlpkg" / "texlive.tlpdb").read_text(encoding="utf-8")
        listed = [n for n, _ in archive._stanzas(text)]
        assert sorted(listed) == sorted(["foreign", *self.model])


class StandaloneTreeMachine(TreeMachine):
    standalone = True


_stateful = settings(max_examples=15, stateful_step_count=10, deadline=None)
TestUserTree = TreeMachine.TestCase
TestUserTree.settings = _stateful
TestStandaloneTree = StandaloneTreeMachine.TestCase
TestStandaloneTree.settings = _stateful


# --- store pruning --------------------------------------------------------------

KINDS = ["entry", "broken", "partial", "staging", "hidden", "stray"]
items = st.lists(st.tuples(st.sampled_from(KINDS), st.booleans()), max_size=6)


@settings(max_examples=40, deadline=None)
@given(spec=items, keep=st.sets(st.integers(0, 6)), dry_run=st.booleans())
def test_prune_removes_only_unused_entries_and_old_leftovers(
    spec: list[tuple[str, bool]], keep: set[int], dry_run: bool
) -> None:
    with tempfile.TemporaryDirectory() as tmp, pytest.MonkeyPatch.context() as mp:
        mp.setenv("XDG_CACHE_HOME", tmp)
        store = archive.store_dir()
        store.mkdir(parents=True)
        every: set[str] = set()
        doomed: dict[str, int] = {}
        kept_ids = {f"entry-{i}" for i in keep} | {f"broken-{i}" for i in keep}
        for i, (kind, old) in enumerate(spec):
            prefix = {"partial": ".dl-", "staging": ".tmp-", "hidden": "."}
            name = f"{prefix.get(kind, '')}{kind}-{i}"
            path = store / name
            if kind in {"partial", "stray"}:
                path.write_bytes(b"12345")
            else:
                path.mkdir()
                (path / "data").write_bytes(b"12345")
            size = 5
            if kind == "entry":
                (path / archive.META_NAME).write_bytes(b"{}")
                size += 2
            if old:
                os.utime(path, (0, 0))
            every.add(name)
            unused = kind == "entry" and name not in kept_ids
            # An old directory without metadata is incomplete, kept id or not.
            leftover = old and kind in {"broken", "partial", "staging"}
            if unused or leftover:
                doomed[name] = size
        removed = archive.prune(kept_ids, dry_run=dry_run)
        assert removed == sorted(doomed.items())
        left = {p.name for p in store.iterdir()}
        assert left == (every if dry_run else every - set(doomed))


# --- regressions --------------------------------------------------------------


def test_archive_id_cannot_leave_the_store() -> None:
    with pytest.raises(archive.ArchiveError):
        archive.archive_id("../../x", "1", "abc")


@pytest.mark.parametrize(
    ("name", "revision", "checksum"),
    [("../../x", "1", "abc"), ("a", "../1", "abc"), ("a", "1", "../../x")],
)
def test_fetch_refuses_unsafe_index_data_before_touching_anything(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    revision: str,
    checksum: str,
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    pkg = tl.TlPackage(
        name, "Package", revision, fields={"containerchecksum": [checksum]}
    )
    with pytest.raises(archive.ArchiveError):
        archive.fetch(tmp_path.as_uri(), pkg)
    with pytest.raises(archive.ArchiveError):
        archive.lookup(tmp_path.as_uri(), pkg)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("category", ["Package", "Collection"])
def test_install_packages_reports_an_unsafe_name_as_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, category: str
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    text = f"name ../../x\ncategory {category}\nrevision 1\ncontainerchecksum abc\n"
    env = tmp_path / "project" / "env"
    texenv.init_usertree(env)
    with tl.parse_tlpdb(text) as db:
        result = archive.install_packages(
            db, tmp_path.as_uri(), ["../../x"], env, notice=lambda _: None
        )
    assert result.failed == ["../../x"]
    assert not list(tmp_path.rglob("x*"))


def test_checked_rejects_the_tree_itself() -> None:
    env = Path("/nonexistent/env")
    try:
        path = archive._checked(env, ".")
    except archive.ArchiveError:
        return
    assert path != env


def test_uninstall_does_not_delete_through_a_symlinked_directory(
    tmp_path: Path,
) -> None:
    env, outside = tmp_path / "env", tmp_path / "outside"
    (env / "tlpkg" / "tlpobj").mkdir(parents=True)
    outside.mkdir()
    (outside / "f").write_text("keep", encoding="utf-8")
    (env / "link").symlink_to(outside)
    (env / "tlpkg" / "tlpobj" / "p.tlpobj").write_text(
        "name p\nrunfiles\n link/f\n", encoding="utf-8"
    )
    assert archive.Tree(env, standalone=False).uninstall("p") == []
    assert (outside / "f").exists()


def _package(tmp_path: Path, files: list[str]) -> tuple[tl.TlPackage, archive.Entry]:
    pkg = tl.TlPackage(
        "p",
        "Package",
        "1",
        files={"runfiles": [tl.TlFile(f"RELOC/{f}") for f in files]},
    )
    path = tmp_path / "entry"
    (path / "p").mkdir(parents=True)
    return pkg, archive.Entry("entry", "p", "1", "r", "c", path, files)


def test_install_does_not_write_through_a_symlinked_directory(tmp_path: Path) -> None:
    env, outside = tmp_path / "env", tmp_path / "outside"
    env.mkdir()
    outside.mkdir()
    (outside / "f").write_text("keep", encoding="utf-8")
    (env / "link").symlink_to(outside)
    pkg, entry = _package(tmp_path, ["link/f", "link/new/g"])
    for rel in ("link/f", "link/new/g"):
        (entry.package_dir / rel).parent.mkdir(parents=True, exist_ok=True)
        (entry.package_dir / rel).write_text("evil", encoding="utf-8")
    with pytest.raises(archive.ArchiveError):
        archive.Tree(env, standalone=False).install(pkg, entry)
    assert [p.name for p in outside.iterdir()] == ["f"]
    assert (outside / "f").read_text(encoding="utf-8") == "keep"


def test_install_does_not_write_through_a_symlink_it_just_placed(
    tmp_path: Path,
) -> None:
    # A usertree rewrites "../../texmf-dist/" links, so one can end up leaving the tree.
    env = tmp_path / "project" / "env"
    env.mkdir(parents=True)
    pkg, entry = _package(tmp_path, ["bin/arch/out", "bin/arch/out/victim"])
    (entry.package_dir / "bin" / "arch").mkdir(parents=True)
    link = entry.package_dir / "bin" / "arch" / "out"
    # In the store the link leads inside the package; in the tree, above it.
    link.symlink_to("../../texmf-dist/../esc")
    for sub in ("texmf-dist", "esc"):
        (entry.package_dir / sub).mkdir()
    (entry.package_dir / "esc" / "victim").write_text("evil", encoding="utf-8")
    (tmp_path / "project" / "esc").mkdir()
    with pytest.raises(archive.ArchiveError, match="through a symlink"):
        archive.Tree(env, standalone=False).install(pkg, entry)
    assert not (tmp_path / "project" / "esc" / "victim").exists()
    assert not (env / "bin").exists()


def test_tlpobj_values_cannot_add_files_to_delete(tmp_path: Path) -> None:
    env = tmp_path / "env"
    (env / "tlpkg" / "tlpobj").mkdir(parents=True)
    (env / "mine").write_text("keep", encoding="utf-8")
    for brk in ("\N{LINE SEPARATOR}", "\r"):
        (env / "tlpkg" / "tlpobj" / "p.tlpobj").write_bytes(
            f"name p\nlongdesc x{brk}runfiles{brk} mine\n".encode()
        )
        assert archive.Tree(env, standalone=False).uninstall("p") == []
        assert (env / "mine").exists()


def _round_trip(pkg: tl.TlPackage) -> tl.TlPackage:
    with tl.parse_tlpdb(archive.render_stanza(pkg, False)) as db:
        got = db.get(pkg.name)
    assert got is not None
    return got


def test_render_stanza_keeps_a_long_word_whole() -> None:
    pkg = tl.TlPackage("a", "Package", "1", longdesc="x" * 71)
    assert _round_trip(pkg).longdesc == pkg.longdesc


def test_render_stanza_keeps_a_hyphenated_word_whole() -> None:
    pkg = tl.TlPackage("a", "Package", "1", longdesc="x" * 60 + " abcde-fghij-klmno")
    assert _round_trip(pkg).longdesc == pkg.longdesc


def test_render_stanza_keeps_an_empty_file_attribute() -> None:
    with tl.parse_tlpdb('name a\nrunfiles\n RELOC/f details=""\n') as db:
        pkg = db.get("a")
    assert pkg is not None
    assert _round_trip(pkg).files["runfiles"][0].path == "f"
