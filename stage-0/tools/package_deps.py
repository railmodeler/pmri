"""Build a package-level dependency graph from jmri.jar.

Parses each `.class` file's constant pool to extract all referenced class names
(constant pool tag 7), maps each to its package, then aggregates to a directed
graph at a configurable package depth (default 3, e.g. `jmri.jmrix.loconet`).

Outputs:
  - inventory/package-deps.dot       -- full graph in Graphviz DOT
  - inventory/port-priority.md       -- topological layering with SCC callouts

This is an inventory tool, not a precision dependency analyzer. It sees class
references in constant pools -- i.e. method-signature types, field types, and
explicit class literals -- but not reflection or string-based class lookups.
For Stage 0 port planning that's sufficient.
"""
from __future__ import annotations

import collections
import io
import pathlib
import struct
import sys
import zipfile

# Constant pool tag constants -- JVM spec sec.4.4
CP_UTF8                 = 1
CP_INTEGER              = 3
CP_FLOAT                = 4
CP_LONG                 = 5  # 8 bytes, takes 2 cp slots
CP_DOUBLE               = 6  # 8 bytes, takes 2 cp slots
CP_CLASS                = 7
CP_STRING               = 8
CP_FIELDREF             = 9
CP_METHODREF            = 10
CP_INTERFACE_METHODREF  = 11
CP_NAME_AND_TYPE        = 12
CP_METHOD_HANDLE        = 15
CP_METHOD_TYPE          = 16
CP_DYNAMIC              = 17
CP_INVOKE_DYNAMIC       = 18
CP_MODULE               = 19
CP_PACKAGE              = 20


def read_constant_pool_classes(class_bytes: bytes) -> tuple[str, set[str]]:
    """Return (this_class_name, set of referenced class names) for one .class file.

    Class names are returned in JVM internal form: ``jmri/jmrix/loconet/LnTurnout``.
    """
    buf = io.BytesIO(class_bytes)
    magic, _minor, _major, cp_count = struct.unpack(">IHHH", buf.read(10))
    if magic != 0xCAFEBABE:
        raise ValueError("not a class file")

    utf8: dict[int, str] = {}
    class_name_idx: dict[int, int] = {}   # cp-index -> name_index for CP_CLASS entries

    i = 1
    while i < cp_count:
        (tag,) = struct.unpack(">B", buf.read(1))
        if tag == CP_UTF8:
            (length,) = struct.unpack(">H", buf.read(2))
            utf8[i] = buf.read(length).decode("utf-8", errors="replace")
        elif tag == CP_CLASS:
            (name_idx,) = struct.unpack(">H", buf.read(2))
            class_name_idx[i] = name_idx
        elif tag in (CP_FIELDREF, CP_METHODREF, CP_INTERFACE_METHODREF,
                     CP_NAME_AND_TYPE, CP_DYNAMIC, CP_INVOKE_DYNAMIC):
            buf.read(4)
        elif tag in (CP_INTEGER, CP_FLOAT):
            buf.read(4)
        elif tag in (CP_LONG, CP_DOUBLE):
            buf.read(8)
            i += 1                  # eats the next slot per spec
        elif tag == CP_STRING:
            buf.read(2)
        elif tag == CP_METHOD_HANDLE:
            buf.read(3)
        elif tag in (CP_METHOD_TYPE, CP_MODULE, CP_PACKAGE):
            buf.read(2)
        else:
            raise ValueError(f"unknown cp tag {tag} at idx {i}")
        i += 1

    # access_flags(u2), this_class(u2), super_class(u2)
    _flags, this_idx, _super_idx = struct.unpack(">HHH", buf.read(6))
    this_name_idx = class_name_idx.get(this_idx, 0)
    this_class = utf8.get(this_name_idx, "?")

    referenced = {utf8[idx] for idx in class_name_idx.values() if idx in utf8}
    return this_class, referenced


def package_of(class_name: str, depth: int) -> str | None:
    """Return the package at given depth for an internal-form class name.

    Drops the final segment (the class name itself) and truncates the remaining
    package path to ``depth`` segments. Skips JVM-internal array descriptors
    (``[Ljmri/...``) and external libs (``java/``, ``javax/``, ``sun/``,
    ``jdk/``, ``com/``, ``org/``).
    """
    if not class_name or class_name.startswith("["):
        return None
    parts = class_name.split("/")
    if parts[0] in ("java", "javax", "sun", "jdk", "com", "org"):
        return None
    pkg_parts = parts[:-1]              # drop trailing class name
    if not pkg_parts:
        return None                     # default package -- skip
    return ".".join(pkg_parts[:depth])


def main(jar_path: pathlib.Path, dot_out: pathlib.Path, md_out: pathlib.Path,
         depth: int = 3) -> None:
    edges: dict[tuple[str, str], int] = collections.Counter()
    pkg_classes: dict[str, int] = collections.Counter()
    parse_errors = 0

    with zipfile.ZipFile(jar_path) as jar:
        for name in jar.namelist():
            if not name.endswith(".class"):
                continue
            try:
                this_class, refs = read_constant_pool_classes(jar.read(name))
            except Exception:
                parse_errors += 1
                continue
            src_pkg = package_of(this_class, depth)
            if src_pkg is None:
                continue
            pkg_classes[src_pkg] += 1
            for ref in refs:
                tgt_pkg = package_of(ref, depth)
                if tgt_pkg is None or tgt_pkg == src_pkg:
                    continue
                edges[(src_pkg, tgt_pkg)] += 1

    # ----- DOT output -----
    dot_lines = ["digraph jmri_packages {", "  rankdir=LR;", "  node [shape=box, fontsize=10];"]
    for pkg, n in sorted(pkg_classes.items()):
        dot_lines.append(f'  "{pkg}" [label="{pkg}\\n{n} classes"];')
    for (a, b), w in sorted(edges.items()):
        # Thin out the noise: only edges with weight >= 3 in the DOT
        if w >= 3:
            dot_lines.append(f'  "{a}" -> "{b}" [weight={w}, label="{w}"];')
    dot_lines.append("}\n")
    dot_out.write_text("\n".join(dot_lines), encoding="utf-8")

    # ----- Topological ordering with SCC detection (Tarjan) -----
    successors: dict[str, set[str]] = collections.defaultdict(set)
    predecessors: dict[str, set[str]] = collections.defaultdict(set)
    for (a, b) in edges:
        successors[a].add(b)
        predecessors[b].add(a)

    sccs = tarjan_scc(pkg_classes.keys(), successors)
    scc_of = {pkg: i for i, comp in enumerate(sccs) for pkg in comp}
    condensed: dict[int, set[int]] = collections.defaultdict(set)
    for (a, b) in edges:
        ai, bi = scc_of[a], scc_of[b]
        if ai != bi:
            condensed[ai].add(bi)

    in_deg = {i: 0 for i in range(len(sccs))}
    for i, outs in condensed.items():
        for j in outs:
            in_deg[j] += 1

    # Kahn layers
    layer_of: dict[int, int] = {}
    frontier = [i for i, d in in_deg.items() if d == 0]
    cur = 0
    while frontier:
        nxt: list[int] = []
        for i in frontier:
            layer_of[i] = cur
            for j in condensed.get(i, ()):
                in_deg[j] -= 1
                if in_deg[j] == 0:
                    nxt.append(j)
        frontier = nxt
        cur += 1

    # ----- Markdown output -----
    lines: list[str] = []
    lines.append("# Package dependency graph & port-priority hint\n")
    lines.append(
        f"Extracted from `{jar_path.name}` by parsing class-file constant pools.\n"
        f"Depth: {depth} (package granularity).\n"
        f"Classes scanned: **{sum(pkg_classes.values())}**, "
        f"packages: **{len(pkg_classes)}**, edges: **{len(edges)}**, "
        f"parse errors: {parse_errors}.\n"
    )
    lines.append("\n## Port-priority layers (topological)\n")
    lines.append(
        "Each layer contains packages whose *internal* dependencies are all in "
        "earlier layers. Layer 0 is leaf packages -- port these first because "
        "nothing else inside JMRI depends on them transitively.\n"
    )
    lines.append(
        "Where multiple packages share a layer, they're independent and can be "
        "ported in parallel. Cycles are collapsed into strongly-connected "
        "components (SCCs) -- packages in the same SCC must be ported together.\n"
    )

    by_layer: dict[int, list[int]] = collections.defaultdict(list)
    for scc_idx, layer in layer_of.items():
        by_layer[layer].append(scc_idx)

    for layer in sorted(by_layer):
        lines.append(f"\n### Layer {layer}\n")
        for scc_idx in by_layer[layer]:
            comp = sorted(sccs[scc_idx])
            total_classes = sum(pkg_classes[p] for p in comp)
            if len(comp) == 1:
                lines.append(f"- `{comp[0]}` ({pkg_classes[comp[0]]} classes)")
            else:
                lines.append(
                    f"- **SCC** ({total_classes} classes across {len(comp)} pkgs -- port together):"
                )
                for p in comp:
                    lines.append(f"    - `{p}` ({pkg_classes[p]} classes)")

    lines.append("\n## Top fan-out packages (depend on many others)\n")
    lines.append("| Package | Outgoing edges |")
    lines.append("|---|---:|")
    fanouts = sorted(((p, len(s)) for p, s in successors.items()), key=lambda t: -t[1])
    for p, n in fanouts[:20]:
        lines.append(f"| `{p}` | {n} |")

    lines.append("\n## Top fan-in packages (depended on by many others)\n")
    lines.append("| Package | Incoming edges |")
    lines.append("|---|---:|")
    fanins = sorted(((p, len(s)) for p, s in predecessors.items()), key=lambda t: -t[1])
    for p, n in fanins[:20]:
        lines.append(f"| `{p}` | {n} |")

    md_out.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {dot_out}")
    print(f"Wrote {md_out}")
    print(f"  packages: {len(pkg_classes)}, edges: {len(edges)}, sccs: {len(sccs)}, layers: {cur}")


def tarjan_scc(nodes, successors):
    """Tarjan's strongly-connected components. Iterative to avoid recursion limits."""
    index_of: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    result: list[list[str]] = []
    counter = [0]

    def strongconnect(start):
        work = [(start, iter(successors.get(start, ())))]
        index_of[start] = lowlink[start] = counter[0]; counter[0] += 1
        stack.append(start); on_stack.add(start)
        while work:
            v, it = work[-1]
            try:
                w = next(it)
                if w not in index_of:
                    index_of[w] = lowlink[w] = counter[0]; counter[0] += 1
                    stack.append(w); on_stack.add(w)
                    work.append((w, iter(successors.get(w, ()))))
                elif w in on_stack:
                    lowlink[v] = min(lowlink[v], index_of[w])
            except StopIteration:
                if lowlink[v] == index_of[v]:
                    comp = []
                    while True:
                        w = stack.pop(); on_stack.discard(w)
                        comp.append(w)
                        if w == v:
                            break
                    result.append(comp)
                work.pop()
                if work:
                    parent, _ = work[-1]
                    lowlink[parent] = min(lowlink[parent], lowlink[v])

    for n in nodes:
        if n not in index_of:
            strongconnect(n)
    return result


if __name__ == "__main__":
    jar = pathlib.Path(sys.argv[1])
    dot_out = pathlib.Path(sys.argv[2])
    md_out  = pathlib.Path(sys.argv[3])
    depth = int(sys.argv[4]) if len(sys.argv) > 4 else 3
    main(jar, dot_out, md_out, depth)
