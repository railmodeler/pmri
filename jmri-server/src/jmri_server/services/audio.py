"""``AudioService`` -- JSON topic ``audio``.

Wire shape per ``jmri/server/json/audio/audio-server.json`` and the
JMRI Java handler ``JsonAudioHttpService`` (case 16 STOPPED, case 17
PLAYING, ``command`` = Play / Stop on post).
"""
from __future__ import annotations

from typing import Any

from jmri_core.beans.audio import (
    AUDIO_STATE_PLAYING,
    AUDIO_STATE_STOPPED,
    Audio,
    AudioManager,
)

from jmri_server.service import JsonServiceError
from jmri_server.services.beans import _BeanService, _common_named_bean


class AudioService(_BeanService):
    topic = "audio"

    def manager(self) -> AudioManager:
        return self.ctx.connection.audios

    def project(self, bean: Audio) -> dict[str, Any]:
        return _common_named_bean(bean) | {
            "state": bean.state,
            "kind": bean.kind,
            "url": bean.url,
            "gain": bean.gain,
        }

    async def apply(self, bean: Audio, data: dict[str, Any]) -> None:
        if "kind" in data:
            kind = data["kind"]
            if kind not in ("source", "buffer", "listener"):
                raise JsonServiceError(400, f"invalid audio kind: {kind!r}")
            bean.kind = kind
        if "url" in data:
            bean.url = str(data["url"]) if data["url"] is not None else None
        if "gain" in data:
            try:
                gain = float(data["gain"])
            except (TypeError, ValueError):
                raise JsonServiceError(400, "audio.gain must be numeric") from None
            if not 0.0 <= gain <= 1.0:
                raise JsonServiceError(400, "audio.gain must be in [0.0, 1.0]")
            bean.gain = gain
        # Explicit state override (rare -- clients normally send
        # ``command`` instead) but scripts sometimes set state directly.
        if "state" in data:
            state = int(data["state"])
            if state not in (AUDIO_STATE_STOPPED, AUDIO_STATE_PLAYING):
                raise JsonServiceError(
                    400,
                    f"audio.state must be {AUDIO_STATE_STOPPED} (stopped) or "
                    f"{AUDIO_STATE_PLAYING} (playing)",
                )
            if state == AUDIO_STATE_PLAYING:
                await bean.play()
            else:
                await bean.stop()
        # JMRI Java's Play / Stop verbs. Only meaningful on sources; a
        # buffer or listener silently no-ops the command rather than
        # 400-ing to keep the API forgiving for polymorphic clients.
        command = data.get("command")
        if command == "Play":
            if bean.kind == "source":
                await bean.play()
        elif command == "Stop":
            if bean.kind == "source":
                await bean.stop()
        elif command is not None:
            raise JsonServiceError(400, f"unknown audio command: {command!r}")
