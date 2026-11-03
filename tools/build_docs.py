"""Generate the pdoc reference under ``pmri-web/src/pmri_web/static/docs/``.

Run from the repo root:

    python -m tools.build_docs

or equivalently::

    python tools/build_docs.py

Behind the scenes: invokes ``pdoc`` with all 12 rewrite packages as
targets, writes the HTML tree into the web package's static dir, and
copies :doc:`OVERVIEW.md` into the same tree as ``index.html`` (so
``/docs/`` lands on the narrative overview, not pdoc's default
package list -- which is fine but less friendly).

Requires ``pdoc`` on the environment; install with ``pip install pdoc``.
Not a runtime dependency of pmri-server -- the docs are baked into
the web wheel when the release is built, not regenerated on every
server start.

Docs are cheap to regenerate (~2s across the twelve packages) so
this doesn't need to be incremental; a full rebuild every time is
simpler and always fresh.
"""
from __future__ import annotations

import argparse
import importlib
import pathlib
import pkgutil
import shutil
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "pmri-web" / "src" / "pmri_web" / "static" / "docs"


def _iter_submodules(package_name: str) -> list[str]:
    """Return every submodule name under *package_name*, recursively.

    pdoc will only auto-cover what's re-exported in a package's
    ``__init__.py`` (a real limitation when you use plain submodules
    without ``from .submod import *``). We enumerate the tree
    ourselves so every ``.py`` file gets a section in the generated
    HTML -- matters most for packages like ``pmri_core`` where
    ``ConfigStore`` / ``Profile`` / ``PowerManager`` live in
    submodules the top-level ``__init__`` doesn't re-export.
    """
    try:
        pkg = importlib.import_module(package_name)
    except ImportError as e:
        print(f"warning: {package_name} not importable ({e})", file=sys.stderr)
        return []
    out = [package_name]
    if hasattr(pkg, "__path__"):
        for info in pkgutil.walk_packages(pkg.__path__, prefix=f"{package_name}."):
            out.append(info.name)
    return out

# Every rewrite package. Order matters for pdoc's sidebar navigation --
# pmri_core first (foundational), then infrastructure, then drivers,
# then top-of-stack (server, web, decoderpro).
PACKAGES = [
    "pmri_core",
    "pmri_scripting",
    "pmri_logixng",
    "pmri_nce",
    "pmri_loconet",
    "pmri_dccex",
    "pmri_openlcb",
    "pmri_ncemonitor",
    "pmri_withrottle",
    "pmri_decoderpro",
    "pmri_server",
    "pmri_web",
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out", type=pathlib.Path, default=OUT_DIR,
        help=f"output directory (default: {OUT_DIR})",
    )
    parser.add_argument(
        "--no-overview", action="store_true",
        help="skip copying OVERVIEW.md as the docs index page",
    )
    args = parser.parse_args()

    out: pathlib.Path = args.out
    # Clean output so removed modules don't leave stale HTML behind.
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)

    # Enumerate submodules explicitly so pdoc renders every ``.py``
    # file, not just the top-level ``__init__.py`` re-exports.
    all_modules: list[str] = []
    for package in PACKAGES:
        all_modules.extend(_iter_submodules(package))
    print(f"building docs for {len(all_modules)} modules -> {out}")
    result = subprocess.run(
        [
            sys.executable, "-m", "pdoc",
            *all_modules,
            "-o", str(out),
            # No favicon needed; we don't publish this beyond the web tab.
            # ``--no-search`` keeps the tree lighter; the sidebar is
            # enough for a codebase this size.
            "--no-search",
        ],
        cwd=REPO_ROOT,
    )
    if result.returncode != 0:
        print("pdoc failed", file=sys.stderr)
        return result.returncode

    if not args.no_overview:
        overview = REPO_ROOT / "docs" / "OVERVIEW.md"
        if overview.is_file():
            # pdoc writes its own index.html listing every package.
            # Preserve that under `packages.html` so the sidebar link
            # still works, and render OVERVIEW.md as the new landing.
            existing = out / "index.html"
            if existing.is_file():
                shutil.move(str(existing), str(out / "packages.html"))
            _render_overview_index(overview, out / "index.html")
        else:
            print(
                f"note: {overview} not found; pdoc's default index remains",
                file=sys.stderr,
            )
    print("done.")
    return 0


def _render_overview_index(md_path: pathlib.Path, out_path: pathlib.Path) -> None:
    """Wrap OVERVIEW.md in a minimal HTML shell that matches pdoc's
    styling for continuity. Not a full markdown renderer -- we just
    ``<pre>``-wrap the source; the target audience is developers who
    can read the raw markdown fine, and it saves a dependency on a
    markdown library."""
    body = md_path.read_text(encoding="utf-8")
    out_path.write_text(
        _INDEX_HTML_TEMPLATE.replace("__BODY__", body),
        encoding="utf-8",
    )


_INDEX_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <title>JMRI Python Rewrite -- Overview</title>
  <style>
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI",
                   Roboto, sans-serif;
      max-width: 900px; margin: 2rem auto; padding: 0 1rem;
      background: #1e1f22; color: #e5e6e8; line-height: 1.55;
    }
    a { color: #6ab7ff; }
    pre {
      background: #2b2c30; border: 1px solid #3a3b3f;
      border-radius: 4px; padding: 1rem; overflow-x: auto;
      white-space: pre-wrap; font-family: "SFMono-Regular", Consolas,
                                          "Liberation Mono", monospace;
    }
    p.nav { margin-bottom: 2rem; }
  </style>
</head>
<body>
  <p class="nav">
    <a href="packages.html">Package index (auto-generated reference)</a>
    &nbsp;.&nbsp; <a href="/">Back to home</a>
  </p>
  <pre>__BODY__</pre>
</body>
</html>
"""


if __name__ == "__main__":
    sys.exit(main())
