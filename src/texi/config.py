from enum import Enum
from typing import Literal
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator


class PdfMode(str, Enum):
    """latexmk's $pdf_mode: how the PDF is produced.

    off      (0) no PDF
    pdflatex (1) pdflatex straight to PDF
    ps2pdf   (2) latex -> dvips -> ps2pdf (PostScript tooling, e.g. pstricks)
    dvi2pdf  (3) latex -> dvipdf(m)x
    lualatex (4) lualatex straight to PDF (best Unicode/OpenType font support)
    xelatex  (5) xelatex via xdvipdfmx (Unicode/OpenType, system fonts)
    """

    OFF = "off"
    PDFLATEX = "pdflatex"
    PS2PDF = "ps2pdf"
    DVI2PDF = "dvi2pdf"
    LUALATEX = "lualatex"
    XELATEX = "xelatex"


class BibtexUse(str, Enum):
    NEVER = "never"
    BIB_EXISTS = "bib_exists"
    EVEN_WITHOUT_BIB = "even_without_bib"
    ALWAYS = "always"


class ViewerUpdate(str, Enum):
    """How latexmk gets a PDF/DVI/PS viewer to show a rebuilt file."""

    AUTO = "auto"  # the viewer watches the file itself
    MANUAL = "manual"  # you refresh the viewer yourself
    SIGNAL = "signal"  # latexmk sends the viewer a signal
    NONE = "none"  # viewer locks the file, so it can't be updated
    COMMAND = "command"  # latexmk runs the *_update_command


class GoMode(str, Enum):
    """How much latexmk redoes (latexmk's $go_mode)."""

    REBUILD = "rebuild"  # only what changed
    FULL_REBUILD = "full-rebuild"  # everything, regardless of timestamps (-g)
    CLEAN = "clean"  # delete generated files first, then build (-gg)
    FORCE_MAIN = "force-main"  # rerun just the main compile, then continue (-gt)


class LatexmkEngines(BaseModel):
    pdf_mode: PdfMode = PdfMode.PDFLATEX
    dvi_mode: bool = False
    postscript_mode: bool = False


class SubdirCreation(str, Enum):
    """Whether latexmk may create subdirectories of aux_dir (latexmk's $allow_subdir_creation)."""

    NONE = "none"
    AUX = "aux"  # only for aux files, e.g. \\include{chapters/chap}
    ALL = "all"


class LatexmkDirectories(BaseModel):
    out_dir: str = "build/"
    aux_dir: str = "build/aux/"
    # Final output files (see out2_exts) are copied here; out_dir keeps intermediates
    out2_dir: str | None = None
    out2_exts: list[str] = Field(default_factory=list)
    # Emulate aux_dir by moving files out of out_dir (for TeX engines without --aux-directory)
    emulate_aux: bool | None = None
    allow_subdir_creation: SubdirCreation | None = None

    @field_validator("out_dir", "aux_dir", "out2_dir")
    @classmethod
    def ensure_trailing_slash(cls, v: str | None) -> str | None:
        if v and not v.endswith("/"):
            return v + "/"
        return v


class LatexmkPrograms(BaseModel):
    """Override the shell commands latexmk uses for each tool.

    Use ``%O`` for latexmk-supplied options and ``%S`` for the source file.
    Example: ``pdflatex = "pdflatex -shell-escape %O %S"``
    """

    pdflatex: str | None = None
    lualatex: str | None = None
    xelatex: str | None = None
    latex: str | None = None
    bibtex: str | None = None
    biber: str | None = None
    makeindex: str | None = None
    makeglossaries: str | None = None
    dvips: str | None = None
    ps2pdf: str | None = None
    dvipdf: str | None = None
    gs: str | None = None
    # Extra switches appended to engine commands in silent/quiet mode
    pdflatex_silent_switch: str | None = None
    lualatex_silent_switch: str | None = None
    xelatex_silent_switch: str | None = None
    latex_silent_switch: str | None = None
    bibtex_silent_switch: str | None = None
    biber_silent_switch: str | None = None


class LatexmkExecution(BaseModel):
    halt_on_error: bool = True
    # Quiet latexmk and TeX engines: only errors and the final status are shown
    silent: bool = False
    # Print latexmk's internal decisions (dependency checks, rule resolution)
    diagnostics: bool = False
    go_mode: GoMode = GoMode.REBUILD
    # -f: keep processing past errors (rc variable $force_mode)
    force_mode: bool = False
    bibtex_use: BibtexUse = BibtexUse.ALWAYS
    max_repeat: int = Field(default=5, ge=1, le=20)
    # Continuous preview mode (-pvc) and one-shot preview (-p)
    preview_continuous_mode: bool = False
    preview_mode: bool = False
    # Seconds to sleep between filesystem checks in continuous mode
    sleep_time: int = Field(default=2, ge=0)
    # Pass -recorder to the TeX engine (produces .fls dependency file)
    recorder: bool = True
    warnings_as_errors: bool = False
    # Inject \pdfminorversion=5 \pdfcompresslevel=9 \pdfobjcompresslevel=3
    # before the document is read, to shrink pdflatex output
    optimize_pdf: bool = False


DEFAULT_CLEAN_EXT = [
    "synctex.gz",
    "synctex.gz(busy)",
    "run.xml",
    "tex.bak",
    "bbl",
    "bcf",
    "fdb_latexmk",
    "fls",
    "lof",
    "lot",
    "out",
    "toc",
    "nav",
    "snm",
    "vrb",
]


class LatexmkFiles(BaseModel):
    # Top-level .tex files to compile; latexmk's @default_files
    default_files: list[str] = Field(default_factory=list)
    # Output name (latexmk's $jobname); defaults to `name`, else the main file's name
    jobname: str | None = None
    # Extra extensions removed by `latexmk -c`
    clean_ext: list[str] = Field(default_factory=lambda: list(DEFAULT_CLEAN_EXT))
    # Extra extensions removed by `latexmk -C` (full clean)
    clean_full_ext: list[str] = Field(default_factory=list)
    # Extensions latexmk treats as generated (triggers rerun detection)
    generated_exts: list[str] = Field(default_factory=list)


class LatexmkViewers(BaseModel):
    pdf_previewer: str | None = None
    dvi_previewer: str | None = None
    ps_previewer: str | None = None
    # How the viewer is signalled when the output file changes
    pdf_update_method: ViewerUpdate | None = None
    dvi_update_method: ViewerUpdate | None = None
    ps_update_method: ViewerUpdate | None = None
    # Command run when update_method == "command"
    pdf_update_command: str | None = None
    dvi_update_command: str | None = None
    ps_update_command: str | None = None
    # Signal number used when update_method == "signal" (default: SIGHUP = 1)
    pdf_update_signal: int | None = Field(default=None, ge=1, le=64)
    dvi_update_signal: int | None = Field(default=None, ge=1, le=64)
    ps_update_signal: int | None = Field(default=None, ge=1, le=64)


class BuildConfig(
    LatexmkEngines,
    LatexmkDirectories,
    LatexmkPrograms,
    LatexmkExecution,
    LatexmkFiles,
    LatexmkViewers,
):
    """Everything under `[build]`: one flat table of latexmk settings."""

    model_config = ConfigDict(extra="forbid")

    # Added to every TeX engine command (pdflatex, lualatex, xelatex, latex)
    synctex: bool = True
    shell_escape: bool = False
    # Report errors as file:line:message, which editors and CI can parse
    file_line_error: bool = False
    extra_args: list[str] = Field(default_factory=list)
    # Extra environment variables for the build, e.g. [build.env]
    env: dict[str, str] = Field(default_factory=dict)
    # Extra directories (relative to the project root) searched for .bib / TeX files
    bib_dirs: list[str] = Field(default_factory=list)
    tex_inputs: list[str] = Field(default_factory=list)


class ProjectConfig(BaseModel):
    # Project name; used as the latexmk jobname unless build.jobname is set
    name: str | None = None
    # Main .tex file, relative to src_dir
    main: str = "main.tex"
    # Expected TeX Live year (e.g. "2026"); texi warns if the installed one differs
    texlive: str | None = None
    # tlmgr repository to install from; point at a frozen tlnet to truly pin versions
    repository: str | None = None
    # Directory holding the .tex sources; "." = project root. latexmk runs with -cd.
    src_dir: str = "src"
    dependencies: list[str] = Field(default_factory=list)


class TexFmtConfig(BaseModel):
    """Settings for `texi fmt`, written to a generated tex-fmt config file."""

    wrap: bool = True
    wraplen: int = Field(default=80, ge=1)
    wrapmin: int | None = Field(default=None, ge=1)
    tabsize: int = Field(default=2, ge=1)
    tabchar: Literal["space", "tab"] = "space"
    # Extend tex-fmt's built-in lists (they are added to, not replaced)
    lists: list[str] = Field(default_factory=list)
    verbatims: list[str] = Field(default_factory=list)
    no_indent_envs: list[str] = Field(default_factory=list)
    wrap_chars: list[str] = Field(default_factory=list)
    format_tables: bool = False


class TexProjectConfig(BaseModel):
    # Reject stray top-level keys, e.g. a pre-[project] `dependencies = [...]`
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    project: ProjectConfig = Field(default_factory=ProjectConfig)
    build: BuildConfig = Field(default_factory=BuildConfig)
    tex_fmt: TexFmtConfig = Field(default_factory=TexFmtConfig, alias="tex-fmt")

    @property
    def name(self) -> str | None:
        return self.project.name

    @property
    def src_dir(self) -> str:
        return self.project.src_dir

    @property
    def dependencies(self) -> list[str]:
        return self.project.dependencies

    @property
    def main_files(self) -> list[str]:
        """Main .tex files relative to src_dir (build.default_files, else project.main)."""
        return self.build.default_files or [self.project.main]

    @property
    def set_jobname(self) -> str | None:
        """Jobname to pass to latexmk; None leaves latexmk's default."""
        return self.build.jobname or self.name

    @property
    def jobname(self) -> str:
        """Name of the output files, as latexmk will produce them."""
        return self.set_jobname or Path(self.main_files[0]).stem
