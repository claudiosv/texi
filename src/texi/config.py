from enum import Enum

from pydantic import BaseModel, Field, field_validator


class PdfMode(str, Enum):
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


class PdfUpdateMethod(int, Enum):
    MANUAL = 0
    SIGNAL = 1
    POPEN = 2
    SYSTEM = 3


class LatexmkEngines(BaseModel):
    pdf_mode: PdfMode = PdfMode.PDFLATEX
    dvi_mode: bool = False
    postscript_mode: bool = False


class LatexmkDirectories(BaseModel):
    out_dir: str = "build/"
    aux_dir: str = "build/aux/"

    @field_validator("out_dir", "aux_dir")
    @classmethod
    def ensure_trailing_slash(cls, v: str) -> str:
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


class LatexmkFiles(BaseModel):
    # Top-level .tex files to compile; latexmk's @default_files
    default_files: list[str] = Field(default_factory=list)
    # Extra extensions removed by `latexmk -c`
    clean_ext: list[str] = Field(default_factory=list)
    # Extra extensions removed by `latexmk -C` (full clean)
    clean_full_ext: list[str] = Field(default_factory=list)
    # Extensions latexmk treats as generated (triggers rerun detection)
    generated_exts: list[str] = Field(default_factory=list)


class LatexmkViewers(BaseModel):
    pdf_previewer: str | None = None
    dvi_previewer: str | None = None
    ps_previewer: str | None = None
    # How the viewer is signalled when the output file changes
    pdf_update_method: PdfUpdateMethod | None = None
    dvi_update_method: PdfUpdateMethod | None = None
    ps_update_method: PdfUpdateMethod | None = None
    # Signal number used when update_method == SIGNAL (default: SIGHUP = 1)
    pdf_update_signal: int | None = Field(default=None, ge=1, le=64)
    dvi_update_signal: int | None = Field(default=None, ge=1, le=64)
    ps_update_signal: int | None = Field(default=None, ge=1, le=64)


class LatexmkConfig(BaseModel):
    engines: LatexmkEngines = Field(default_factory=LatexmkEngines)
    directories: LatexmkDirectories = Field(default_factory=LatexmkDirectories)
    programs: LatexmkPrograms = Field(default_factory=LatexmkPrograms)
    execution: LatexmkExecution = Field(default_factory=LatexmkExecution)
    files: LatexmkFiles = Field(default_factory=LatexmkFiles)
    viewers: LatexmkViewers = Field(default_factory=LatexmkViewers)


class TexProjectConfig(BaseModel):
    dependencies: list[str] = Field(default_factory=list)
    latexmk: LatexmkConfig = Field(default_factory=LatexmkConfig)
