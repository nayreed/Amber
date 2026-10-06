r"""Idempotent LaTeX hygiene: make long \code{} identifiers breakable (\codew) in body text,
keep plain \code in captions/headings/TikZ, and unescape \_ inside \code."""
import re, glob, sys
def process_text(t, thr=11):
    def f(m):
        body = m.group(1).replace("\\_", "_")
        return ("\\codew{%s}" if len(body) > thr else "\\code{%s}") % body
    return re.sub(r"\\code\{([^{}]*)\}", f, t)
for fn in sorted(glob.glob("chapters/*.tex")):
    out = []; changed = False
    for ln in open(fn).read().split("\n"):
        if re.search(r"\\caption|\\(section|subsection|chapter)\{", ln) or ln.lstrip().startswith("\\node") or "tikzpicture" in ln:
            new = re.sub(r"\\codew\{", r"\\code{", ln)
        elif ln.lstrip().startswith("\\paragraph{"):
            i = ln.index("\\paragraph{") + len("\\paragraph{"); d = 1; j = i
            while j < len(ln) and d > 0:
                d += (ln[j] == "{") - (ln[j] == "}"); j += 1
            new = ln[:j] + process_text(ln[j:])
        else:
            new = process_text(ln)
        changed |= new != ln; out.append(new)
    if changed:
        open(fn, "w").write("\n".join(out)); print("updated", fn)
