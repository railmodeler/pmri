"""``Audio`` -- a JMRI audio bean (source / buffer / listener).

JMRI's audio subsystem in Java wraps JOAL/OpenAL, but the JSON contract
is a thin bean shape: name, comment, state (16 STOPPED / 17 PLAYING),
plus per-kind properties (a URL for a buffer, a gain for a source).

We ship the bean + manager here; the JSON service in
``jmri_server.services.audio`` exposes list/get/put/post/delete over
the wire with the Play / Stop commands JMRI mobile clients issue.
Actually *playing* audio is out of scope -- this pass gives wire
parity so scripts and mobile clients can drive audio state and
react to state pushes.
"""
from __future__ import annotations

from typing import Literal

from jmri_core.bean import AbstractNamedBean
from jmri_core.manager import Manager

# JMRI Java's Audio.getState() returns these two numbers on audio
# sources -- checked against ``jmri.server.json.audio.JsonAudioHttpService``
# (case 16 = STOPPED, case 17 = PLAYING). The stage-0 schema lists
# ``[0, 2, 4, 8]``; that predates the state-enum revision and doesn't
# reflect what live JMRI Java actually emits. We follow the code.
AUDIO_STATE_STOPPED = 16
AUDIO_STATE_PLAYING = 17

AudioKind = Literal["source", "buffer", "listener"]


class Audio(AbstractNamedBean):
    """One audio object. Kind selects between the three JMRI audio roles."""

    __slots__ = ("_state", "gain", "kind", "url")

    type_letter = "A"

    def __init__(
        self, system_name: str, user_name: str | None = None,
    ) -> None:
        super().__init__(system_name, user_name)
        self._state: int = AUDIO_STATE_STOPPED
        self.kind: AudioKind = "source"
        # Source: URL of a buffer to play. Buffer: URL of the sample file.
        # Listener: unused. All are opaque strings for the JSON layer.
        self.url: str | None = None
        # Source gain 0..1. Ignored for buffer / listener.
        self.gain: float = 1.0

    @property
    def state(self) -> int:
        return self._state

    async def play(self) -> None:
        """Mark this source as playing; emits ``state_changed``."""
        if self._state == AUDIO_STATE_PLAYING:
            return
        self._state = AUDIO_STATE_PLAYING
        await self.state_changed.emit(self._state)

    async def stop(self) -> None:
        """Mark this source as stopped; emits ``state_changed``."""
        if self._state == AUDIO_STATE_STOPPED:
            return
        self._state = AUDIO_STATE_STOPPED
        await self.state_changed.emit(self._state)


class AudioManager(Manager[Audio]):
    type_letter = "A"

    def _create(self, system_name: str) -> Audio:
        return Audio(system_name)
