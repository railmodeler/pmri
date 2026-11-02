"""NCE command-set strategy -- captures variations between firmware versions.

NCE command stations have evolved memory layouts and feature flags. The
three production variants Stage 4 supports:

* **PH-Pro 2006+** -- full PowerPro/Smart Booster with the post-2006 binary
  command set; turnout/macro/consist tables at the canonical addresses.
* **PowerCab** -- smaller command station, different turnout-memory base,
  same binary command opcodes.
* **SB5 (Smart Booster)** -- booster-only variant; same memory map as
  PH-Pro 2006+ but a few features (e.g. macro recall) are unavailable.

The :class:`NceCommandSet` dataclass captures every per-variant constant
so service code (turnout monitor, AIU poller, programmer) reads memory
addresses from the active command set rather than hard-coding.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class NceCommandSet:
    """Per-firmware constants used by services."""

    name: str
    turnout_memory_base: int    # base of accessory/turnout status table
    turnout_table_bytes: int    # 8 turnouts per byte (256 bytes -> 2048 turnouts)
    macro_memory_base: int      # base of macro table
    # NCE consist state lives in three separate memory regions, one per
    # role. Per-consist offset: HEAD/TAIL = consist_num * 2 (2 bytes
    # each), MID = consist_num * consist_mid_entries * 2. Together the
    # regions are contiguous (HEAD | TAIL | MID) but treating them as
    # one 16-byte record per consist is *wrong* -- cross-checked against
    # ``NceCmdStationMemory.getConsistHeadAddr`` (0xF500),
    # ``getConsistTailAddr`` (0xF600), and ``getConsistMidAddr``
    # (0xF700) in the JMRI Java source. Per-region sizes on stock
    # firmware: HEAD 256 B, TAIL 256 B, MID 1024 B (4 mids x 128 consists
    # x 2 B).
    consist_head_addr: int      # base of LEAD table (2 B x 128 consists)
    consist_tail_addr: int      # base of REAR table (2 B x 128 consists)
    consist_mid_addr: int       # base of MID table
    consist_mid_entries: int    # mid slots per consist (4 on stock NCE)
    supports_ops_mode_prog: bool
    aiu_polling_supported: bool


# ---- canonical instances --------------------------------------------------


NCE_PH_PRO_2006 = NceCommandSet(
    name="PH-Pro 2006+",
    turnout_memory_base=0xEC00,
    turnout_table_bytes=256,
    macro_memory_base=0xC800,
    consist_head_addr=0xF500,
    consist_tail_addr=0xF600,
    consist_mid_addr=0xF700,
    consist_mid_entries=4,
    supports_ops_mode_prog=True,
    aiu_polling_supported=True,
)

NCE_POWERCAB = NceCommandSet(
    name="PowerCab",
    turnout_memory_base=0xEC00,
    turnout_table_bytes=64,    # PowerCab supports fewer turnouts (~512)
    macro_memory_base=0xC800,
    consist_head_addr=0xF500,
    consist_tail_addr=0xF600,
    consist_mid_addr=0xF700,
    consist_mid_entries=4,
    supports_ops_mode_prog=True,
    aiu_polling_supported=False,  # AIU isn't typically used with PowerCab
)

NCE_SB5 = NceCommandSet(
    name="Smart Booster SB5",
    turnout_memory_base=0xEC00,
    turnout_table_bytes=256,
    macro_memory_base=0xC800,
    consist_head_addr=0xF500,
    consist_tail_addr=0xF600,
    consist_mid_addr=0xF700,
    consist_mid_entries=4,
    supports_ops_mode_prog=True,
    aiu_polling_supported=True,
)


COMMAND_SETS: dict[str, NceCommandSet] = {
    cs.name: cs for cs in (NCE_PH_PRO_2006, NCE_POWERCAB, NCE_SB5)
}


def get_command_set(name: str) -> NceCommandSet:
    """Look up a command set by friendly name. Raises ``KeyError`` if unknown."""
    if name not in COMMAND_SETS:
        raise KeyError(
            f"unknown NCE command set: {name!r} (have: {sorted(COMMAND_SETS)})"
        )
    return COMMAND_SETS[name]
