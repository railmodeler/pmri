"""``IdTag`` -- one RFID / tag identity a reader has seen (or hasn't).

The JMRI mobile-throttle apps read ID-tag reads to attribute a
locomotive to a physical block via an RFID reader; the tag bean stores
the last reporter that saw it and the timestamp.

Wire shape (per ``jmri/server/json/idtag/idTag-server.json``):

* ``name`` -- system name (typically ``IDxxx``).
* ``userName`` -- friendly label.
* ``comment`` -- free-form.
* ``state`` -- 0 UNKNOWN, 2 SEEN, 3 UNSEEN.
* ``reporter`` -- system name of the last reporter to have read this
  tag, or ``null`` if never seen.
* ``time`` -- ISO-8601 timestamp of the last sighting, or ``null``.
"""
from __future__ import annotations

from jmri_core.bean import AbstractNamedBean
from jmri_core.manager import Manager

# JMRI Java's IdTag.getState() values. UNKNOWN=0 matches every other
# NamedBean's unknown; SEEN=2 mirrors sensor ACTIVE=2; UNSEEN=3 is
# specific to IdTag (a tag that WAS seen but has since been reported
# gone by the reader).
ID_TAG_STATE_UNKNOWN = 0
ID_TAG_STATE_SEEN = 2
ID_TAG_STATE_UNSEEN = 3


class IdTag(AbstractNamedBean):
    """One RFID / detector tag. Read-only from the layout's perspective --
    the app doesn't command tags, it observes reader reports."""

    __slots__ = ("_reporter_name", "_state", "_time")

    type_letter = "D"

    def __init__(
        self, system_name: str, user_name: str | None = None,
    ) -> None:
        super().__init__(system_name, user_name)
        self._state: int = ID_TAG_STATE_UNKNOWN
        # Set by a reporter when the tag comes into range; kept as a
        # string so the JSON layer can hand it back without needing a
        # live reference to the reporter bean.
        self._reporter_name: str | None = None
        self._time: str | None = None

    @property
    def state(self) -> int:
        return self._state

    @property
    def reporter_name(self) -> str | None:
        return self._reporter_name

    @property
    def time(self) -> str | None:
        return self._time

    async def report_seen(
        self, reporter_name: str, when: str,
    ) -> None:
        """A reader saw this tag at time ``when``. ``when`` is an ISO
        timestamp string -- no timezone parsing at the bean level,
        callers supply what they already have."""
        self._state = ID_TAG_STATE_SEEN
        self._reporter_name = reporter_name
        self._time = when
        await self.state_changed.emit(self._state)

    async def report_unseen(self, when: str) -> None:
        """The reader no longer sees this tag. Reporter attribution is
        retained so clients can render "last seen at X, gone since Y"."""
        self._state = ID_TAG_STATE_UNSEEN
        self._time = when
        await self.state_changed.emit(self._state)


class IdTagManager(Manager[IdTag]):
    type_letter = "D"

    def _create(self, system_name: str) -> IdTag:
        return IdTag(system_name)
