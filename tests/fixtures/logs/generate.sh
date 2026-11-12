#!/usr/bin/env bash
# Regenerates the real-tool fixtures in this directory (macOS, TeX Live 2026 basic).
# Usage: tests/fixtures/logs/generate.sh   (works in /tmp/texi_corpus/work, copies results here)
set -o nounset
HERE="$(cd "$(dirname "$0")" && pwd)"
W=/tmp/texi_corpus/work
rm -rf "$W"; mkdir -p "$W"; cd "$W" || exit 1
export max_print_line=100000 error_line=254 half_error_line=238

tex() { # tex <engine> <case> <fle: fle|nofle> <<< source
  local eng=$1 case=$2 fle=$3
  cat > "$case.tex"
  local flag=-no-file-line-error; [ "$fle" = fle ] && flag=-file-line-error
  "$eng" -interaction=nonstopmode "$flag" -jobname "${eng}_${case}" "$case.tex" > "${eng}_${case}.stdout" 2>&1
  cp "${eng}_${case}.log" "$HERE/${eng}_${case}.log"
}

PRE='\documentclass{article}'

tex pdflatex undefcs fle <<EOF2
$PRE
\begin{document}
Hello \foo world.
\bar
\end{document}
EOF2
tex pdflatex undefcs_nofle nofle <<EOF2
$PRE
\begin{document}
Hello \foo world.
\end{document}
EOF2
tex pdflatex missingfile fle <<EOF2
$PRE
\begin{document}
\input{nope}
\includegraphics{x}
\end{document}
EOF2
tex pdflatex missingpkg fle <<EOF2
$PRE
\usepackage{nonexistentpkg}
\begin{document}
Hi
\end{document}
EOF2
tex pdflatex missingpkg_nofle nofle <<EOF2
$PRE
\usepackage{nonexistentpkg}
\begin{document}
Hi
\end{document}
EOF2
tex pdflatex runaway fle <<EOF2
$PRE
\begin{document}
\def\a#1{[#1]}
\a{x

y}
\textbf{abc
\end{document}
EOF2
tex pdflatex refs fle <<EOF2
$PRE
\begin{document}
See \ref{nolabel} and \cite{nokey}.
\section{A}\label{a}
Also \pageref{missing}.
\begin{thebibliography}{9}\bibitem{k}Key.\end{thebibliography}
\end{document}
EOF2
tex pdflatex boxes fle <<EOF2
$PRE
\usepackage[paperwidth=4cm,paperheight=6cm]{geometry}
\begin{document}
\hbadness=0
Averyveryveryverylongwordthatdoesnotfitonthelineatallbecauseitiswaytoolong and more.

\hbox to 10cm{short}

\hsize=3cm \parindent=0pt \raggedright
a b \hfuzz=0pt \hbox to 5cm{\hskip 0pt plus 1pt x}

\vbox to 20cm{\hrule}\vbadness=0 \vbox to 5cm{\vskip 0pt plus 1pt x}
\end{document}
EOF2
tex pdflatex fontshape fle <<EOF2
$PRE
\begin{document}
\fontshape{xx}\selectfont Hello
\fontfamily{qqq}\selectfont World
\end{document}
EOF2
tex xelatex fontmissing fle <<EOF2
$PRE
\usepackage{fontspec}
\setmainfont{Latin Modern Roman}
\tracinglostchars=1
\begin{document}
Hello 😀 ☃ world.
\end{document}
EOF2
tex pdflatex babel fle <<EOF2
$PRE
\usepackage[klingon]{babel}
\begin{document}
Hi
\end{document}
EOF2
tex pdflatex pkgerror fle <<EOF2
$PRE
\makeatletter
\begin{document}
\PackageError{mypkg}{Something went wrong}{Try this\MessageBreak and that.}
\GenericError{(gen)\space\space\space}{Gen error}{Gen help}{More help}
\ClassError{myclass}{Class trouble}{Class help}
\PackageWarning{mypkg}{A warning\MessageBreak with two lines}
\PackageWarningNoLine{mypkg}{No line warning}
\PackageInfo{mypkg}{Info message}
\ClassWarning{myclass}{Class warning}
\@latex@warning{Custom LaTeX warning}
\end{document}
EOF2
tex pdflatex math fle <<EOF2
$PRE
\begin{document}
\$x^2^3\$ and \$x_1_2\$.

Missing dollar: x^2 here.

\$\$ \left( x \$\$
\begin{equation} \frac{1}{ \end{equation}
\end{document}
EOF2
tex pdflatex fileended fle <<EOF2
$PRE
\begin{document}
\def\a#1{[#1]}
\a{unterminated
EOF2
tex pdflatex emergency fle <<EOF2
$PRE
\begin{document}
\input{missing-input}
\end{document}
EOF2
tex pdflatex emergency_nofle nofle <<EOF2
$PRE
\begin{document}
\input{missing-input}
\end{document}
EOF2
tex pdflatex ok fle <<EOF2
$PRE
\begin{document}
Hello. \newpage World.\newpage Three.
\end{document}
EOF2
tex xelatex ok fle <<EOF2
$PRE
\usepackage{fontspec}
\begin{document}
Hello world.
\end{document}
EOF2
tex lualatex ok fle <<EOF2
$PRE
\usepackage{fontspec}
\begin{document}
Hello world.
\end{document}
EOF2
tex lualatex luaerror fle <<EOF2
$PRE
\begin{document}
\directlua{error("boom")}
\directlua{local x = nil; x.y = 1}
\end{document}
EOF2
tex lualatex luawarn fle <<EOF2
$PRE
\usepackage{fontspec}
\setmainfont{NoSuchFontAnywhere}
\begin{document}
Hello
\end{document}
EOF2
tex xelatex xetexerr fle <<EOF2
$PRE
\usepackage{fontspec}
\setmainfont{NoSuchFontAnywhere}
\begin{document}
Hello \foo
\end{document}
EOF2
tex pdflatex undefcs_cite fle <<EOF2
$PRE
\begin{document}
\cite{a}
\end{document}
EOF2
tex pdflatex pdftexwarn fle <<EOF2
$PRE
\usepackage{graphicx}
\begin{document}
\pdfpageattr{/Rotate 0}
\pdfmapfile{+nonexistent.map}
Hi \pdfliteral{q}
\end{document}
EOF2

# xdvipdfmx failure: a corrupt png
printf 'not a png' > bad.png
tex xelatex xdvipdfmx fle <<EOF2
$PRE
\usepackage{graphicx}
\begin{document}
\includegraphics{bad.png}
\end{document}
EOF2
cp xelatex_xdvipdfmx.stdout "$HERE/xelatex_xdvipdfmx.stdout.txt"

# shell escape
tex pdflatex shellescape fle <<EOF2
$PRE
\begin{document}
\immediate\write18{echo hi}
\immediate\openout3=out.txt \immediate\write3{x}\immediate\closeout3
\end{document}
EOF2

# --- bibtex
cat > refs.bib <<'EOF2'
@article{good, author={A. Author}, title={T}, journal={J}, year=2000}
@article{nojournal, author={B. Author}, title={Missing stuff}, year=2001}
@article{broken, author={C. Author} title={no comma}, year=2002}
EOF2
cat > bt.tex <<'EOF2'
\documentclass{article}
\begin{document}
\cite{good,nojournal,absent}
\bibliographystyle{plain}
\bibliography{refs,missingdb}
\end{document}
EOF2
pdflatex -interaction=nonstopmode -file-line-error -jobname bt bt.tex > /dev/null 2>&1
env -u max_print_line -u error_line -u half_error_line bibtex bt > bt.stdout 2>&1
cp bt.blg "$HERE/bibtex_errors.blg"
cp bt.log "$HERE/pdflatex_bibtex_aux.log"
cat > bt2.tex <<'EOF2'
\documentclass{article}
\begin{document}
\cite{good,nojournal}
\bibliographystyle{plain}
\bibliography{refs}
\bibliographystyle{alpha}
\end{document}
EOF2
cat > refs2.bib <<'EOF2'
@article{good, author={A. Author}, title={T}, journal={J}, year=2000}
@article{nojournal, author={B. Author}, title={Missing stuff}, year=2001}
EOF2
sed 's/refs}/refs2}/' bt2.tex > bt3.tex
pdflatex -interaction=nonstopmode bt3.tex >/dev/null 2>&1; env -u max_print_line -u error_line -u half_error_line bibtex bt3 >/dev/null 2>&1
cp bt3.blg "$HERE/bibtex_warn.blg"
pdflatex -interaction=nonstopmode bt3.tex >/dev/null 2>&1; pdflatex -interaction=nonstopmode bt3.tex >/dev/null 2>&1
# bibtex: missing .aux / style
printf '\\relax\n\\citation{good}\n\\bibstyle{nostyle}\n\\bibdata{refs2}\n' > bt4.aux
env -u max_print_line -u error_line -u half_error_line bibtex bt4 > bt4.stdout 2>&1; cp bt4.blg "$HERE/bibtex_nostyle.blg"

# --- biber (no biblatex in basic TL; only the missing-control-file case is real)
biber nonexistent > biber_nocontrol.stdout 2>&1
cp nonexistent.blg "$HERE/biber_nocontrol.blg" 2>/dev/null

# --- makeindex
cat > idx.tex <<'EOF2'
\documentclass{article}\usepackage{makeidx}\makeindex
\begin{document}
Foo\index{foo}\index{bar!baz}\index{zed@Zed}.
\printindex
\end{document}
EOF2
pdflatex -interaction=nonstopmode idx.tex >/dev/null 2>&1
makeindex idx > idx.stdout 2>&1; cp idx.ilg "$HERE/makeindex_ok.ilg"
cat > bad.idx <<'EOF2'
\indexentry{foo}{1}
\indexentry{bar@}{2}
\indexentry{baz|(}{3}
\indexentry{bad{unbalanced}{4}
\indexentry{x!!y}{5}
garbage line
\indexentry{foo}{abc}
EOF2
makeindex bad > bad.stdout 2>&1; cp bad.ilg "$HERE/makeindex_errors.ilg"
makeindex missing.idx > mi.stdout 2>&1; cp mi.stdout "$HERE/makeindex_missing.txt"

# --- latexmk
mkdir -p lm1 && cd lm1
cat > main.tex <<'EOF2'
\documentclass{article}
\begin{document}
\tableofcontents
\section{A}\label{a} See \ref{a}.
\end{document}
EOF2
latexmk -pdf -interaction=nonstopmode -file-line-error main.tex > "$HERE/latexmk_success.txt" 2>&1
latexmk -pdf -interaction=nonstopmode -file-line-error main.tex > "$HERE/latexmk_uptodate.txt" 2>&1
cp main.log "$HERE/pdflatex_latexmk_main.log"
cd ..
mkdir -p lm2 && cd lm2
cat > main.tex <<'EOF2'
\documentclass{article}
\begin{document}
\section{A} See \ref{nolabel}. \foo
\input{nope}
\end{document}
EOF2
latexmk -pdf -interaction=nonstopmode -file-line-error main.tex > "$HERE/latexmk_failed.txt" 2>&1; echo $? > "$HERE/latexmk_failed.exit"
latexmk -pdf -f -interaction=nonstopmode -file-line-error main.tex > "$HERE/latexmk_failed_force.txt" 2>&1; echo $? > "$HERE/latexmk_failed_force.exit"
latexmk -pdf main2.tex > "$HERE/latexmk_nofile.txt" 2>&1; echo $? > "$HERE/latexmk_nofile.exit"
cd ..
mkdir -p lm3 && cd lm3
cat > main.tex <<'EOF2'
\documentclass{article}
\makeatletter
\begin{document}
\newwrite\f
\@ifundefined{c@x}{\newcounter{x}}{}
\InputIfFileExists{\jobname.cnt}{}{\gdef\n{0}}
\immediate\openout\f=\jobname.cnt
\count255=\n \advance\count255 1
\immediate\write\f{\string\gdef\string\n{\the\count255}}
\immediate\closeout\f
\begingroup\def\x{}\label{a}\endgroup
\expandafter\ifnum\n<3 \AtEndDocument{\immediate\write\@mainaux{\string\newlabel{a}{{\n}{1}}}}\fi
\end{document}
EOF2
latexmk -pdf -interaction=nonstopmode -e '$max_repeat=2' main.tex > "$HERE/latexmk_maxruns.txt" 2>&1; echo $? > "$HERE/latexmk_maxruns.exit"
cd ..
# rc error
echo 'this is not perl (' > bad.rc
latexmk -r bad.rc -pdf lm1/main.tex > "$HERE/latexmk_rcerror.txt" 2>&1; echo $? > "$HERE/latexmk_rcerror.exit"
latexmk -badarg > "$HERE/latexmk_badarg.txt" 2>&1; echo $? > "$HERE/latexmk_badarg.exit"
tex pdflatex missingchar fle <<EOF2
$PRE
\begin{document}
\font\x=cmr10 \tracinglostchars=1 \x A\char200 B
\end{document}
EOF2
tex pdflatex unicode fle <<EOF2
$PRE
\usepackage[utf8]{inputenc}
\begin{document}
Hello 😀 world.
\end{document}
EOF2

# latexmk with bibtex + makeindex rules (bibtex breaks under max_print_line=100000, so unset it for that rule)
mkdir -p lm4 && cd lm4
cp ../refs2.bib refs.bib
cat > main.tex <<EOF2
\documentclass{article}\usepackage{makeidx}\makeindex
\begin{document}
Cite \cite{good,nojournal,absent}. Index\index{foo}.
\bibliographystyle{plain}
\bibliography{refs}
\printindex
\end{document}
EOF2
latexmk -pdf -interaction=nonstopmode -file-line-error -e '$bibtex=q(env -u max_print_line -u error_line -u half_error_line bibtex %O %B); $makeindex=q(makeindex %O -o %D %S)' main.tex > "$HERE/latexmk_bib_index.txt" 2>&1; echo $? > "$HERE/latexmk_bib_index.exit"
cd ..

# classic 79-column wrapping (default max_print_line)
cat > wrap.tex <<'EOF3'
\documentclass{article}
\usepackage{amsmath}
\begin{document}
\section{A}\label{averyveryveryveryveryverylonglabelnamethatwillgetwrappedbytheengineatcolumn79}
See \ref{anotherveryveryveryveryveryverylonglabelnamethatwillgetwrappedbytheengineatcolumn79}.
\foo
\end{document}
EOF3
env -u max_print_line -u error_line -u half_error_line pdflatex -interaction=nonstopmode -file-line-error -jobname pdflatex_wrapped79 wrap.tex >/dev/null 2>&1
cp pdflatex_wrapped79.log "$HERE/pdflatex_wrapped79.log"
echo done
