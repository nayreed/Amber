# Amber and 6G Ambient IoT: a teaching text

A study guide (LaTeX, ~160 pages) that teaches the physics, protocols and simulation
methodology behind the Amber 6G Ambient IoT simulator and its SenSys 2026 demo abstract.

* `main.pdf` – the compiled book
* `main.tex`, `preamble.tex`, `chapters/` – LaTeX sources (21 chapters + 6 appendices)
* `figures/` – vector figures generated from real simulator runs
* `code/` – scripts that regenerate every figure, table and exercise answer
  (see Appendix F of the book)
* `references.bib` – bibliography

Build: `latexmk -pdf main.tex` (TeX Live with tikz, circuitikz, tcolorbox, siunitx, algorithmicx, natbib).
