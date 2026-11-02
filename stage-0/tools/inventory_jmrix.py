"""Enumerate jmri.jmrix.* hardware-system packages from jmri.jar.

Produces inventory/jmrix-systems.md with one row per top-level jmrix.<system>
package: total class count, sub-package breakdown, and a placeholder column
for human-rated port complexity (filled in during Stage 0 review).

This is a mechanical inventory -- the port-complexity rating, message-protocol
shape, and priority order are judgement calls captured later.
"""
from __future__ import annotations

import collections
import pathlib
import sys
import zipfile

PREFIX = "jmri/jmrix/"


# Hand-curated short descriptions for the major systems we know about.
# Anything not listed here gets a TODO description.
KNOWN_SYSTEMS: dict[str, dict[str, str]] = {
    "loconet":    {"vendor": "Digitrax",      "transport": "Serial/USB/TCP",  "shape": "async bus broadcast"},
    "can":        {"vendor": "MERG/CBUS et al.","transport": "CAN/USB",       "shape": "CAN frames"},
    "lenz":       {"vendor": "Lenz",          "transport": "Serial/USB/TCP",  "shape": "XpressNet req/resp"},
    "openlcb":    {"vendor": "OpenLCB / LCC", "transport": "CAN/TCP",         "shape": "event-driven LCC"},
    "nce":        {"vendor": "NCE",           "transport": "Serial/USB",      "shape": "sync req/resp + poll"},
    "cmri":       {"vendor": "CMRI",          "transport": "Serial",          "shape": "poll/response packet"},
    "powerline":  {"vendor": "X10/Insteon",   "transport": "Powerline",       "shape": "broadcast cmd"},
    "dccpp":      {"vendor": "DCC-EX",        "transport": "Serial/TCP",      "shape": "text protocol"},
    "bidib":      {"vendor": "BiDiB",         "transport": "Serial/IP",       "shape": "binary nodes"},
    "bachrus":    {"vendor": "Bachrus",       "transport": "Serial",          "shape": "speedometer-only"},
    "sprog":      {"vendor": "SPROG-DCC",     "transport": "Serial/USB",      "shape": "programmer-focused"},
    "rps":        {"vendor": "JMRI RPS",      "transport": "Serial",          "shape": "ultrasonic positioning"},
    "roco":       {"vendor": "Roco/Fleischmann","transport": "Serial/USB",    "shape": "XpressNet variant"},
    "ecos":       {"vendor": "ESU ECoS",      "transport": "TCP",             "shape": "text protocol"},
    "ieee802154": {"vendor": "ZigBee/Xbee",   "transport": "Wireless",        "shape": "packet"},
    "srcp":       {"vendor": "SRCP",          "transport": "TCP",             "shape": "text protocol (RFC-ish)"},
    "rfid":       {"vendor": "Generic RFID",  "transport": "Serial",          "shape": "tag read events"},
    "ipocs":      {"vendor": "RailComm IPOCS","transport": "TCP",             "shape": "ATC/CTC"},
    "grapevine":  {"vendor": "ProTrak",       "transport": "Serial",          "shape": "node poll"},
    "marklin":    {"vendor": "Märklin CS",    "transport": "TCP",             "shape": "CS2/CS3 IP"},
    "maple":      {"vendor": "Maple Systems", "transport": "Serial",          "shape": "PLC polling"},
    "tams":       {"vendor": "TAMS",          "transport": "Serial",          "shape": "MC binary"},
    "mrc":        {"vendor": "MRC",           "transport": "Serial",          "shape": "binary req/resp"},
    "acela":      {"vendor": "CTI Acela",     "transport": "Serial",          "shape": "node poll"},
    "mqtt":       {"vendor": "MQTT broker",   "transport": "TCP+MQTT",        "shape": "pub/sub topics"},
    "zimo":       {"vendor": "Zimo MX1/10",   "transport": "Serial/USB",      "shape": "binary req/resp"},
    "oaktree":    {"vendor": "Oak Tree",      "transport": "Serial",          "shape": "node poll"},
    "secsi":      {"vendor": "Tracetronics",  "transport": "Serial",          "shape": "node poll"},
    "easydcc":    {"vendor": "EasyDCC",       "transport": "Serial",          "shape": "binary"},
    "dcc4pc":     {"vendor": "DCC4PC",        "transport": "Serial/USB",      "shape": "RailCom reader"},
    "jmriclient": {"vendor": "JMRI bridge",   "transport": "TCP",             "shape": "JMRI-to-JMRI"},
    "pricom":     {"vendor": "Pricom",        "transport": "Serial",          "shape": "binary"},
    "tmcc":       {"vendor": "Lionel TMCC",   "transport": "Serial",          "shape": "binary"},
    "xpa":        {"vendor": "XpressNet/PA",  "transport": "Serial",          "shape": "DTMF/phone-bridge"},
    "internal":   {"vendor": "JMRI internal", "transport": "in-memory",       "shape": "loopback / test bus"},
    "ncemonitor": {"vendor": "NCE",           "transport": "Serial",          "shape": "NCE bus sniffer"},
    "qsi":        {"vendor": "QSI",           "transport": "Serial",          "shape": "programmer"},
    "direct":     {"vendor": "DCC direct",    "transport": "Serial",          "shape": "raw DCC packets"},
    "pi":         {"vendor": "Raspberry Pi",  "transport": "GPIO",            "shape": "GPIO sensors/turnouts"},
    "jinput":     {"vendor": "JInput",        "transport": "USB HID",         "shape": "gamepad/throttle input"},
    "anyma":      {"vendor": "Anyma uDMX",    "transport": "USB",             "shape": "DMX lighting"},
    "ztc":        {"vendor": "ZTC",           "transport": "Serial",          "shape": "binary"},
    "swing":      {"vendor": "(UI helpers)",  "transport": "-",               "shape": "shared Swing widgets"},
    "purejavacomm":{"vendor": "(transport)",  "transport": "Serial",          "shape": "library wrapper"},
    "configurexml":{"vendor": "(framework)",  "transport": "-",               "shape": "shared XML config"},
    "wangrow":    {"vendor": "Wangrow",       "transport": "Serial",          "shape": "NCE-compatible"},
    "jserialcomm":{"vendor": "(transport)",   "transport": "Serial",          "shape": "library wrapper"},
    "debugthrottle":{"vendor": "(test)",      "transport": "-",               "shape": "test fixture"},
    "fakeport":   {"vendor": "(test)",        "transport": "-",               "shape": "test fixture"},
    "libusb":     {"vendor": "(transport)",   "transport": "USB",             "shape": "library wrapper"},
}


def main(jar_path: pathlib.Path, out_md: pathlib.Path) -> None:
    per_system: dict[str, dict[str, int]] = collections.defaultdict(
        lambda: {"total": 0, "depth1": 0}
    )
    sub_packages: dict[str, collections.Counter[str]] = collections.defaultdict(
        collections.Counter
    )

    with zipfile.ZipFile(jar_path) as jar:
        for name in jar.namelist():
            if not name.startswith(PREFIX) or not name.endswith(".class"):
                continue
            rest = name[len(PREFIX):]
            parts = rest.split("/")
            if len(parts) == 1:
                # Class lives directly in jmri/jmrix/ -- shared infrastructure,
                # not a per-system package. Skip from the per-system rollup.
                continue
            system = parts[0]
            per_system[system]["total"] += 1
            if len(parts) == 2:
                per_system[system]["depth1"] += 1
            else:
                sub = parts[1]
                sub_packages[system][sub] += 1

    lines: list[str] = []
    lines.append("# jmri.jmrix.* hardware system inventory\n")
    lines.append(
        "Mechanical extraction from `jmri.jar`: one row per top-level "
        "`jmri.jmrix.<system>` package. Class counts are total `.class` files "
        "under that package (including inner classes).\n"
    )
    lines.append(
        "**Port complexity** is a judgement call (S/M/L/XL) made during Stage 0 "
        "review -- left as TODO until that pass. Numbers are mechanical signals: "
        "high class count + many sub-packages + binary-protocol shape = larger port.\n"
    )

    lines.append("\n## Summary table\n")
    lines.append("| System | Classes | Sub-pkgs | Vendor | Transport | Protocol shape | Port effort |")
    lines.append("|---|---:|---:|---|---|---|---|")

    rows: list[tuple[str, int, int, str, str, str]] = []
    for system, counts in per_system.items():
        meta = KNOWN_SYSTEMS.get(system, {})
        rows.append((
            system,
            counts["total"],
            len(sub_packages[system]),
            meta.get("vendor", "_TODO_"),
            meta.get("transport", "_TODO_"),
            meta.get("shape", "_TODO_"),
        ))
    rows.sort(key=lambda r: -r[1])

    for system, total, subs, vendor, transport, shape in rows:
        lines.append(
            f"| `{system}` | {total} | {subs} | {vendor} | {transport} | {shape} | _TODO_ |"
        )

    lines.append("\n## Per-system sub-package breakdown\n")
    for system, total, subs, vendor, *_ in rows:
        if subs == 0:
            continue
        lines.append(f"\n### `{system}` -- {total} classes, {subs} sub-packages -- {vendor}\n")
        lines.append("| Sub-package | Classes |")
        lines.append("|---|---:|")
        for sub, n in sub_packages[system].most_common():
            lines.append(f"| `{system}.{sub}` | {n} |")

    lines.append("\n## Notes for the Stage 0 review pass\n")
    lines.append(
        "For each system, fill in **Port effort** (S = days, M = 1-2 weeks, "
        "L = 3-6 weeks, XL = 2+ months) based on: protocol complexity, hardware "
        "availability for testing, presence of a Python equivalent library, "
        "and how much of the package is shared infrastructure vs. unique "
        "protocol logic.\n"
    )
    lines.append(
        "Recommended port-order anchors:\n"
        "1. `internal` (already done in Stage 1)\n"
        "2. `loconet` -- sets the async-push pattern\n"
        "3. `nce` -- sets the sync-poll pattern\n"
        "4. `dccpp` -- modern text protocol, quick win, large user base\n"
        "5. `openlcb` -- event-driven, network-native, very different shape\n"
        "6. `mqtt` -- pub/sub broker pattern\n"
        "7. _Remaining 38 systems_ -- community-driven from here, each a "
        "self-contained PyPI package.\n"
    )

    out_md.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {out_md} ({len(rows)} systems, {sum(r[1] for r in rows)} classes)")


if __name__ == "__main__":
    jar_path = pathlib.Path(sys.argv[1])
    out_md = pathlib.Path(sys.argv[2])
    main(jar_path, out_md)
