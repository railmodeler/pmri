"""``OlcbConnection`` -- SystemConnection subclass for OpenLCB/LCC.

Settings::

    {
        "kind": "loopback" | "serial" | "tcp",
        "node_id": "02.01.12.FE.DC.BA",   # 48-bit Node ID, dotted hex or int
        "node_info": {                    # optional; SNIP identity strings
            "manufacturer": "JMRI-Python",
            "model": "JMRI Server",
            "hardware_version": "linux-arm64",
            "software_version": "0.1.0",
            "user_name": "",
            "user_description": "",
        },
        # skip_alias_claim: bool          # loopback tests only -- default False
        # source_alias: int               # override candidate; skip_alias_claim only
        # ...transport-specific...
    }

Node ID is required for real hardware. Priority: an explicit
``node_id`` in settings wins; else we load from a persisted file
(``<profile>/openlcb-node-id`` by default, overridable via
``settings["node_id_path"]``); else we mint a random dev-range ID
and save it so subsequent restarts reuse the same value. That
keeps the alias stable across boots without requiring the
operator to hand-pick a Node ID.
"""
from __future__ import annotations

import logging
import secrets
from typing import Any, ClassVar

from pmri_core.hal.connection import ConnectionSettings, SystemConnection

from pmri_openlcb.alias import (
    DEV_RANGE_TOP_BYTE,
    AliasReleaseWatcher,
    AmeResponder,
    NodeId,
    claim_alias,
)
from pmri_openlcb.beans import OlcbSensorManager, OlcbTurnoutManager
from pmri_openlcb.datagrams import DatagramEngine
from pmri_openlcb.duplicate_nid import DuplicateNodeIdWatcher
from pmri_openlcb.event_ident import EventRoster, ProducerConsumerService
from pmri_openlcb.pcer_history import PcerHistory
from pmri_openlcb.memory import (
    SPACE_CDI,
    SPACE_CONFIG,
    MemoryClient,
    MemoryConfigResponder,
)
from pmri_openlcb.monitor import DEFAULT_MONITOR_SIZE, FrameMonitor
from pmri_openlcb.pip import PipClient, PipService
from pmri_openlcb.power import OlcbPowerManager
from pmri_openlcb.snip import NodeInfo, SnipClient, SnipResponder
from pmri_openlcb.stale import (
    DEFAULT_STALE_THRESHOLD,
    DEFAULT_SWEEP_INTERVAL,
    LastSeenTracker,
    StalePeerSweeper,
)
from pmri_openlcb.throttle import OlcbThrottleManager
from pmri_openlcb.traction import TractionClient
from pmri_openlcb.traction_search import TractionSearchClient
from pmri_openlcb.traffic import OlcbTrafficController
from pmri_openlcb.transport import BytePort, LoopbackPort, SerialPort, TcpPort
from pmri_openlcb.verify import NodeIdLookup, VerifyNodeIdService

_log = logging.getLogger(__name__)


class OlcbConnection(SystemConnection):
    name: ClassVar[str] = "OpenLCB"
    prefix: ClassVar[str] = "X"

    def __init__(self) -> None:
        super().__init__()
        self._port: BytePort | None = None
        self._tc: OlcbTrafficController | None = None
        self._ame: AmeResponder | None = None
        self._node_id: NodeId | None = None
        self._node_info: NodeInfo | None = None
        self._node_info_persist_path: "pathlib.Path | None" = None
        self._snip_responder: SnipResponder | None = None
        self._snip_client: SnipClient | None = None
        self._pc_service: ProducerConsumerService | None = None
        self._event_roster: EventRoster | None = None
        self._verify_service: VerifyNodeIdService | None = None
        self._node_id_lookup: NodeIdLookup | None = None
        self._traction_client: TractionClient | None = None
        self._traction_search: TractionSearchClient | None = None
        self._pip_service: PipService | None = None
        self._pip_client: PipClient | None = None
        self._datagram_engine: DatagramEngine | None = None
        self._memory_responder: MemoryConfigResponder | None = None
        self._frame_monitor: FrameMonitor | None = None
        self._alias_release: AliasReleaseWatcher | None = None
        self._last_seen: LastSeenTracker | None = None
        self._stale_sweeper: StalePeerSweeper | None = None
        self._duplicate_nid: DuplicateNodeIdWatcher | None = None
        self._pcer_history: PcerHistory | None = None
        # Cached at open time so reclaim() can persist a new NodeID
        # to the same location without re-parsing settings.
        self._node_id_persist_path: "pathlib.Path | None" = None

    async def open(self, settings: ConnectionSettings | None = None) -> None:
        s = settings or {}
        self._port = await _build_port(s)
        self._node_id = _resolve_node_id(s)
        self._node_id_persist_path = _resolve_persist_path(s)

        skip_claim = bool(s.get("skip_alias_claim", False))
        initial_alias: int | None = None
        if skip_claim:
            # Loopback tests + a few internal call-sites bypass the
            # handshake entirely; they must supply the alias directly.
            initial_alias = int(s.get("source_alias", 0x123))

        self._tc = OlcbTrafficController(
            self._port, source_alias=initial_alias,
        )
        await self._tc.start()

        if not skip_claim:
            # Real hardware waits 200 ms per S-9.7.0.1; tests can
            # override via ``claim_wait`` to keep the suite snappy.
            wait = float(s.get("claim_wait", 0.2))
            alias = await claim_alias(self._tc, self._node_id, wait=wait)
            self._tc.source_alias = alias

        # Answer AME probes so other nodes can resolve our alias -> NID.
        self._ame = AmeResponder(
            self._tc, self._node_id, self._tc.source_alias,
        )
        # Answer SNIP probes with our node identity strings; expose a
        # client so an operator-facing discovery UI can probe peers.
        # Resolve advertised SNIP identity. Precedence:
        #   1. Explicit ``settings["node_info"]`` user_name /
        #      user_description (config-file wins -- deterministic
        #      startup for a caller managing config out-of-band).
        #   2. Sidecar persistence file -- the operator's web edits
        #      that need to survive a restart.
        #   3. Empty strings from :func:`_resolve_node_info`.
        raw_info = s.get("node_info") or {}
        self._node_info = _resolve_node_info(s)
        self._node_info_persist_path = _resolve_snip_persist_path(s)
        persisted_snip = _load_persisted_snip(self._node_info_persist_path)
        if persisted_snip is not None:
            import dataclasses  # noqa: PLC0415
            fields: dict[str, str] = {}
            if "user_name" not in raw_info and "user_name" in persisted_snip:
                fields["user_name"] = persisted_snip["user_name"]
            if (
                "user_description" not in raw_info
                and "user_description" in persisted_snip
            ):
                fields["user_description"] = persisted_snip["user_description"]
            if fields:
                self._node_info = dataclasses.replace(
                    self._node_info, **fields,
                )
        # Pass a lambda so probe replies always read current state --
        # ``update_snip`` mutates ``self._node_info`` and the next
        # probe sees it without any responder reset.
        self._snip_responder = SnipResponder(
            self._tc, lambda: self._node_info,
        )
        self._snip_client = SnipClient(self._tc)
        # Producer/Consumer identification -- the service answers
        # Identify {Producers,Consumers,Events} probes about our
        # events; the roster tracks what every other peer produces
        # and consumes. Constructed *before* the bean managers so
        # they can register each configured bean's events with it
        # automatically.
        self._pc_service = ProducerConsumerService(self._tc)
        self._event_roster = EventRoster(self._tc)
        # Verify Node ID -- answer peers asking who we are, and offer
        # a client to build the alias<->NodeId map. Uses the same
        # Node ID we claimed our alias with, so peers see a
        # consistent identity.
        self._verify_service = VerifyNodeIdService(self._tc, self._node_id)
        self._node_id_lookup = NodeIdLookup(self._tc)
        # PIP -- advertise which OpenLCB protocols we implement so
        # peers know what to expect. Uses the default JMRI-Python
        # flag set (Datagram + Memory Config + Event Exchange +
        # Identification + SNIP + CDI); a specialised node can call
        # ``pip_service.set_flags(...)`` to override.
        self._pip_service = PipService(self._tc)
        self._pip_client = PipClient(self._tc)
        # Traction -- command loco nodes over the bus. Client-side
        # only for now; a throttle bean layer on top comes separately.
        self._traction_client = TractionClient(self._tc)
        self._traction_search = TractionSearchClient(self._tc)
        # Replace the base ThrottleManager with an OpenLCB-aware one
        # so ``connection.throttles.acquire(3)`` resolves the DCC
        # address to a train NodeID, finds its current alias, and
        # sends Assign Controller -- all transparent to the caller.
        self.throttles = OlcbThrottleManager(  # type: ignore[assignment]
            self.prefix,
            client=self._traction_client,
            lookup=self._node_id_lookup,
            our_node_id=self._node_id,
        )

        self.turnouts = OlcbTurnoutManager(  # type: ignore[assignment]
            self.prefix, tc=self._tc, pc_service=self._pc_service,
        )
        self.sensors = OlcbSensorManager(    # type: ignore[assignment]
            self.prefix, tc=self._tc, pc_service=self._pc_service,
        )
        # Well-known event power manager -- produces + consumes the
        # reserved 01.00.00.00.00.00.FF.FE / .FF event IDs so a
        # single set_power() call converges every OpenLCB node
        # listening to the bus. See :mod:`pmri_openlcb.power`.
        self.power = OlcbPowerManager(self._tc)

        # Memory-config responder -- makes our advertised PIP
        # MEMORY_CONFIG flag true. Pre-loads SPACE_CDI with either
        # a caller-supplied XML (settings["cdi_xml"]) or a minimal
        # stub identifying this as a JMRI-Python node; SPACE_CONFIG
        # gets a caller-configured size (default 256 bytes, all
        # zeros) so ``read``/``write`` don't 404 immediately. Set
        # ``skip_memory_responder=True`` to disable if a caller
        # wants to stand up their own responder differently.
        if not s.get("skip_memory_responder", False):
            self._datagram_engine = DatagramEngine(self._tc)
            await self._datagram_engine.attach()
            # Passing our NodeID bytes lets the responder enforce the
            # Factory Reset safety check per S-9.10.1 sec.3.3 -- a peer
            # can only factory-reset us if the payload matches our
            # NodeID exactly.
            self._memory_responder = MemoryConfigResponder(
                self._datagram_engine,
                our_node_id_bytes=self._node_id.bytes,
            )
            self._memory_responder.set_space(
                SPACE_CDI, _resolve_cdi_xml(s), read_only=True,
            )
            self._memory_responder.set_space(
                SPACE_CONFIG,
                bytes(int(s.get("config_space_bytes", 256))),
            )

        # Frame monitor -- rolling window of recent inbound frames
        # for the bus-monitor UI. Set ``skip_monitor=True`` to
        # opt out (a stress-test setup that doesn't want the
        # per-frame bookkeeping overhead).
        if not s.get("skip_monitor", False):
            self._frame_monitor = FrameMonitor(
                self._tc,
                max_entries=int(s.get("monitor_max_entries", DEFAULT_MONITOR_SIZE)),
            )

        # AMR watcher -- when a peer cleanly releases its alias, drop
        # every entry we hold keyed by that alias. Peer callers can
        # add extra callbacks (a SNIP/PIP cache on the service layer
        # subscribes here) via ``alias_release.on_release(cb)``.
        self._alias_release = AliasReleaseWatcher(self._tc)
        self._alias_release.on_release(self._node_id_lookup.forget)
        self._alias_release.on_release(self._event_roster.forget)

        # PCER history -- per-peer event count + last-seen. Wired
        # into the release fan-out so a peer that goes away drops
        # from history alongside its other cached data.
        self._pcer_history = PcerHistory(self._tc)
        self._alias_release.on_release(self._pcer_history.forget)

        # Duplicate-NodeID watcher -- if a peer alias reports our own
        # 48-bit NodeID (via AMD or Verified NID), we've got a spec
        # violation. Broadcast the well-known Duplicate NID event
        # and fan out to callbacks so the caller decides whether to
        # shut down. Skipped when ``skip_duplicate_nid_watch`` is
        # set (a bench test intentionally running two nodes with
        # the same NID for protocol testing wants the observation
        # suppressed).
        if not s.get("skip_duplicate_nid_watch", False):
            self._duplicate_nid = DuplicateNodeIdWatcher(
                self._tc, self._node_id,
            )
            # Auto-reclaim: mint a fresh NID and re-claim silently
            # when a collision is seen. Disabled by default because
            # it changes our identity -- the operator generally wants
            # to know a duplicate happened and fix the config that
            # caused it.
            if s.get("auto_reclaim_on_duplicate", False):
                self._duplicate_nid.on_duplicate(
                    self._schedule_auto_reclaim,
                )

        # Stale-peer sweeper -- nodes that lose power skip AMR and
        # just vanish. The tracker records per-alias last-seen time
        # from every inbound frame; the sweeper prunes anyone quiet
        # past the threshold, fanning out through the same release
        # callbacks as AMR. Set ``skip_stale_sweep=True`` for stress
        # tests that don't want the background task; the tracker
        # still runs so ``last_seen`` queries stay accurate.
        self._last_seen = LastSeenTracker(self._tc)
        self._alias_release.on_release(self._last_seen.forget)
        if not s.get("skip_stale_sweep", False):
            self._stale_sweeper = StalePeerSweeper(
                self._last_seen,
                self._alias_release,
                threshold=float(
                    s.get("stale_threshold", DEFAULT_STALE_THRESHOLD),
                ),
                interval=float(
                    s.get("sweep_interval", DEFAULT_SWEEP_INTERVAL),
                ),
            )
            self._stale_sweeper.start()

        # Announce Node Initialization Complete (S-9.7.3 sec.5) so
        # peers can populate their rosters without a Verify NID
        # sweep. Emitted as a PCER; the source alias in the header
        # identifies us, the event ID identifies the state ("we
        # just finished initializing"). Skipped when
        # ``skip_init_complete=True`` -- bench scenarios sometimes
        # want a silent boot, and loopback tests avoid the extra
        # frame in captures they inspect.
        if not s.get("skip_init_complete", False):
            from pmri_openlcb.events import (  # noqa: PLC0415
                EVENT_INIT_COMPLETE,
                EVENT_INIT_COMPLETE_SIMPLE,
            )
            # We advertise the SIMPLE PIP subset by default, so the
            # "simple" variant is the correct announcement. A caller
            # advertising the full protocol set can override with
            # ``init_complete_event="full"``.
            variant = s.get("init_complete_event", "simple")
            event = (
                EVENT_INIT_COMPLETE if variant == "full"
                else EVENT_INIT_COMPLETE_SIMPLE
            )
            await self._tc.produce_event(event)

        await super().open(settings)

    async def close(self) -> None:
        # Drop the power manager's listener before we tear down the
        # traffic controller so the manager doesn't fire against a
        # closed signal.
        if isinstance(self.power, OlcbPowerManager):
            self.power.close()
        if self._pcer_history is not None:
            self._pcer_history.close()
            self._pcer_history = None
        if self._duplicate_nid is not None:
            self._duplicate_nid.close()
            self._duplicate_nid = None
        if self._stale_sweeper is not None:
            await self._stale_sweeper.close()
            self._stale_sweeper = None
        if self._last_seen is not None:
            self._last_seen.close()
            self._last_seen = None
        if self._alias_release is not None:
            self._alias_release.close()
            self._alias_release = None
        if self._frame_monitor is not None:
            self._frame_monitor.close()
            self._frame_monitor = None
        if self._memory_responder is not None:
            self._memory_responder.close()
            self._memory_responder = None
        if self._datagram_engine is not None:
            await self._datagram_engine.detach()
            self._datagram_engine = None
        if self._traction_search is not None:
            self._traction_search.close()
            self._traction_search = None
        if self._traction_client is not None:
            self._traction_client.close()
            self._traction_client = None
        if self._pip_client is not None:
            self._pip_client.close()
            self._pip_client = None
        if self._pip_service is not None:
            self._pip_service.close()
            self._pip_service = None
        if self._node_id_lookup is not None:
            self._node_id_lookup.close()
            self._node_id_lookup = None
        if self._verify_service is not None:
            self._verify_service.close()
            self._verify_service = None
        if self._event_roster is not None:
            self._event_roster.close()
            self._event_roster = None
        if self._pc_service is not None:
            self._pc_service.close()
            self._pc_service = None
        if self._snip_client is not None:
            self._snip_client.close()
            self._snip_client = None
        if self._snip_responder is not None:
            self._snip_responder.close()
            self._snip_responder = None
        if self._ame is not None:
            self._ame.close()
            self._ame = None
        if self._tc is not None:
            await self._tc.aclose()
            self._tc = None
        self._port = None
        await super().close()

    @property
    def node_id(self) -> NodeId:
        if self._node_id is None:
            raise RuntimeError("connection not open")
        return self._node_id

    # ---- re-claim ----------------------------------------------------

    async def reclaim(
        self, new_node_id: NodeId | None = None,
        *, claim_wait: float = 0.05,
    ) -> NodeId:
        """Release our current alias, pick a new NodeID (or use
        *new_node_id* if given), re-run the CID/RID handshake, and
        rebuild the AME responder against the new alias/NID.

        The default is to mint a fresh dev-range NID -- the whole
        point of a re-claim is that the current NID collided with
        a peer's, so keeping it would just repeat the problem. Pass
        an explicit *new_node_id* when the operator has picked one
        manually (e.g. from a serial number bay).

        Persists the new NodeID to the same file
        :meth:`open` loaded / saved it to, so a subsequent restart
        picks up the fresh identity. Returns the new NodeID."""
        import contextlib  # noqa: PLC0415
        if self._tc is None:
            raise RuntimeError("connection not open")
        from pmri_openlcb.codec import OlcbFrame, make_amr_header  # noqa: PLC0415
        # 1. Send AMR so peers drop their cached (alias -> old NID)
        #    mapping cleanly. Best-effort -- a transport failure here
        #    doesn't block the re-claim.
        old_alias = self._tc.source_alias
        with contextlib.suppress(Exception):
            await self._tc.send_frame(OlcbFrame(
                header=make_amr_header(old_alias), data=b"",
            ))
        # 2. Tear down the AME responder -- it captured the old
        #    alias/NID at construction time.
        if self._ame is not None:
            self._ame.close()
            self._ame = None
        # 3. Decide on the new NodeID.
        if new_node_id is None:
            body = secrets.randbits(40)
            new_node_id = NodeId((DEV_RANGE_TOP_BYTE << 40) | body)
        # 4. Run the CID/RID handshake with the new NID.
        new_alias = await claim_alias(
            self._tc, new_node_id, wait=claim_wait,
        )
        self._tc.source_alias = new_alias
        self._node_id = new_node_id
        # 5. Rebuild the AME responder with the new identity.
        self._ame = AmeResponder(
            self._tc, self._node_id, self._tc.source_alias,
        )
        # 6. Update the duplicate-NID watcher's baseline so it
        #    doesn't fire against our new (and definitely valid)
        #    identity, and persist for the next boot.
        if self._duplicate_nid is not None:
            self._duplicate_nid._our_nid_bytes = new_node_id.bytes  # noqa: SLF001
        if self._node_id_persist_path is not None:
            _save_node_id(self._node_id_persist_path, new_node_id)
        _log.warning(
            "OpenLCB: re-claimed alias %#05x with NodeID %s "
            "(was alias %#05x)",
            new_alias, new_node_id.dotted(), old_alias,
        )
        return new_node_id

    def _schedule_auto_reclaim(self, peer_alias: int) -> None:
        """Fire-and-forget task that runs :meth:`reclaim` when the
        duplicate-NID watcher detects a collision. Wrapped as a
        task so the watcher's synchronous fan-out doesn't block on
        the async claim handshake."""
        import asyncio  # noqa: PLC0415
        _log.warning(
            "OpenLCB: auto-reclaim triggered by duplicate NID from "
            "peer alias %#05x", peer_alias,
        )
        asyncio.create_task(
            self.reclaim(), name="olcb-auto-reclaim",
        )

    def update_snip(
        self,
        *,
        user_name: str | None = None,
        user_description: str | None = None,
    ) -> NodeInfo:
        """Update our advertised SNIP identity. Only user_name /
        user_description are mutable per S-9.7.4.5 sec.4 (manufacturer,
        model, hardware/software versions are vendor-fixed).

        Persists to the sidecar file so the edit survives a
        restart. Returns the new NodeInfo. Peers see the change on
        their next SNIP probe -- no explicit re-advertisement is
        needed."""
        import dataclasses  # noqa: PLC0415
        if self._node_info is None:
            raise RuntimeError("connection not open")
        changes: dict[str, str] = {}
        if user_name is not None:
            changes["user_name"] = user_name
        if user_description is not None:
            changes["user_description"] = user_description
        if not changes:
            return self._node_info
        self._node_info = dataclasses.replace(self._node_info, **changes)
        if self._node_info_persist_path is not None:
            _save_snip(self._node_info_persist_path, self._node_info)
        return self._node_info

    @property
    def node_info(self) -> NodeInfo:
        """The SNIP identity block this node advertises."""
        if self._node_info is None:
            raise RuntimeError("connection not open")
        return self._node_info

    @property
    def snip_client(self) -> SnipClient:
        """The SNIP probe client -- call ``probe(alias)`` on it to
        fetch another node's SNIP."""
        if self._snip_client is None:
            raise RuntimeError("connection not open")
        return self._snip_client

    @property
    def pc_service(self) -> ProducerConsumerService:
        """Our produced/consumed event advertiser. Register events on
        this via ``register_produced(event)`` / ``register_consumed(event)``
        so peers probing the bus find them."""
        if self._pc_service is None:
            raise RuntimeError("connection not open")
        return self._pc_service

    @property
    def event_roster(self) -> EventRoster:
        """Peer roster -- call ``discover()`` to populate, then query
        via ``producers_of(event)`` / ``consumers_of(event)``."""
        if self._event_roster is None:
            raise RuntimeError("connection not open")
        return self._event_roster

    @property
    def node_id_lookup(self) -> NodeIdLookup:
        """Alias<->NodeId lookup -- call ``verify_all()`` to sweep the
        bus, then query with ``get(alias)`` / ``by_nid(nid)``."""
        if self._node_id_lookup is None:
            raise RuntimeError("connection not open")
        return self._node_id_lookup

    @property
    def pip_service(self) -> PipService:
        """PIP responder -- auto-answers Protocol Identification
        inquiries. Update the advertised flag set via
        ``pip_service.set_flags(...)``."""
        if self._pip_service is None:
            raise RuntimeError("connection not open")
        return self._pip_service

    @property
    def pip_client(self) -> PipClient:
        """PIP inquiry client -- ``probe(alias)`` returns the peer's
        ``ProtocolFlag`` mask."""
        if self._pip_client is None:
            raise RuntimeError("connection not open")
        return self._pip_client

    @property
    def memory_responder(self) -> MemoryConfigResponder:
        """Serve inbound memory-config reads/writes for our alias.
        Pre-loaded with SPACE_CDI (read-only) + SPACE_CONFIG. Update
        the CDI via ``memory_responder.set_space(SPACE_CDI, xml)``
        when the operator saves new content."""
        if self._memory_responder is None:
            raise RuntimeError(
                "connection not open or memory responder disabled "
                "(skip_memory_responder=True in settings)",
            )
        return self._memory_responder

    @property
    def last_seen(self) -> LastSeenTracker:
        """Per-alias last-seen timestamps for every peer whose frames
        we've observed. Never raises for aliases we haven't heard
        from -- those simply aren't in the map."""
        if self._last_seen is None:
            raise RuntimeError("connection not open")
        return self._last_seen

    @property
    def stale_sweeper(self) -> StalePeerSweeper:
        """Background sweeper that prunes peers idle past the
        threshold. Errors if the sweeper was disabled at open time
        via ``skip_stale_sweep=True``."""
        if self._stale_sweeper is None:
            raise RuntimeError(
                "connection not open or stale sweeper disabled "
                "(skip_stale_sweep=True in settings)",
            )
        return self._stale_sweeper

    @property
    def pcer_history(self) -> PcerHistory:
        """Per-peer PCER count + last-seen tracker. Query via
        ``.for_alias(alias)``."""
        if self._pcer_history is None:
            raise RuntimeError("connection not open")
        return self._pcer_history

    @property
    def duplicate_nid(self) -> DuplicateNodeIdWatcher:
        """Duplicate-NodeID watcher -- subscribe via
        ``on_duplicate(cb)`` to react when a peer alias reports our
        own 48-bit NodeID. Errors if the watcher was disabled at
        open time via ``skip_duplicate_nid_watch=True``."""
        if self._duplicate_nid is None:
            raise RuntimeError(
                "connection not open or duplicate-NID watcher "
                "disabled (skip_duplicate_nid_watch=True)",
            )
        return self._duplicate_nid

    @property
    def alias_release(self) -> AliasReleaseWatcher:
        """AMR watcher -- call ``on_release(cb)`` to register a
        callback that fires with a released peer alias so cross-
        layer caches (SNIP, PIP, discovery projections) can drop
        the dead entry."""
        if self._alias_release is None:
            raise RuntimeError("connection not open")
        return self._alias_release

    @property
    def frame_monitor(self) -> FrameMonitor:
        """Rolling-window record of recent inbound frames -- powers
        the bus-monitor UI. Errors if the monitor was disabled at
        open time via ``skip_monitor=True``."""
        if self._frame_monitor is None:
            raise RuntimeError(
                "connection not open or frame monitor disabled "
                "(skip_monitor=True in settings)",
            )
        return self._frame_monitor

    @property
    def traction(self) -> TractionClient:
        """Traction protocol client -- command loco nodes on the bus
        with ``set_speed`` / ``set_function`` / ``emergency_stop`` /
        ``assign_controller`` / ``query_speed`` / ``query_function`` /
        ``query_controller``."""
        if self._traction_client is None:
            raise RuntimeError("connection not open")
        return self._traction_client

    @property
    def traction_search(self) -> TractionSearchClient:
        """Traction Search client -- ``search(address)`` finds loco
        nodes by DCC address across every CS on the bus."""
        if self._traction_search is None:
            raise RuntimeError("connection not open")
        return self._traction_search

    @property
    def traffic(self) -> OlcbTrafficController:
        if self._tc is None:
            raise RuntimeError("connection not open")
        return self._tc


_DEFAULT_CDI_XML = """\
<?xml version="1.0"?>
<cdi>
  <identification>
    <manufacturer>JMRI-Python</manufacturer>
    <model>JMRI Server</model>
    <hardwareVersion>python</hardwareVersion>
    <softwareVersion>0.1.0</softwareVersion>
  </identification>
</cdi>
"""


def _resolve_cdi_xml(settings: dict[str, Any]) -> bytes:
    """Return the CDI XML bytes to serve from SPACE_CDI.

    Accepts either ``settings["cdi_xml"]`` (str or bytes) or falls
    back to a minimal identification-only stub -- enough that a peer
    fetching our CDI gets a valid document rather than a truncated
    read timeout. Every CDI document is null-terminated on the
    wire, so the trailing 0 byte is appended if not present."""
    raw = settings.get("cdi_xml")
    if raw is None:
        data = _DEFAULT_CDI_XML.encode("utf-8")
    elif isinstance(raw, bytes):
        data = raw
    else:
        data = str(raw).encode("utf-8")
    if not data.endswith(b"\x00"):
        data = data + b"\x00"
    return data


_DEFAULT_SNIP_PERSIST_FILENAME = "openlcb-node-snip.json"


def _resolve_snip_persist_path(settings: dict[str, Any]) -> "pathlib.Path":
    """Return the sidecar path where operator SNIP edits are saved.
    Defaults to a ``openlcb-node-snip.json`` file next to the
    node-id persistence file, so both live together in the profile."""
    raw = settings.get("node_snip_path")
    if raw is not None:
        import pathlib  # noqa: PLC0415
        return pathlib.Path(raw).expanduser()
    # Sibling to the node-id file.
    nid_path = _resolve_persist_path(settings)
    return nid_path.with_name(_DEFAULT_SNIP_PERSIST_FILENAME)


def _load_persisted_snip(
    path: "pathlib.Path",
) -> "dict[str, str] | None":
    """Load a previously-saved user_name/user_description from
    *path*. Missing / malformed -> None; the caller falls back to the
    NodeInfo defaults."""
    import json  # noqa: PLC0415
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as e:
        _log.warning("could not read persisted SNIP at %s: %s", path, e)
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        _log.warning(
            "persisted SNIP at %s is malformed (%s); ignoring", path, e,
        )
        return None
    if not isinstance(data, dict):
        return None
    return {
        k: str(v) for k, v in data.items()
        if k in ("user_name", "user_description")
    }


def _save_snip(path: "pathlib.Path", info: NodeInfo) -> None:
    """Best-effort save of the operator-mutable SNIP fields. A
    write failure isn't fatal -- the connection keeps advertising
    the in-memory values, they just won't survive a restart."""
    import json  # noqa: PLC0415
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({
                "user_name": info.user_name,
                "user_description": info.user_description,
            }, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError as e:
        _log.warning("could not save SNIP edits to %s: %s", path, e)


def _resolve_node_info(settings: dict[str, Any]) -> NodeInfo:
    """Build the SNIP identity block from settings, falling back to
    sensible defaults so a bare configuration still answers probes
    with something more useful than empty strings.

    The default manufacturer/model/software strings identify this
    server so an operator scanning the bus doesn't wonder "what's
    this ghost node." Hardware version reflects the platform the
    server is running on, since JMRI-Python has no hardware of
    its own -- the actual hardware ID for a bridged bus goes on
    the CAN interface node's SNIP, not ours.
    """
    raw = settings.get("node_info") or {}
    from pmri_openlcb import __version__  # noqa: PLC0415
    import platform  # noqa: PLC0415
    return NodeInfo(
        manufacturer=str(raw.get("manufacturer", "JMRI-Python")),
        model=str(raw.get("model", "JMRI Server")),
        hardware_version=str(raw.get(
            "hardware_version",
            f"{platform.system().lower()}-{platform.machine().lower()}",
        )),
        software_version=str(raw.get("software_version", __version__)),
        user_name=str(raw.get("user_name", "")),
        user_description=str(raw.get("user_description", "")),
    )


# Persistence path for a minted-and-saved Node ID. Lives in the
# active profile if we can find one, else falls back to a hidden
# dot-dir under ~. Callers can also override via
# ``settings["node_id_path"]`` (mostly for tests + operators who
# want the file somewhere specific).
_DEFAULT_PERSIST_FILENAME = "openlcb-node-id"


def _default_persist_path() -> "pathlib.Path":
    """Preferred filesystem location for the persisted Node ID file.
    Uses the JMRI profile directory when available (so the ID lives
    with the operator's other config), else a ``~/.pmri-python``
    subdir as a portable fallback."""
    import pathlib  # noqa: PLC0415
    try:
        from pmri_core.persistence.profile import Profile  # noqa: PLC0415
        profile = Profile.default()
        # ``profile_dir`` is <root>/<name> and may not exist yet on
        # a fresh install -- that's fine, we create the dir at save
        # time. The file lives at the profile root so it's easy to
        # spot in a directory listing.
        return profile.profile_dir / _DEFAULT_PERSIST_FILENAME
    except Exception:  # noqa: BLE001
        return (
            pathlib.Path("~/.pmri-python").expanduser()
            / _DEFAULT_PERSIST_FILENAME
        )


def _load_persisted_node_id(path: "pathlib.Path") -> NodeId | None:
    """Read the Node ID from *path* if it exists + parses. Returns
    ``None`` on any error (missing, malformed, unreadable) -- the
    caller then falls back to minting a fresh one."""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError as e:
        _log.warning("could not read persisted Node ID at %s: %s", path, e)
        return None
    if not text:
        return None
    try:
        return NodeId.parse(text)
    except ValueError as e:
        _log.warning("persisted Node ID at %s is malformed (%s); ignoring", path, e)
        return None


def _save_node_id(path: "pathlib.Path", nid: NodeId) -> None:
    """Best-effort save; a failure here isn't fatal -- the connection
    still works, just without persistence. Logs the failure so an
    operator investigating "why does my alias keep changing" has a
    breadcrumb."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(nid.dotted() + "\n", encoding="utf-8")
    except OSError as e:
        _log.warning("could not save minted Node ID to %s: %s", path, e)


def _resolve_persist_path(settings: dict[str, Any]) -> "pathlib.Path":
    """Return the file path where the Node ID is persisted for this
    connection. Mirrors :func:`_resolve_node_id`'s path handling so
    both agree on where saved NIDs live."""
    persist_path = settings.get("node_id_path")
    if persist_path is None:
        return _default_persist_path()
    import pathlib  # noqa: PLC0415
    return pathlib.Path(persist_path).expanduser()


def _resolve_node_id(settings: dict[str, Any]) -> NodeId:
    """Return the Node ID in priority order:

    1. ``settings["node_id"]`` -- explicit operator config wins.
    2. Persisted file at ``settings["node_id_path"]`` (default
       :func:`_default_persist_path`) -- survives across restarts
       so alias claim lands on a stable value each boot.
    3. A freshly-minted random dev-range Node ID, saved to the
       persist path so the next restart uses (2).

    Accepts either dotted hex (``02.01.12.FE.DC.BA``) or a Python
    int literal (``0x02011200FEDCBA``) in ``settings["node_id"]``.
    """
    raw = settings.get("node_id")
    if raw is not None:
        if isinstance(raw, int):
            return NodeId(raw)
        return NodeId.parse(str(raw))
    # Fall back to persistence.
    persist_path = settings.get("node_id_path")
    if persist_path is None:
        path = _default_persist_path()
    else:
        import pathlib  # noqa: PLC0415
        path = pathlib.Path(persist_path).expanduser()
    persisted = _load_persisted_node_id(path)
    if persisted is not None:
        _log.info(
            "OpenLCB: loaded persisted Node ID %s from %s",
            persisted.dotted(), path,
        )
        return persisted
    # No configured ID + nothing on disk -> mint one and save.
    body = secrets.randbits(40)
    minted = NodeId((DEV_RANGE_TOP_BYTE << 40) | body)
    _save_node_id(path, minted)
    _log.warning(
        "OpenLCB: minted random dev-range Node ID: %s (saved to %s). "
        "Set 'node_id' in settings to override.",
        minted.dotted(), path,
    )
    return minted


async def _build_port(settings: dict[str, Any]) -> BytePort:
    kind = settings.get("kind", "loopback")
    if kind == "serial":
        port = settings.get("port")
        if not port:
            raise ValueError("serial transport requires 'port'")
        baud = int(settings.get("baud", 333333))
        return await SerialPort.open(port=port, baud=baud)
    if kind == "tcp":
        host = settings.get("host")
        tcp_port = settings.get("tcp_port")
        if not host or tcp_port is None:
            raise ValueError("tcp transport requires 'host' and 'tcp_port'")
        return await TcpPort.open(host=host, port=int(tcp_port))
    if kind == "loopback":
        lb = settings.get("loopback_port")
        if not isinstance(lb, LoopbackPort):
            raise ValueError("loopback transport requires 'loopback_port'")
        return lb
    raise ValueError(f"unknown OpenLCB transport kind: {kind!r}")
