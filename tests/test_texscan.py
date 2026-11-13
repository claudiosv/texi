import pytest

from texi import texscan

SRC = rb"""\documentclass[11pt,a4paper]{article}
% \usepackage{commented}
\usepackage[opt={a,b}]{amsmath, amssymb}
\RequirePackage{xcolor}[2020/01/01]
\usepackage{hyperref}% trailing
\begin{verbatim}
\usepackage{inverb}
\end{verbatim}
\LoadClassWithOptions{report}
\RequirePackageWithOptions{foo}
\usepackage
  [x]
  {multi,
   line}
\iffalse \usepackage{iffalsepkg} \fi
\newcommand{\pkg}[1]{\usepackage{#1}}
"""

COMMON = {
    "article",
    "amsmath",
    "amssymb",
    "xcolor",
    "hyperref",
    "report",
    "foo",
    "multi",
    "line",
}


@pytest.fixture
def tree():
    if not texscan.prefetch():
        pytest.skip("tree-sitter LaTeX grammar not available")


def test_tree_skips_comments_verbatim_iffalse(tree):
    assert texscan.loaded_names(SRC) == COMMON


def test_regex_fallback(monkeypatch):
    monkeypatch.setattr(texscan, "_parser", lambda: None)
    found = texscan.loaded_names(SRC)
    assert found >= COMMON
    assert {"commented", "inverb", "iffalsepkg"} <= found  # what the tree fixes
    assert "#1" not in found
