"""``Consist`` -- an ordered group of locomotives operating together.

Not a NamedBean: a consist is a configuration object that owns a set of
addresses + per-loco direction flags. The hardware-level consisting (whether
the loconet/NCE/etc. command station tracks the group, or whether the
software simply broadcasts identical commands to each address) is the
driver's concern; here we only carry the data.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ConsistKind = Literal["advanced", "command_station", "software"]

# Consist addresses are constrained to NMRA CV19's 7-bit range across
# every kind -- advanced consists literally encode the group into CV19
# on each decoder, and we hold command-station / software consists to
# the same rule so the address space is uniform and the UI doesn't
# have to fork on kind. Bit 7 of CV19 carries direction, so only
# 1..127 is available for the group identifier itself.
CONSIST_ADDRESS_MAX = 127


class ConsistLoco(BaseModel):
    """One member of a consist. Individual member locomotives may still
    have long addresses -- only the *consist*'s own address is
    restricted to short 1..127."""

    address: int = Field(ge=1, le=10239)
    long_address: bool = False
    direction_normal: bool = True
    # Position is the index within the consist; first added = 0.
    position: int = Field(ge=0)


class Consist(BaseModel):
    """An ordered group of locomotives.

    The "consist address" identifies the group on the bus. For advanced
    (CV19) consists this is the active short address shared across locos;
    for command-station consists it's a slot/handle the command station
    assigns; for software consists it's the bookkeeping key. All three
    variants are held to the 1..127 short-address range for consistency
    with the CV19 wire limit.
    """

    address: int = Field(ge=1, le=CONSIST_ADDRESS_MAX)
    kind: ConsistKind = "advanced"
    locos: list[ConsistLoco] = Field(default_factory=list)
    comment: str = ""

    def add(self, loco: ConsistLoco) -> None:
        self.locos.append(loco)

    def remove(self, address: int) -> bool:
        """Drop the loco at *address*. Returns True if found."""
        for i, loco in enumerate(self.locos):
            if loco.address == address:
                del self.locos[i]
                return True
        return False
