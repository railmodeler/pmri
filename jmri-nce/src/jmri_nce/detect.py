"""Auto-detect the NCE command-set variant via ``SW_REV_CMD`` (opcode 0xAA).

The query returns 3 bytes: major version, minor version, and a model byte.
We use the model byte to choose between the three production variants;
falls back to PH-Pro 2006+ if the model is unknown (the most permissive
of the three).
"""
from __future__ import annotations

import logging

from jmri_nce.command_set import (
    NCE_PH_PRO_2006,
    NCE_POWERCAB,
    NCE_SB5,
    NceCommandSet,
)
from jmri_nce.messages import NceCommandError, query_command_version
from jmri_nce.traffic import NceTrafficController

_log = logging.getLogger(__name__)


# Model byte values per the NCE binary command reference.
MODEL_POWERPRO  = 0x00
MODEL_SB5       = 0x01
MODEL_POWERCAB  = 0x02
MODEL_TWIN      = 0x03


def model_to_command_set(model: int) -> NceCommandSet:
    """Choose the canonical command set for an NCE model byte."""
    if model == MODEL_POWERCAB:
        return NCE_POWERCAB
    if model == MODEL_SB5:
        return NCE_SB5
    # PowerPro, Twin, and unknowns all use the PH-Pro 2006+ map.
    return NCE_PH_PRO_2006


async def detect_command_set(
    tc: NceTrafficController, *, fallback: NceCommandSet = NCE_PH_PRO_2006
) -> NceCommandSet:
    """Query the command station and pick the matching command set.

    On any error (timeout, unsupported by old firmware, NACK) we log a
    warning and return *fallback* so the connection still opens.
    """
    try:
        reply = await tc.send_command(query_command_version())
    except (TimeoutError, NceCommandError) as e:
        _log.warning("NCE version query failed (%s); falling back to %s", e, fallback.name)
        return fallback
    if len(reply.data) < 3:
        _log.warning("NCE version reply too short: %r", reply.data)
        return fallback
    major, minor, model = reply.data[0], reply.data[1], reply.data[2]
    cs = model_to_command_set(model)
    _log.info("NCE firmware v%d.%d, model=%#x -> %s", major, minor, model, cs.name)
    return cs
