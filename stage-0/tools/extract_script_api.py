"""Static-analyze Jython scripts to extract the empirical JMRI script API.

For each *.py file under the JMRI install's jython/ directory, parse with
ast.parse and collect every Attribute access chain (e.g. ``turnouts.provideTurnout``).
Aggregate counts across all files and emit a Markdown report.

Output: contracts/script-api.md with two ranked tables:
  1. JMRI-namespaced attribute paths (jmri.*, plus common bare globals like
     ``turnouts``, ``sensors``, ``masts`` that JMRI's script context injects).
  2. Java standard library paths (java.*, javax.*) -- these reveal Swing/IO
     surfaces user scripts depend on.

Plus a tail of "unclassified" attribute paths (rare / domain-specific).
"""
from __future__ import annotations

import ast
import collections
import pathlib
import sys

JMRI_INJECTED_GLOBALS = {
    # See Bundle.java / JmriScriptEngineManager.java; these names are
    # pre-bound into the script execution context.
    "turnouts", "sensors", "masts", "signals", "lights", "memories",
    "reporters", "routes", "blocks", "sections", "transits", "layoutblocks",
    "warrants", "audio", "shutdown", "programmers", "addressedProgrammers",
    "logixs", "conditionals", "idtags", "throttle", "throttleManager",
    "powermanager", "programmer", "logixng", "InstanceManager",
}

JAVA_ROOTS = {"java", "javax", "org"}


def chain_path(node: ast.AST) -> str | None:
    """Return dotted path for ``a.b.c`` style Attribute chains, else None."""
    parts: list[str] = []
    cur: ast.AST = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        return ".".join(reversed(parts))
    return None


def walk_file(path: pathlib.Path, counts: collections.Counter[str]) -> None:
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(src, filename=str(path))
    except SyntaxError:
        # Some Jython scripts use Java-specific syntax (rare); skip.
        return
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            p = chain_path(node)
            if p:
                counts[p] += 1


def classify(path: str) -> str:
    root = path.split(".", 1)[0]
    if root in JAVA_ROOTS:
        return "java"
    if root.startswith("jmri") or root in JMRI_INJECTED_GLOBALS:
        return "jmri"
    return "other"


def main(jython_root: pathlib.Path, out_md: pathlib.Path) -> None:
    counts: collections.Counter[str] = collections.Counter()
    file_count = 0
    for p in jython_root.rglob("*.py"):
        walk_file(p, counts)
        file_count += 1

    by_class: dict[str, list[tuple[str, int]]] = {"jmri": [], "java": [], "other": []}
    for path, n in counts.most_common():
        by_class[classify(path)].append((path, n))

    lines: list[str] = []
    lines.append("# JMRI Jython script API surface\n")
    lines.append(
        f"Extracted from {file_count} `.py` files under "
        f"`{jython_root}` by static AST walk.\n"
    )
    lines.append(
        "Counts are **attribute-access occurrences**, not unique callers; "
        "a high count means the path is depended on by many scripts and "
        "must be preserved (or shimmed) for script compatibility.\n"
    )

    def render(title: str, key: str, top: int) -> None:
        lines.append(f"\n## {title} (top {top})\n")
        lines.append("| Rank | Path | Hits |")
        lines.append("|---:|---|---:|")
        for i, (path, n) in enumerate(by_class[key][:top], 1):
            lines.append(f"| {i} | `{path}` | {n} |")

    render("JMRI script surface", "jmri", 200)
    render("Java stdlib / Swing surface used by scripts", "java", 100)
    render("Other (3rd-party / unclassified) attribute paths", "other", 50)

    lines.append("\n## Summary counts\n")
    lines.append(f"- Files analyzed: **{file_count}**")
    lines.append(f"- Unique attribute paths: **{len(counts)}**")
    lines.append(f"- JMRI-namespaced unique paths: **{len(by_class['jmri'])}**")
    lines.append(f"- Java-namespaced unique paths: **{len(by_class['java'])}**")
    lines.append(f"- Other unique paths: **{len(by_class['other'])}**")

    out_md.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {out_md} ({len(counts)} unique paths from {file_count} files)")


if __name__ == "__main__":
    jython_root = pathlib.Path(sys.argv[1])
    out_md = pathlib.Path(sys.argv[2])
    main(jython_root, out_md)
