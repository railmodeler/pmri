"""``OBlock`` -- an occupancy block that participates in JMRI's Warrant
system.

Real OBlocks in JMRI Java hold Portals, Paths, and Warrants -- they're
the graph substrate the Warrant engine drives trains through. That
whole subsystem is deferred with Panel Editor (Tier 1 #3). This bean
carries only the fields the JSON schema requires, so mobile clients
that probe ``oblock`` see valid shapes.

Wire shape (per ``jmri/server/json/oblock/oblock-server.json``):

* ``status`` -- an integer bitfield JMRI uses for occupancy + allocation
  state. Enum lists valid combos (0/1/2/4/8/16/18/20/24/32/34/36/40/
  64/66/68/72/128/130/132/136); we default 0 (UNOCCUPIED) and let the
  caller drive it.
* ``value`` -- string associated with whatever's in the block (idTag,
  reporter, or free text).
* ``sensor`` -- name of the occupancy sensor.
* ``warrant`` -- allocated warrant (a warrant is a train's reservation
  across a chain of oblocks). We store the name; the Warrant engine
  itself doesn't exist yet.
* ``train`` -- train name from the warrant, mirrored so mobile clients
  don't have to cross-reference.
"""
from __future__ import annotations

from pmri_core.bean import AbstractNamedBean
from pmri_core.manager import Manager

OBLOCK_STATUS_UNOCCUPIED = 0
OBLOCK_STATUS_OCCUPIED = 2


class OBlock(AbstractNamedBean):
    """One occupancy block. Round-trip only in this pass."""

    __slots__ = (
        "_status", "sensor_name", "train_name", "value", "warrant_name",
    )

    type_letter = "B"

    def __init__(
        self, system_name: str, user_name: str | None = None,
    ) -> None:
        super().__init__(system_name, user_name)
        self._status: int = OBLOCK_STATUS_UNOCCUPIED
        self.value: str | None = None
        self.sensor_name: str | None = None
        self.warrant_name: str | None = None
        self.train_name: str | None = None

    @property
    def status(self) -> int:
        return self._status

    async def set_status(self, status: int) -> None:
        if self._status == status:
            return
        self._status = status
        await self.state_changed.emit(self._status)


class OBlockManager(Manager[OBlock]):
    # JMRI uses ``OB``-prefixed system names; managers are constructed
    # with prefix ``"O"`` + type letter ``"B"``.
    type_letter = "B"

    def _create(self, system_name: str) -> OBlock:
        return OBlock(system_name)
