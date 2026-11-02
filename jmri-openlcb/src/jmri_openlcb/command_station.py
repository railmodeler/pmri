"""OpenLCB Command Station node -- S-9.7.4.7 sec.6 + S-9.7.5.1.

When JMRI-Python is attached to a DCC command station (NCE, LocoNet,
DCC-EX), we can expose each of its locomotives as a virtual OpenLCB
train node. Every hosted loco gets its own alias, its own train
NodeID (per :func:`~jmri_openlcb.throttle.dcc_to_train_node_id`),
and full protocol handling -- an OpenLCB throttle attaching to the
loco doesn't know or care that the actual DCC packets are being
emitted by an NCE Cab Bus in the next room.

What this module handles for each hosted loco:

* **Traction Control Command** (MTI ``0x05EB``):

  - Set Speed/Dir -> :meth:`Throttle.set_speed` + direction
  - Set Function -> :meth:`Throttle.set_function`
  - Emergency Stop -> :meth:`Throttle.emergency_stop`
  - Assign Controller -> acquire the DCC throttle + remember the
    controller's NodeID for later Query Controller
  - Release Controller -> release the DCC throttle + clear controller
  - Query Speed / Query Function / Query Controller -- reply from
    the tracked state

* **Verify NID** -- both flavours (global + addressed): reply with a
  Verified NID from the hosted alias.
* **SNIP** -- reply with a canonical name (``DCC 3`` / ``DCC L3601``).
* **PIP** -- advertise ``TRACTION`` + ``COMMAND_STATION`` +
  ``IDENTIFICATION`` + ``SNIP`` for hosted train aliases.

What's out of scope for this pass:

* **Consist configuration** (Traction 0x30 sub-commands).
* **Traction Management** (0x40 sub-commands -- heartbeat, status).

Both are non-blocking for basic throttle -> loco control; a future
task can extend the command byte dispatcher.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from jmri_core.beans.consist_manager import ConsistManager
from jmri_core.beans.throttle import Throttle
from jmri_core.beans.throttle_manager import ThrottleManager

from jmri_openlcb.datagrams import DatagramEngine
from jmri_openlcb.fdi import SPACE_FDI
from jmri_openlcb.memory import (
    SPACE_CDI,
    MemoryConfigResponder,
)
from jmri_core.state import ThrottleDirection

from jmri_openlcb.addressed import (
    AddressedMessage,
    AddressedReassembler,
    encode_addressed_frames,
)
from jmri_openlcb.alias import NodeId, claim_alias
from jmri_openlcb.codec import (
    CTRL_AME,
    CTRL_AMD,
    CTRL_AMR,
    FT_CONTROL,
    FT_MTI,
    OlcbFrame,
    _pack_header,
    extract_frame_type,
    extract_mti,
    extract_variable_field,
)
from jmri_openlcb.pip import ProtocolFlag, encode_flags
from jmri_openlcb.snip import MTI_SNIP_REPLY, MTI_SNIP_REQUEST, NodeInfo
from jmri_openlcb.throttle import (
    MPH_PER_UNIT,
    dcc_to_train_node_id,
    train_node_id_to_dcc,
)
from jmri_openlcb.traction import (
    CC_ASSIGN,
    CC_QUERY,
    CC_RELEASE,
    CONSIST_ATTACH,
    CONSIST_DETACH,
    CONSIST_INDEX_INVALID,
    CONSIST_QUERY,
    MGMT_NOOP,
    ConsistMember,
    MTI_TRACTION_REPLY,
    MTI_TRACTION_REQUEST,
    TC_CONSIST_CONFIG,
    TC_CONTROLLER_CONFIG,
    TC_EMERGENCY_STOP,
    TC_MANAGEMENT,
    TC_QUERY_FUNCTION,
    TC_QUERY_SPEED,
    TC_SET_FUNCTION,
    TC_SET_SPEED_DIR,
    decode_consist_flags,
    decode_speed,
    encode_consist_flags,
    encode_speed,
)
from jmri_openlcb.traction_search import (
    MTI_IDENTIFY_PRODUCERS,
    MTI_PRODUCER_IDENT_VALID,
    decode_search_event,
)
from jmri_openlcb.events import EventId
from jmri_openlcb.verify import (
    MTI_VERIFIED_NID,
    MTI_VERIFY_NID_ADDRESSED,
    MTI_VERIFY_NID_GLOBAL,
)

if TYPE_CHECKING:
    pass

_log = logging.getLogger(__name__)

# Persistence path for the hosted-loco list. Mirrors the Node ID
# persistence pattern in :mod:`jmri_openlcb.connection` -- lives in
# the JMRI profile directory when available, falls back to a
# hidden dot-dir under home.
_DEFAULT_HOSTED_FILENAME = "openlcb-hosted-locos.json"


def default_hosted_persist_path():
    """Default filesystem location for the hosted-loco list. Uses
    the JMRI profile directory when available, else a portable
    ``~/.jmri-python`` subdir. Same shape as the Node ID persist
    helper so the two files live side by side."""
    import pathlib  # noqa: PLC0415
    try:
        from jmri_core.persistence.profile import Profile  # noqa: PLC0415
        return Profile.default().profile_dir / _DEFAULT_HOSTED_FILENAME
    except Exception:  # noqa: BLE001
        return (
            pathlib.Path("~/.jmri-python").expanduser()
            / _DEFAULT_HOSTED_FILENAME
        )


# PIP flags a hosted train node advertises. Train nodes support the
# Traction protocol (they're locos), Identification (they answer
# Verify NID), SNIP (they answer identity probes), and FDI (they
# advertise a synthesized function description). COMMAND_STATION on a
# *loco* would be wrong -- that's for the CS itself.
TRAIN_PIP_FLAGS = (
    ProtocolFlag.IDENTIFICATION
    | ProtocolFlag.SNIP
    | ProtocolFlag.TRACTION
    | ProtocolFlag.FDI
    | ProtocolFlag.MEMORY_CONFIG
)


# Per-loco CDI: two editable string fields -- user_name and
# user_description -- that a throttle can set via the CDI editor.
# Backed by SPACE_CONFIG (0xFE). User-name reads travel through the
# SNIP responder so a UI change immediately reflects in identity
# probes without a service restart.
CDI_USER_NAME_OFFSET = 0
CDI_USER_NAME_SIZE = 63
CDI_USER_DESC_OFFSET = 64
CDI_USER_DESC_SIZE = 128
CDI_CONFIG_TOTAL_BYTES = CDI_USER_NAME_OFFSET + CDI_USER_NAME_SIZE + 1 + CDI_USER_DESC_SIZE

_TRAIN_CDI_XML = """\
<?xml version="1.0"?>
<cdi>
  <identification>
    <manufacturer>JMRI-Python</manufacturer>
    <model>DCC Train</model>
    <hardwareVersion>bridged-DCC</hardwareVersion>
    <softwareVersion>0.1.0</softwareVersion>
  </identification>
  <segment origin="0" space="0xFE">
    <name>User settings</name>
    <string size="63">
      <name>User name</name>
      <description>Display label for this loco in throttle UIs.</description>
    </string>
    <string size="128">
      <name>User description</name>
      <description>Longer free-form description (roadname, notes, ...).</description>
    </string>
  </segment>
</cdi>
"""


def _train_cdi_bytes() -> bytes:
    """The per-loco CDI XML with the trailing null the wire expects."""
    return _TRAIN_CDI_XML.encode("utf-8") + b"\x00"


def _string_from_config(
    responder: MemoryConfigResponder, offset: int, size: int,
) -> str:
    """Extract a null-terminated UTF-8 string from a fixed slot of
    the responder's SPACE_CONFIG. Empty (all-zero) slot decodes to
    the empty string -- matches how a fresh install looks before an
    operator has written anything."""
    raw = responder.get_space(0xFE)
    if raw is None or len(raw) < offset + size:
        return ""
    slot = raw[offset : offset + size]
    end = slot.find(b"\x00")
    if end < 0:
        end = size
    return slot[:end].decode("utf-8", errors="replace")


def _synthesized_fdi_for(address: int, long_address: bool) -> bytes:
    """Build a generic FDI XML for a DCC loco we host.

    F0 gets the canonical "Headlight" label; F1..F28 get "Function N"
    since we have no way to know what each decoder maps them to. All
    functions are binary + latching. A future revision could accept
    per-loco overrides from a JMRI-side config; the synthesized fall-
    back matters because it's what makes the FDI advertisement in
    :data:`TRAIN_PIP_FLAGS` truthful."""
    kind = "L" if long_address else ""
    lines = [
        '<?xml version="1.0"?>',
        "<fdi>",
        "  <segment>",
        f"    <name>DCC {kind}{address} functions</name>",
        "    <group>",
        "      <name>Functions</name>",
    ]
    for n in range(29):   # F0..F28
        name = "Headlight" if n == 0 else f"Function {n}"
        lines.extend([
            '      <function size="1" kind="binary">',
            f"        <name>{name}</name>",
            f"        <number>{n}</number>",
            "      </function>",
        ])
    lines.extend(["    </group>", "  </segment>", "</fdi>", ""])
    return "\n".join(lines).encode("utf-8") + b"\x00"


@dataclass
class HostedLoco:
    """One DCC loco exposed as a virtual OpenLCB train node.

    ``throttle`` is populated when an OpenLCB controller claims the
    loco via Assign Controller; the CS calls
    :meth:`ThrottleManager.acquire` on the DCC side to get the
    handle. ``controller`` remembers which NodeID has us assigned
    so a later Query Controller reply is meaningful."""

    address: int
    long_address: bool
    node_id: NodeId
    alias: int
    throttle: Throttle | None = None
    controller: NodeId = field(default_factory=lambda: NodeId(0))
    consist_members: list[ConsistMember] = field(default_factory=list)
    # Seconds since last inbound traction command (any sub-command);
    # bumped to 0 on Noop / speed / function / e-stop / assign / etc.
    # Callers can watch this for a "controller went silent" watchdog
    # without the CS itself imposing a policy -- real hardware CSes
    # differ on whether silence should e-stop the loco.
    last_activity_at: float = 0.0
    # Per-loco memory responder -- set when the CS was constructed
    # with a datagram_engine so we can serve SPACE_FDI for this
    # train. None when memory-config serving is disabled.
    memory_responder: MemoryConfigResponder | None = None

    @property
    def canonical_name(self) -> str:
        """Human-facing name used in the SNIP reply when no user
        name is set. Long-address locos get an ``L`` prefix so a
        throttle UI can render "DCC L3601" without collisions with
        short address 3601 (which can't exist, but consistency
        helps if the code is ever extended)."""
        prefix = "L" if self.long_address else ""
        return f"DCC {prefix}{self.address}"

    @property
    def user_name(self) -> str:
        """Operator-set display label from SPACE_CONFIG. Empty
        string when never written. Not stored on the dataclass --
        reads directly from the memory responder so a CDI-editor
        write is immediately visible."""
        if self.memory_responder is None:
            return ""
        return _string_from_config(
            self.memory_responder,
            CDI_USER_NAME_OFFSET, CDI_USER_NAME_SIZE,
        )

    @property
    def user_description(self) -> str:
        if self.memory_responder is None:
            return ""
        return _string_from_config(
            self.memory_responder,
            CDI_USER_DESC_OFFSET, CDI_USER_DESC_SIZE,
        )

    @property
    def display_name(self) -> str:
        """Preferred label for a UI: user_name if set, else the
        canonical DCC-derived name."""
        name = self.user_name
        return name if name else self.canonical_name


class CommandStationNode:
    """Serve the Traction protocol for DCC locos on top of an
    OpenLCB traffic controller.

    Construct once per connection, then call :meth:`add_loco` for
    each DCC address you want to expose. Each call claims a fresh
    OpenLCB alias (with the well-known train NodeID) and starts
    answering probes for that virtual node.

    ``dcc_throttles`` is the throttle manager belonging to the DCC
    connection (NCE, LocoNet, DCC-EX). Assign Controller acquires a
    throttle from that manager; the CS then routes every subsequent
    Traction command onto that throttle. Release Controller returns
    it, freeing the DCC-side slot.

    ``station_node_id`` identifies the CS itself -- a distinct
    NodeID from any hosted loco. Used for the AMR frame we send
    when :meth:`close` runs.
    """

    def __init__(
        self,
        tc: _TcLike,
        dcc_throttles: ThrottleManager,
        *,
        station_node_id: NodeId,
        dcc_consists: ConsistManager | None = None,
        datagram_engine: DatagramEngine | None = None,
        persist_path: str | None = None,
    ) -> None:
        """*dcc_consists* is optional. When passed, Attach/Detach
        Traction commands bridge to the DCC ``ConsistManager``: the
        CS ensures a DCC consist exists at the head loco's DCC
        address and adds/removes members by their translated DCC
        addresses. Members whose NodeIDs aren't well-known train
        NIDs are still tracked in the in-memory member list but
        skipped for DCC forwarding -- those are foreign OpenLCB
        trains we can't drive.

        When *dcc_consists* is ``None`` (the default), consist
        state is bookkeeping-only, matching the pre-bridge shape.

        *datagram_engine* is optional. When passed, each
        :meth:`add_loco` registers the hosted alias with the engine
        and stands up a per-loco :class:`MemoryConfigResponder`
        seeded with a synthesized FDI at SPACE_FDI. Throttles reading
        a hosted train's FDI then get generic F0..F28 labels
        (F0=Headlight, F1..F28=Function N). When *datagram_engine* is
        ``None`` the CS operates without memory-config support and
        hosted trains don't advertise FDI in their PIP.

        *persist_path* opts into hosted-loco persistence. When set
        (default :func:`default_hosted_persist_path` via
        :meth:`restore_from_disk`), mutations save a JSON
        snapshot so a restart re-hosts the same trains + user
        names via ``await cs.restore_from_disk()`` on startup.
        """
        import pathlib  # noqa: PLC0415
        self._tc = tc
        self._dcc = dcc_throttles
        self._dcc_consists = dcc_consists
        self._datagram_engine = datagram_engine
        self._persist_path = (
            pathlib.Path(persist_path).expanduser()
            if persist_path is not None else None
        )
        self._station_nid = station_node_id
        self._locos_by_alias: dict[int, HostedLoco] = {}
        self._locos_by_nid: dict[NodeId, HostedLoco] = {}
        self._reassembler = AddressedReassembler()
        self._unsubscribe = tc.frames.connect(self._on_frame)

    @property
    def train_pip_flags(self) -> "ProtocolFlag":
        """The PIP flag set each hosted train advertises. Excludes
        FDI + MEMORY_CONFIG when the CS was constructed without a
        datagram engine -- we shouldn't advertise what we don't
        serve."""
        flags = TRAIN_PIP_FLAGS
        if self._datagram_engine is None:
            flags &= ~ProtocolFlag.FDI
            flags &= ~ProtocolFlag.MEMORY_CONFIG
        return flags

    def close(self) -> None:
        """Stop answering probes. Does *not* release DCC throttles
        or send AMR frames -- call :meth:`remove_loco` first for a
        clean shutdown per loco."""
        self._unsubscribe()

    # ---- loco lifecycle ---------------------------------------------------

    def hosted(self) -> list[HostedLoco]:
        """Snapshot of currently-hosted locos, in insertion order."""
        return list(self._locos_by_alias.values())

    def get(self, address: int, *, long_address: bool = False) -> HostedLoco | None:
        """Find a hosted loco by DCC address (helper for tests + UI)."""
        nid = dcc_to_train_node_id(address, long_address=long_address)
        return self._locos_by_nid.get(nid)

    async def add_loco(
        self, address: int, *, long_address: bool = False,
    ) -> HostedLoco:
        """Claim an alias for *(address, long_address)*, register
        protocol handlers, and start answering probes.

        Idempotent -- a second call for the same address returns the
        existing hosted loco without re-claiming. That matters when
        a UI loop asks the CS to re-add every configured loco on
        startup: repeat calls should be safe."""
        nid = dcc_to_train_node_id(address, long_address=long_address)
        existing = self._locos_by_nid.get(nid)
        if existing is not None:
            return existing
        alias = await claim_alias(self._tc, nid)
        loco = HostedLoco(
            address=address, long_address=long_address,
            node_id=nid, alias=alias,
        )
        # Wire per-loco memory responder + register alias with the
        # shared datagram engine so throttles can read this train's
        # SPACE_FDI. Seed with a synthesized FDI (F0=Headlight,
        # F1..F28=Function N) -- that gives a throttle UI something to
        # label buttons with even when the decoder itself has no
        # opinion.
        if self._datagram_engine is not None:
            self._datagram_engine.accept_alias(alias)

            async def _persist_on_write(
                space: int, offset: int, data: bytes,
            ) -> None:
                # Only SPACE_CONFIG writes carry persistable state
                # (user_name / user_description). Ignore writes to
                # other spaces so unrelated activity doesn't churn
                # the file.
                if space == 0xFE:
                    self._save_persisted()

            responder = MemoryConfigResponder(
                self._datagram_engine,
                target_alias=alias,
                on_write=_persist_on_write,
            )
            responder.set_space(
                SPACE_FDI,
                _synthesized_fdi_for(address, long_address),
                read_only=True,
            )
            # Per-loco CDI + editable user settings. The CDI XML at
            # SPACE_CDI describes two string fields; the actual
            # bytes live in SPACE_CONFIG which starts zero-filled
            # and grows the strings as the operator saves via the
            # CDI editor.
            responder.set_space(SPACE_CDI, _train_cdi_bytes(), read_only=True)
            responder.set_space(0xFE, bytes(CDI_CONFIG_TOTAL_BYTES))
            loco.memory_responder = responder
        self._locos_by_alias[alias] = loco
        self._locos_by_nid[nid] = loco
        self._save_persisted()
        return loco

    async def remove_loco(
        self, address: int, *, long_address: bool = False,
    ) -> None:
        """Release the DCC throttle (if held), send AMR to release
        the alias, and drop the hosted loco. Safe to call on a
        loco we never hosted -- no-op in that case."""
        nid = dcc_to_train_node_id(address, long_address=long_address)
        loco = self._locos_by_nid.pop(nid, None)
        if loco is None:
            return
        self._locos_by_alias.pop(loco.alias, None)
        # Tear down the per-loco memory responder + unregister the
        # alias from the shared datagram engine so a peer probing
        # the just-removed alias sees nothing rather than a stale
        # FDI reply.
        if loco.memory_responder is not None:
            loco.memory_responder.close()
            loco.memory_responder = None
        if self._datagram_engine is not None:
            self._datagram_engine.stop_accepting(loco.alias)
        # Dissolve any DCC-side consist we built for this loco before
        # dropping the throttle -- some drivers' ConsistManager.delete
        # sends a KILL/UNLINK that needs the throttle handle.
        await self._bridge_dissolve(loco)
        if loco.throttle is not None:
            try:
                await self._dcc.release(loco.throttle)
            except Exception as e:  # noqa: BLE001
                _log.warning(
                    "releasing DCC throttle for %s failed: %s",
                    loco.canonical_name, e,
                )
        # AMR announces the alias is going back to the pool; the
        # variable field is 0x703, data is our 6-byte NID.
        await self._tc.send_frame(OlcbFrame(
            header=_pack_header(0x0, CTRL_AMR, loco.alias),
            data=nid.bytes,
        ))
        self._save_persisted()

    # ---- persistence -----------------------------------------------------

    def _save_persisted(self) -> None:
        """Write the current hosted-loco list (address, long_address,
        user_name, user_description) to :attr:`_persist_path`. No-op
        when persistence is off. Best-effort: file errors log a
        warning but don't disrupt the CS."""
        if self._persist_path is None:
            return
        try:
            import json  # noqa: PLC0415
            snapshot = [
                {
                    "address": loco.address,
                    "long_address": loco.long_address,
                    "user_name": loco.user_name,
                    "user_description": loco.user_description,
                }
                for loco in self._locos_by_alias.values()
            ]
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            self._persist_path.write_text(
                json.dumps(snapshot, indent=2), encoding="utf-8",
            )
        except OSError as e:
            _log.warning(
                "could not save hosted-loco list to %s: %s",
                self._persist_path, e,
            )

    async def restore_from_disk(self) -> list[HostedLoco]:
        """Load the persisted hosted-loco list and re-``add_loco``
        each entry, restoring user_name / user_description via a
        SPACE_CONFIG write. Startup hook shape::

            cs = CommandStationNode(..., persist_path=...)
            await cs.restore_from_disk()

        Returns the list of restored HostedLocos in load order.
        Silently returns ``[]`` when persistence is off or the file
        doesn't exist yet (first boot). A single malformed loco
        entry is skipped with a warning rather than failing the
        whole restore."""
        if self._persist_path is None or not self._persist_path.exists():
            return []
        try:
            import json  # noqa: PLC0415
            raw = self._persist_path.read_text(encoding="utf-8")
            entries = json.loads(raw)
        except (OSError, ValueError) as e:
            _log.warning(
                "could not read hosted-loco list from %s: %s",
                self._persist_path, e,
            )
            return []
        if not isinstance(entries, list):
            _log.warning(
                "hosted-loco list at %s is not a JSON array -- ignoring",
                self._persist_path,
            )
            return []
        restored: list[HostedLoco] = []
        for entry in entries:
            if not isinstance(entry, dict) or "address" not in entry:
                _log.warning(
                    "skipping malformed hosted-loco entry: %r", entry,
                )
                continue
            try:
                loco = await self.add_loco(
                    int(entry["address"]),
                    long_address=bool(entry.get("long_address", False)),
                )
                # Restore user_name / user_description by writing
                # them into SPACE_CONFIG at the canonical offsets.
                # This bypasses the persist callback re-save (loco
                # is already known + writes are idempotent).
                if loco.memory_responder is not None:
                    self._restore_user_strings(loco, entry)
                restored.append(loco)
            except Exception as e:  # noqa: BLE001
                _log.warning(
                    "restore of loco %r failed: %s", entry, e,
                )
        return restored

    def _restore_user_strings(
        self, loco: HostedLoco, entry: dict,
    ) -> None:
        """Write the persisted user_name + user_description into
        the loco's SPACE_CONFIG. Called from :meth:`restore_from_disk`
        so operators see the same labels they set before."""
        assert loco.memory_responder is not None
        buf = bytearray(CDI_CONFIG_TOTAL_BYTES)
        name = str(entry.get("user_name", "")).encode("utf-8")
        name = name[:CDI_USER_NAME_SIZE]
        buf[CDI_USER_NAME_OFFSET : CDI_USER_NAME_OFFSET + len(name)] = name
        desc = str(entry.get("user_description", "")).encode("utf-8")
        desc = desc[:CDI_USER_DESC_SIZE]
        buf[CDI_USER_DESC_OFFSET : CDI_USER_DESC_OFFSET + len(desc)] = desc
        loco.memory_responder.set_space(0xFE, bytes(buf))

    # ---- inbound frame dispatch -------------------------------------------

    async def _on_frame(self, frame: OlcbFrame) -> None:
        ft = extract_frame_type(frame.header)
        # AME (Alias Map Enquiry) is a control frame -- variable
        # field 0x702 -- not an addressed MTI, so it needs handling
        # before the FT_MTI gate.
        if ft == FT_CONTROL and extract_variable_field(frame.header) == CTRL_AME:
            await self._on_ame(frame)
            return
        if ft != FT_MTI:
            return
        mti = extract_mti(frame.header)
        # Global Verify NID (with matching data -> a specific loco
        # answers). Empty data would provoke every node on the bus
        # to answer -- we skip those here so we don't drown the bus
        # in verified replies from every hosted train.
        if mti == MTI_VERIFY_NID_GLOBAL:
            await self._on_verify_global(frame)
            return
        # Identify Producers with a Traction Search event -> reply
        # from every hosted loco whose address matches. Non-search
        # Identify Producers (targeting arbitrary event IDs the
        # operator wired up) are none of the CS's business.
        if mti == MTI_IDENTIFY_PRODUCERS:
            await self._on_traction_search(frame)
            return
        # Everything else is an addressed message -- assemble first.
        msg = self._reassembler.receive(frame)
        if msg is None:
            return
        loco = self._locos_by_alias.get(msg.dest_alias)
        if loco is None:
            return
        if msg.mti == MTI_VERIFY_NID_ADDRESSED:
            await self._send_verified_nid(loco, msg.source_alias)
            return
        if msg.mti == MTI_SNIP_REQUEST:
            await self._send_snip_reply(loco, msg.source_alias)
            return
        if msg.mti == 0x0828:   # PIP inquiry
            await self._send_pip_reply(loco, msg.source_alias)
            return
        if msg.mti == MTI_TRACTION_REQUEST:
            await self._on_traction_request(loco, msg)
            return

    async def _on_ame(self, frame: OlcbFrame) -> None:
        """Answer Alias Map Enquiry probes for hosted trains.

        AME semantics (S-9.7.0.1 sec.6.3.4):

        * Empty data -> every node answers with its AMD. Each hosted
          train qualifies as an independent node from the bus's
          perspective, so each replies. Yes, that's ``N`` replies
          for ``N`` hosted trains, but that's the spec -- peers rely
          on it to bulk-populate their alias<->NID maps.
        * 6-byte NID data -> only the node with matching NID replies.
          We check every hosted train's NodeID and answer from the
          matching one.
        """
        if not frame.data:
            for loco in self._locos_by_alias.values():
                await self._send_amd(loco)
            return
        if len(frame.data) >= 6:
            target_nid = NodeId(int.from_bytes(frame.data[:6], "big"))
            loco = self._locos_by_nid.get(target_nid)
            if loco is not None:
                await self._send_amd(loco)

    async def _send_amd(self, loco: HostedLoco) -> None:
        """AMD (Alias Map Definition) -- control frame variable
        ``0x701``, data = 6-byte NodeID, source alias = the hosted
        train's alias. Same frame shape as claim_alias's post-RID
        announcement."""
        await self._tc.send_frame(OlcbFrame(
            header=_pack_header(FT_CONTROL, CTRL_AMD, loco.alias),
            data=loco.node_id.bytes,
        ))

    async def _on_traction_search(self, frame: OlcbFrame) -> None:
        """Handle Identify Producers with a Traction Search event.

        Every hosted loco matching the search criteria emits a
        Producer Identified Valid reply from its own alias, echoing
        the exact search event ID so the client can correlate. A
        search that doesn't match any hosted loco is silently
        ignored -- the client's timeout window still expires
        normally, returning an empty result set."""
        if len(frame.data) < 8:
            return
        event = EventId(int.from_bytes(frame.data[:8], "big"))
        query = decode_search_event(event)
        if query is None:
            return
        for loco in self._locos_by_alias.values():
            if not query.matches(loco.address, loco.long_address):
                continue
            await self._tc.send_frame(OlcbFrame(
                header=_pack_header(
                    FT_MTI, MTI_PRODUCER_IDENT_VALID, loco.alias,
                ),
                data=event.bytes,
            ))

    async def _on_verify_global(self, frame: OlcbFrame) -> None:
        """Answer a Verify NID Global that carries a 6-byte NID
        matching one of our hosted trains. Empty-data global probes
        would draw a reply from every node; the CS deliberately
        stays silent for those and lets the connection's main
        VerifyNodeIdService cover its single canonical identity --
        otherwise a bus with 50 hosted locos would emit 51
        Verified NIDs on every empty probe."""
        if len(frame.data) < 6:
            return
        target_nid = NodeId(int.from_bytes(frame.data[:6], "big"))
        loco = self._locos_by_nid.get(target_nid)
        if loco is None:
            return
        await self._send_verified_nid(loco, source_alias=None)

    # ---- outbound replies -------------------------------------------------

    async def _send_verified_nid(
        self, loco: HostedLoco, source_alias: int | None,
    ) -> None:
        """Verified NID is a global broadcast (frame type
        ``FT_MTI``, MTI ``0x0170``, source alias = the loco's
        hosted alias, data = the loco's 6-byte NID). *source_alias*
        is the address of whoever asked but doesn't affect the
        reply shape -- Verified NID has no addressed prefix."""
        await self._tc.send_frame(OlcbFrame(
            header=_pack_header(FT_MTI, MTI_VERIFIED_NID, loco.alias),
            data=loco.node_id.bytes,
        ))

    async def _send_snip_reply(
        self, loco: HostedLoco, requester_alias: int,
    ) -> None:
        """SNIP reply with a user_name (from the CDI editor if set,
        else the canonical DCC-derived label) + description."""
        default_desc = (
            f"DCC loco address {loco.address} "
            f"({'long' if loco.long_address else 'short'})"
        )
        user_desc = loco.user_description or default_desc
        info = NodeInfo(
            manufacturer="JMRI-Python",
            model="DCC Train",
            hardware_version="",
            software_version="",
            user_name=loco.display_name,
            user_description=user_desc,
        )
        for reply in encode_addressed_frames(
            MTI_SNIP_REPLY, loco.alias, requester_alias, info.encode(),
        ):
            await self._tc.send_frame(reply)

    async def _send_pip_reply(
        self, loco: HostedLoco, requester_alias: int,
    ) -> None:
        """PIP reply: hosted train advertises Traction + SNIP + IDENT."""
        for reply in encode_addressed_frames(
            0x0668,   # MTI_PIP_REPLY
            loco.alias, requester_alias, encode_flags(self.train_pip_flags),
        ):
            await self._tc.send_frame(reply)

    # ---- Traction command dispatch ----------------------------------------

    async def _on_traction_request(
        self, loco: HostedLoco, msg: AddressedMessage,
    ) -> None:
        """Route one Traction Control Command onto the loco's DCC
        throttle bean. Assign Controller acquires the throttle;
        every subsequent command uses the acquired handle. If a
        loco isn't assigned yet, non-assign commands are silently
        ignored -- real hardware would emit a rejection, but the
        base API doesn't have a rejection path yet and the more
        common case is a controller that skipped Assign (which we
        should still tolerate by tacitly acquiring)."""
        if not msg.body:
            return
        # Any inbound traction command counts as a heartbeat tick --
        # the controller is present and talking. Timestamped for
        # external watchdogs (the CS itself is passive on silence).
        import time  # noqa: PLC0415
        loco.last_activity_at = time.monotonic()
        cmd = msg.body[0]
        if cmd == TC_MANAGEMENT:
            # Noop is the whole point -- bumping last_activity_at
            # above is the useful side effect. Any unknown sub-command
            # here is ignored (a loco -> CS Heartbeat Request would
            # come from a loco, not to us).
            return
        if cmd == TC_CONTROLLER_CONFIG:
            await self._on_controller_config(loco, msg)
            return
        if cmd == TC_CONSIST_CONFIG:
            # Consist config doesn't need a DCC throttle -- it's
            # bookkeeping on the CS. Handle before auto-acquire so
            # a throttle asking for consist membership on a loco we
            # never commanded doesn't spuriously acquire a slot.
            await self._on_consist_config(loco, msg)
            return
        # Auto-acquire for any speed/function command so a controller
        # that skipped Assign (some barebones throttles do this) still
        # sees the loco move.
        if loco.throttle is None:
            await self._acquire_throttle(loco)
        if cmd == TC_SET_SPEED_DIR:
            await self._on_set_speed(loco, msg.body)
        elif cmd == TC_SET_FUNCTION:
            await self._on_set_function(loco, msg.body)
        elif cmd == TC_EMERGENCY_STOP:
            await self._on_emergency_stop(loco)
        elif cmd == TC_QUERY_SPEED:
            await self._on_query_speed(loco, msg.source_alias)
        elif cmd == TC_QUERY_FUNCTION:
            await self._on_query_function(loco, msg)

    async def _acquire_throttle(self, loco: HostedLoco) -> None:
        loco.throttle = await self._dcc.acquire(
            loco.address, long_address=loco.long_address,
        )

    # ---- DCC-side consist bridge -----------------------------------------

    async def _bridge_attach(
        self, loco: HostedLoco, member: ConsistMember,
    ) -> None:
        """Forward one attached consist member to the DCC
        ``ConsistManager``. Silently no-ops when the CS wasn't
        configured with a DCC consist bridge, when the member's
        NodeID isn't a well-known train NID (foreign OpenLCB
        train), or when the DCC-side call raises -- we prefer to
        keep the OpenLCB layer working when the DCC side has a
        transient problem.

        Ensures a DCC consist exists at the head loco's DCC
        address before adding the member; ``add_member`` in the
        core :class:`ConsistManager` handles the underlying wire
        ops (LINK/UNLINK for LocoNet, CS-side consist for NCE,
        CV19 writes for advanced consists).
        """
        if self._dcc_consists is None:
            return
        member_dcc = train_node_id_to_dcc(member.node_id)
        if member_dcc is None:
            _log.debug(
                "consist attach: %s isn't a train NID; skipping DCC bridge",
                member.node_id.dotted(),
            )
            return
        member_addr, member_long = member_dcc
        try:
            if self._dcc_consists.get(loco.address) is None:
                await self._dcc_consists.create(loco.address)
            await self._dcc_consists.add_member(
                loco.address, member_addr,
                long_address=member_long,
                direction_normal=not member.reverse,
            )
        except Exception as e:  # noqa: BLE001
            _log.warning(
                "consist attach: DCC add_member(%d, %d) failed: %s",
                loco.address, member_addr, e,
            )

    async def _bridge_detach(
        self, loco: HostedLoco, member_nid: NodeId,
    ) -> None:
        """Remove one member from the DCC consist. If the resulting
        consist has no non-lead members left, the OpenLCB layer
        still tracks the head loco but the DCC consist is
        implicitly dissolved on the next :meth:`remove_loco`."""
        if self._dcc_consists is None:
            return
        member_dcc = train_node_id_to_dcc(member_nid)
        if member_dcc is None:
            return
        member_addr, _ = member_dcc
        try:
            if self._dcc_consists.get(loco.address) is not None:
                await self._dcc_consists.remove_member(
                    loco.address, member_addr,
                )
        except Exception as e:  # noqa: BLE001
            _log.warning(
                "consist detach: DCC remove_member(%d, %d) failed: %s",
                loco.address, member_addr, e,
            )

    async def _bridge_dissolve(self, loco: HostedLoco) -> None:
        """Delete the DCC consist backing *loco*. Called on
        :meth:`remove_loco` so the DCC command station's consist
        table stays in sync with the OpenLCB layer."""
        if self._dcc_consists is None:
            return
        if self._dcc_consists.get(loco.address) is None:
            return
        try:
            await self._dcc_consists.delete(loco.address)
        except Exception as e:  # noqa: BLE001
            _log.warning(
                "consist dissolve: DCC delete(%d) failed: %s",
                loco.address, e,
            )

    async def _on_controller_config(
        self, loco: HostedLoco, msg: AddressedMessage,
    ) -> None:
        if len(msg.body) < 2:
            return
        sub = msg.body[1]
        if sub == CC_ASSIGN and len(msg.body) >= 9:
            # Byte 2 is flags (usually 0); NID starts at byte 3.
            loco.controller = NodeId(int.from_bytes(msg.body[3:9], "big"))
            if loco.throttle is None:
                await self._acquire_throttle(loco)
            return
        if sub == CC_RELEASE and len(msg.body) >= 9:
            loco.controller = NodeId(0)
            if loco.throttle is not None:
                try:
                    await self._dcc.release(loco.throttle)
                except Exception as e:  # noqa: BLE001
                    _log.warning(
                        "releasing throttle for %s failed: %s",
                        loco.canonical_name, e,
                    )
                loco.throttle = None
            return
        if sub == CC_QUERY:
            reply_body = (
                bytes([TC_CONTROLLER_CONFIG, CC_QUERY, 0x00])
                + loco.controller.bytes
            )
            for reply in encode_addressed_frames(
                MTI_TRACTION_REPLY, loco.alias, msg.source_alias, reply_body,
            ):
                await self._tc.send_frame(reply)

    async def _on_consist_config(
        self, loco: HostedLoco, msg: AddressedMessage,
    ) -> None:
        """Consist Configuration sub-commands.

        The in-memory ``consist_members`` list always tracks what
        the OpenLCB throttle asked for. When ``dcc_consists`` was
        passed to the CS at construction, Attach/Detach also drive
        the underlying DCC consist -- the head loco's DCC address
        becomes the DCC consist address, and members are added by
        their translated DCC addresses. Member NIDs outside the
        well-known train range (e.g. a native-OpenLCB loco) are
        still added to the in-memory list but skipped for DCC
        forwarding since we can't map them to a DCC address.
        """
        if len(msg.body) < 2:
            return
        sub = msg.body[1]
        if sub == CONSIST_ATTACH:
            # Layout: 0x30, 0x01, flags(2), NID(6) = 10 bytes.
            if len(msg.body) < 10:
                return
            hidden, reverse = decode_consist_flags(msg.body[2:4])
            member_nid = NodeId(int.from_bytes(msg.body[4:10], "big"))
            new_member = ConsistMember(
                node_id=member_nid, hidden=hidden, reverse=reverse,
            )
            # Idempotent: re-attach of an existing member updates
            # the flags, doesn't duplicate.
            existing_idx: int | None = None
            for i, existing in enumerate(loco.consist_members):
                if existing.node_id == member_nid:
                    existing_idx = i
                    break
            if existing_idx is not None:
                loco.consist_members[existing_idx] = new_member
            else:
                loco.consist_members.append(new_member)
            await self._bridge_attach(loco, new_member)
            return
        if sub == CONSIST_DETACH:
            # Layout: 0x30, 0x02, NID(6) = 8 bytes.
            if len(msg.body) < 8:
                return
            member_nid = NodeId(int.from_bytes(msg.body[2:8], "big"))
            loco.consist_members = [
                m for m in loco.consist_members if m.node_id != member_nid
            ]
            await self._bridge_detach(loco, member_nid)
            return
        if sub == CONSIST_QUERY:
            # Layout: 0x30, 0x03, index(1) = 3 bytes.
            if len(msg.body) < 3:
                return
            index = msg.body[2]
            size = len(loco.consist_members)
            if index >= size:
                # Out of range -> sentinel index, zero flags/NID.
                reply_body = (
                    bytes([TC_CONSIST_CONFIG, CONSIST_QUERY, size,
                           CONSIST_INDEX_INVALID])
                    + bytes(2)   # zero flags
                    + bytes(6)   # zero NID
                )
            else:
                member = loco.consist_members[index]
                reply_body = (
                    bytes([TC_CONSIST_CONFIG, CONSIST_QUERY, size, index])
                    + encode_consist_flags(member)
                    + member.node_id.bytes
                )
            for reply in encode_addressed_frames(
                MTI_TRACTION_REPLY, loco.alias, msg.source_alias, reply_body,
            ):
                await self._tc.send_frame(reply)

    async def _on_set_speed(self, loco: HostedLoco, body: bytes) -> None:
        if len(body) < 3 or loco.throttle is None:
            return
        mph, forward = decode_speed(body[1:3])
        # Track direction on the throttle bean, then feed the
        # normalized magnitude to set_speed.
        direction = ThrottleDirection.FORWARD if forward else ThrottleDirection.REVERSE
        if loco.throttle.direction != direction:
            await loco.throttle.set_direction(direction)
        # Clamp normalized speed to [0, 1] -- a poorly-behaved
        # controller could send more than MPH_PER_UNIT and the base
        # Throttle bean would reject it. Silently clamping preserves
        # the "loco keeps moving" invariant.
        normalized = min(1.0, max(0.0, mph / MPH_PER_UNIT))
        await loco.throttle.set_speed(normalized)

    async def _on_set_function(self, loco: HostedLoco, body: bytes) -> None:
        # Layout: 0x01, fn(3), value(2). Total 6 bytes minimum.
        if len(body) < 6 or loco.throttle is None:
            return
        function = int.from_bytes(body[1:4], "big")
        value = int.from_bytes(body[4:6], "big")
        # Core Throttle only supports F0..F31; silently drop
        # requests for higher functions rather than raising, so a
        # controller commanding F32+ doesn't break the whole session.
        from jmri_core.beans.throttle import NUM_FUNCTIONS  # noqa: PLC0415
        if function >= NUM_FUNCTIONS:
            _log.debug(
                "dropping F%d for %s (out of Throttle bean range)",
                function, loco.canonical_name,
            )
            return
        await loco.throttle.set_function(function, value != 0)

    async def _on_emergency_stop(self, loco: HostedLoco) -> None:
        if loco.throttle is None:
            return
        await loco.throttle.emergency_stop()

    async def _on_query_speed(
        self, loco: HostedLoco, requester_alias: int,
    ) -> None:
        # Read from the bean's current state so we don't have to
        # duplicate speed tracking. Reply layout: cmd(1) + set(2)
        # + status(1) + commanded(2 = NaN) + actual(2 = NaN).
        # NaN in commanded/actual signals "not tracked" per S-9.7.4.7.
        import struct  # noqa: PLC0415
        import math    # noqa: PLC0415
        speed = 0.0
        forward = True
        if loco.throttle is not None:
            speed = abs(loco.throttle.speed) * MPH_PER_UNIT
            forward = loco.throttle.direction == ThrottleDirection.FORWARD
        nan = struct.pack(">e", math.nan)
        reply_body = (
            bytes([TC_QUERY_SPEED])
            + encode_speed(speed, forward=forward)
            + bytes([0x00])   # status
            + nan             # commanded (not tracked)
            + nan             # actual (not tracked)
        )
        for reply in encode_addressed_frames(
            MTI_TRACTION_REPLY, loco.alias, requester_alias, reply_body,
        ):
            await self._tc.send_frame(reply)

    async def _on_query_function(
        self, loco: HostedLoco, msg: AddressedMessage,
    ) -> None:
        # Layout: 0x11, fn(3). Reply: 0x11, fn(3), value(2).
        if len(msg.body) < 4:
            return
        function = int.from_bytes(msg.body[1:4], "big")
        value = 0
        if loco.throttle is not None:
            from jmri_core.beans.throttle import NUM_FUNCTIONS  # noqa: PLC0415
            if 0 <= function < NUM_FUNCTIONS:
                value = 1 if loco.throttle.functions[function] else 0
        reply_body = (
            bytes([TC_QUERY_FUNCTION])
            + function.to_bytes(3, "big")
            + value.to_bytes(2, "big")
        )
        for reply in encode_addressed_frames(
            MTI_TRACTION_REPLY, loco.alias, msg.source_alias, reply_body,
        ):
            await self._tc.send_frame(reply)


# ---- typing shim --------------------------------------------------------


class _TcLike:
    """Same structural shape as :class:`~jmri_openlcb.snip.SnipTrafficLike`."""
