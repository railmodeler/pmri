"""Human help for each JSON API topic.

Rendered as an accordion under the "Topics available on the JSON API"
card on the home tab. Each :class:`TopicHelp` entry gets one badge on
the home page that, when clicked, expands to show the summary, a
methods table, and an example round-trip.

**Content policy** -- every method entry needs at minimum a
one-sentence description and either an example request or a note
saying no data is required. Keep examples short: they're for
"remind me what the shape is," not full protocol docs. Full protocol
docs live in the generated pdoc reference under ``/docs/``.

Adding a new service:

* Register a new :class:`TopicHelp` in :data:`TOPIC_HELP`.
* If you can't fill in an example immediately, use
  ``example_request=None`` -- the UI hides the block cleanly.
* The ``source`` field is a short pointer for developers reading the
  reference; usually ``pmri_server.services.<module>.<Class>``.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class MethodHelp:
    """One method (``list`` / ``get`` / ``put`` / ``post`` / ``delete``)."""

    name: str
    description: str
    example_request: str | None = None
    example_response: str | None = None


@dataclass(frozen=True, slots=True)
class TopicHelp:
    """Everything the accordion needs to render one topic's help panel."""

    topic: str
    summary: str
    methods: list[MethodHelp] = field(default_factory=list)
    # Short "read the code" pointer -- module.ClassName, no leading dots.
    source: str = ""
    # Any topic that isn't in JMRI Java's original API gets ``True`` so
    # the UI can badge it as rewrite-specific. Keeps client-side JMRI
    # tooling from expecting it against a legacy server.
    rewrite_extension: bool = False


# ---- shared examples ---------------------------------------------------

# Every bean list follows this shape, so keep one canonical example
# to avoid drift across the eight bean services.
_BEAN_LIST_EXAMPLE = '{"type": "turnout", "method": "list"}'
_BEAN_LIST_RESPONSE = (
    '{"type": "list", "data": [\n'
    '  {"name": "IT1", "userName": "Main", "state": 2, "inverted": false}\n'
    "]}"
)
_BEAN_GET_EXAMPLE = (
    '{"type": "turnout", "method": "get", "data": {"name": "IT1"}}'
)
_BEAN_POST_EXAMPLE = (
    '{"type": "turnout", "method": "post",\n'
    ' "data": {"name": "IT1", "state": 2}}'
)


def _bean_methods(kind: str) -> list[MethodHelp]:
    """The standard four-method surface every bean service exposes."""
    return [
        MethodHelp(
            "list", f"Every registered {kind} with its current state.",
            example_request=f'{{"type": "{kind}", "method": "list"}}',
            example_response=_BEAN_LIST_RESPONSE.replace("turnout", kind),
        ),
        MethodHelp(
            "get", f"One {kind}'s current state by system name.",
            example_request=_BEAN_GET_EXAMPLE.replace("turnout", kind),
        ),
        MethodHelp(
            "post", f"Change one {kind}'s state (throw/close, active/inactive).",
            example_request=_BEAN_POST_EXAMPLE.replace("turnout", kind),
        ),
        MethodHelp(
            "put", f"Create or update a {kind}. Idempotent -- a repeated "
            "put with the same fields is a no-op.",
        ),
    ]


# ---- topic help registry ----------------------------------------------


TOPIC_HELP: dict[str, TopicHelp] = {
    # ---- meta ---------------------------------------------------------
    "hello": TopicHelp(
        topic="hello",
        summary=(
            "WebSocket handshake response. Emitted by the server as soon "
            "as a client connects; carries the server's clock heartbeat "
            "interval so a client can tell when to expect keep-alives."
        ),
        source="pmri_server.services.meta.HelloService",
    ),
    "ping": TopicHelp(
        topic="ping",
        summary=(
            "Liveness probe. Send ``{\"type\": \"ping\"}`` and get "
            "``{\"type\": \"pong\"}`` back -- useful to keep an idle "
            "WebSocket connection alive through a stateful firewall."
        ),
        methods=[
            MethodHelp(
                "get", "Round-trip ping -- returns pong.",
                example_request='{"type": "ping"}',
                example_response='{"type": "pong", "data": {}}',
            ),
        ],
        source="pmri_server.services.meta.PingService",
    ),
    "version": TopicHelp(
        topic="version",
        summary=(
            "Server version string. Matches the JMRI Java ``version`` "
            "topic so a legacy JMRI client can gate features by server "
            "version. Ours reports the ``pmri-server`` package version."
        ),
        methods=[
            MethodHelp(
                "get", "Report the running server's version.",
                example_request='{"type": "version"}',
                example_response='{"type": "version", "data": {"version": "0.1.0b3"}}',
            ),
        ],
        source="pmri_server.services.meta.VersionService",
    ),
    "railroad": TopicHelp(
        topic="railroad",
        summary=(
            "Layout metadata -- the railroad's user-set name from the "
            "active profile. Read-only in the current build; JMRI Java "
            "allows editing here but we defer to profile-edit for now."
        ),
        methods=[
            MethodHelp(
                "get", "Fetch the railroad name.",
                example_request='{"type": "railroad"}',
            ),
        ],
        source="pmri_server.services.meta.RailroadService",
    ),
    "node": TopicHelp(
        topic="node",
        summary=(
            "This server's identity as a networking peer -- hostname, "
            "profile id, uuid. Consumed by external tools that need to "
            "distinguish multiple JMRI instances on one LAN."
        ),
        methods=[
            MethodHelp("get", "Report node identity.",
                       example_request='{"type": "node"}'),
        ],
        source="pmri_server.services.meta.NodeService",
    ),
    "power": TopicHelp(
        topic="power",
        summary=(
            "Track power state (ON / OFF / UNKNOWN). Wired through the "
            "active connection's ``PowerManager``. For OpenLCB, "
            "``set`` emits the well-known power events; for LocoNet / "
            "NCE / DCC-EX, it sends the driver-specific power packet."
        ),
        methods=[
            MethodHelp(
                "get", "Current power state.",
                example_request='{"type": "power"}',
                example_response=(
                    '{"type": "power", "data": {"state": 2}}\n'
                    "// state: 2 = ON, 4 = OFF, 0 = UNKNOWN (JMRI convention)"
                ),
            ),
            MethodHelp(
                "post", "Set power on or off. State ``2`` = ON, ``4`` = OFF.",
                example_request='{"type": "power", "method": "post", "data": {"state": 2}}',
            ),
        ],
        source="pmri_server.services.meta.PowerService",
    ),

    # ---- beans --------------------------------------------------------
    "turnout": TopicHelp(
        topic="turnout",
        summary=(
            "Turnout points (switch machines). State: 2 = closed, "
            "4 = thrown. Post the target state to command a change; "
            "listeners on the WebSocket get pushes on every observed "
            "transition, including hardware-initiated ones."
        ),
        methods=_bean_methods("turnout"),
        source="pmri_server.services.beans.TurnoutService",
    ),
    "sensor": TopicHelp(
        topic="sensor",
        summary=(
            "Track occupancy / block detection sensors. State: 2 = "
            "active (occupied), 4 = inactive. Typically read-only from "
            "hardware; the post method is available for testing or "
            "simulated layouts."
        ),
        methods=_bean_methods("sensor"),
        source="pmri_server.services.beans.SensorService",
    ),
    "light": TopicHelp(
        topic="light",
        summary=(
            "Layout lights (buildings, streetlights, tower lamps). "
            "Modeled as two-state beans on most drivers; some support "
            "variable intensity via CV writes."
        ),
        methods=_bean_methods("light"),
        source="pmri_server.services.beans.LightService",
    ),
    "signalHead": TopicHelp(
        topic="signalHead",
        summary=(
            "Individual signal heads -- single-aspect indicators driven "
            "by a bank of outputs. Composed into signal masts by the "
            "``signalMast`` service for multi-aspect signalling."
        ),
        methods=_bean_methods("signalHead"),
        source="pmri_server.services.beans.SignalHeadService",
    ),
    "signalMast": TopicHelp(
        topic="signalMast",
        summary=(
            "Signal masts -- multi-aspect signals composed of one or "
            "more heads with a named signalling system (e.g. AAR-1946, "
            "NORAC). State is an aspect string like ``\"Clear\"`` or "
            "``\"Approach Medium\"``."
        ),
        methods=_bean_methods("signalMast"),
        source="pmri_server.services.beans.SignalMastService",
    ),
    "memory": TopicHelp(
        topic="memory",
        summary=(
            "Memory variables -- free-form named values used by LogixNG "
            "logic and panel scripts. Not persistent by default; "
            "declaring a Memory in PanelPro.xml makes it stick."
        ),
        methods=_bean_methods("memory"),
        source="pmri_server.services.beans.MemoryService",
    ),
    "reporter": TopicHelp(
        topic="reporter",
        summary=(
            "Reporters -- beans that carry a last-seen value (typically "
            "a loco address from a Digitrax transponding zone or an "
            "RFID reader). Different from sensors because they carry "
            "structured payloads, not just on/off state."
        ),
        methods=_bean_methods("reporter"),
        source="pmri_server.services.beans.ReporterService",
    ),
    "block": TopicHelp(
        topic="block",
        summary=(
            "Blocks -- segments of track with associated occupancy and "
            "the loco address currently in them. Feeds routing / "
            "signalling / dispatcher logic."
        ),
        methods=_bean_methods("block"),
        source="pmri_server.services.beans.BlockService",
    ),
    "route": TopicHelp(
        topic="route",
        summary=(
            "Routes -- named collections of turnout positions activated "
            "together (e.g. ``\"Yard Lead Left\"`` throws the ladder "
            "into a specific yard track). Static; for conditional "
            "routing use LogixNG."
        ),
        methods=_bean_methods("route"),
        source="pmri_server.services.beans.RouteService",
    ),
    "audio": TopicHelp(
        topic="audio",
        summary=(
            "Layout audio sources -- sound files triggered by scripts "
            "or LogixNG. State controls playback (play, stop, loop)."
        ),
        methods=_bean_methods("audio"),
        source="pmri_server.services.audio.AudioService",
    ),
    "idTag": TopicHelp(
        topic="idTag",
        summary=(
            "ID tags -- the values a Reporter sees. Modeling RFID / "
            "transponder tokens so LogixNG can match against the "
            "*identity* rather than the reporter that saw it."
        ),
        methods=_bean_methods("idTag"),
        source="pmri_server.services.id_tag.IdTagService",
    ),
    "layoutBlock": TopicHelp(
        topic="layoutBlock",
        summary=(
            "Layout blocks -- a routing-graph abstraction over one or "
            "more ``block`` beans. Adds physical connectivity used by "
            "the automatic dispatcher and by SSL signalling."
        ),
        methods=_bean_methods("layoutBlock"),
        source="pmri_server.services.layout_block.LayoutBlockService",
    ),
    "oblock": TopicHelp(
        topic="oblock",
        summary=(
            "OBlocks (\"occupancy blocks\") -- the Warrant system's "
            "richer block model, with per-block path definitions and "
            "signalling state. Used by Warrants and the automatic "
            "dispatcher."
        ),
        methods=_bean_methods("oblock"),
        source="pmri_server.services.oblock.OBlockService",
    ),

    # ---- programming + throttles --------------------------------------
    "throttle": TopicHelp(
        topic="throttle",
        summary=(
            "Loco control -- acquire an address, set speed, set "
            "functions, release. One throttle per DCC address on the "
            "active connection."
        ),
        methods=[
            MethodHelp(
                "put", "Acquire a throttle for a DCC address.",
                example_request=(
                    '{"type": "throttle", "method": "put",\n'
                    ' "data": {"address": 3, "long_address": false}}'
                ),
            ),
            MethodHelp(
                "post", "Update speed / direction / function state.",
                example_request=(
                    '{"type": "throttle", "method": "post",\n'
                    ' "data": {"address": 3, "speed": 0.5, '
                    '"direction": "forward", "functions": {"0": true}}}'
                ),
            ),
            MethodHelp(
                "delete", "Release the throttle (clean release, keeps "
                "the loco rolling at its last commanded speed until "
                "the CS purge timeout).",
                example_request=(
                    '{"type": "throttle", "method": "delete",\n'
                    ' "data": {"address": 3}}'
                ),
            ),
        ],
        source="pmri_server.services.throttle.ThrottleService",
    ),
    "programmer": TopicHelp(
        topic="programmer",
        summary=(
            "CV read / write on the programming track (service mode) "
            "or on a running loco (ops mode). Ops-mode reads require "
            "RailCom on the CS + decoder. Requests block for 1-3s per "
            "CV on service mode."
        ),
        methods=[
            MethodHelp(
                "get", "Read one CV.",
                example_request=(
                    '{"type": "programmer", "method": "get",\n'
                    ' "data": {"cv": 29, "mode": "service"}}'
                ),
            ),
            MethodHelp(
                "post", "Write one CV.",
                example_request=(
                    '{"type": "programmer", "method": "post",\n'
                    ' "data": {"cv": 29, "value": 6, "mode": "service"}}'
                ),
            ),
        ],
        source="pmri_server.services.programmer.ProgrammerService",
    ),
    "roster": TopicHelp(
        topic="roster",
        summary=(
            "Roster entries -- the profile's loco list, each with a "
            "decoder family/model/productID and per-loco function "
            "labels. Backed by ``<profile>/roster.xml``."
        ),
        methods=[
            MethodHelp(
                "list", "Every roster entry in the active profile.",
                example_request='{"type": "roster", "method": "list"}',
            ),
            MethodHelp(
                "get", "One entry by roster ID.",
                example_request=(
                    '{"type": "roster", "method": "get", "data": {"id": "3601"}}'
                ),
            ),
            MethodHelp(
                "put", "Create or update a roster entry.",
            ),
            MethodHelp(
                "delete", "Remove a roster entry from the profile.",
            ),
        ],
        source="pmri_server.services.roster.RosterService",
    ),

    # ---- automation ---------------------------------------------------
    "consist": TopicHelp(
        topic="consist",
        summary=(
            "Loco consists (multi-unit lash-ups). Add / remove locos, "
            "set direction-relative-to-consist, kill the consist. "
            "Backed by the active connection's ``ConsistManager``."
        ),
        methods=[
            MethodHelp(
                "list", "Every registered consist.",
                example_request='{"type": "consist", "method": "list"}',
            ),
            MethodHelp(
                "put", "Create a new consist at a given address.",
                example_request=(
                    '{"type": "consist", "method": "put",\n'
                    ' "data": {"address": 20}}'
                ),
            ),
            MethodHelp(
                "post", "Add or remove a member, set direction.",
                example_request=(
                    '{"type": "consist", "method": "post", "data":\n'
                    ' {"address": 20, "action": "add", "loco": 3601,\n'
                    '  "direction_normal": true}}'
                ),
            ),
            MethodHelp(
                "delete", "Kill the consist.",
                example_request=(
                    '{"type": "consist", "method": "delete",\n'
                    ' "data": {"address": 20}}'
                ),
            ),
            MethodHelp(
                "post",
                "Refresh from the command station's consist table "
                "(NCE only). Optional ``min_consist`` / ``max_consist`` "
                "to bound the scan; returns the list of consists whose "
                "membership changed and a fresh snapshot of every "
                "consist.",
                example_request=(
                    '{"type": "consist", "method": "post",\n'
                    ' "data": {"action": "refresh_from_cs"}}'
                ),
            ),
        ],
        source="pmri_server.services.consist.ConsistService",
    ),
    "openlcb": TopicHelp(
        topic="openlcb",
        summary=(
            "OpenLCB / LCC bus discovery. Enumerate the alias<->NodeID "
            "map, cached SNIP / PIP flags, and produced/consumed "
            "events. ``post {\"action\": \"discover\"}`` broadcasts "
            "Verify NID Global + Identify Events Global to "
            "repopulate the roster; ``probe_snip`` / ``probe_pip`` "
            "target a specific alias; ``search`` runs Traction "
            "Search by DCC address. Errors 400 when the active "
            "connection isn't OpenLCB."
        ),
        methods=[
            MethodHelp(
                "list", "Every alias with any cached identity data.",
                example_request='{"type": "openlcb", "method": "list"}',
            ),
            MethodHelp(
                "get", "One node's full projection.",
                example_request=(
                    '{"type": "openlcb", "method": "get",\n'
                    ' "data": {"alias": "0x222"}}'
                ),
            ),
            MethodHelp(
                "post", "Discovery / probe / search / monitor actions.",
                example_request=(
                    '{"type": "openlcb", "method": "post",\n'
                    ' "data": {"action": "discover"}}'
                ),
            ),
            MethodHelp(
                "post",
                "``recent_frames`` returns the bus-monitor ring buffer "
                "(newest first). Optional ``limit`` bounds the count.",
                example_request=(
                    '{"type": "openlcb", "method": "post",\n'
                    ' "data": {"action": "recent_frames", "limit": 100}}'
                ),
            ),
        ],
        source="pmri_server.services.openlcb.OpenLcbService",
    ),
    "openlcbCS": TopicHelp(
        topic="openlcbCS",
        summary=(
            "OpenLCB Command Station node -- the set of DCC locos "
            "we expose to the OpenLCB bus as virtual train nodes. "
            "``list`` returns hosted locos with alias, controller NID, "
            "and consist membership. ``post {\"action\": \"add\"|\"remove\", "
            "\"address\": N, \"long_address\": bool}`` manages hosted "
            "trains -- requires a CS already attached via startup "
            "script (multi-connection binding step)."
        ),
        methods=[
            MethodHelp(
                "list", "Hosted locos with alias + controller state.",
                example_request='{"type": "openlcbCS", "method": "list"}',
            ),
            MethodHelp(
                "post",
                "Add / remove one hosted DCC address.",
                example_request=(
                    '{"type": "openlcbCS", "method": "post",\n'
                    ' "data": {"action": "add", "address": 3,\n'
                    '  "long_address": false}}'
                ),
            ),
        ],
        source="pmri_server.services.openlcb.OpenLcbCSService",
    ),
    "logixng": TopicHelp(
        topic="logixng",
        summary=(
            "LogixNG programs -- declarative automation trees. Each "
            "program has a trigger (turnout thrown, sensor active, "
            "clock time, ...) and an action tree (set turnout, run "
            "route, invoke a script). Stored per-profile as YAML."
        ),
        methods=[
            MethodHelp(
                "list", "Every loaded LogixNG program.",
                example_request='{"type": "logixng", "method": "list"}',
            ),
            MethodHelp(
                "get", "One program's structure by system name.",
            ),
            MethodHelp(
                "post", "Enable / disable a program at runtime.",
            ),
        ],
        source="pmri_server.services.logixng.LogixNGService",
    ),
    "signalSystem": TopicHelp(
        topic="signalSystem",
        summary=(
            "Loaded signal systems (AAR-1946, NORAC, custom). Read-"
            "only; systems are defined by XML files under the profile "
            "and enumerated on load."
        ),
        methods=[
            MethodHelp(
                "list", "Every loaded signal system with its aspects.",
                example_request='{"type": "signalSystem", "method": "list"}',
            ),
            MethodHelp("get", "One signal system by name."),
        ],
        source="pmri_server.services.signal_system.SignalSystemService",
    ),

    # ---- infra --------------------------------------------------------
    "time": TopicHelp(
        topic="time",
        summary=(
            "Fast clock -- layout time separate from wall-clock time. "
            "Rate is a multiplier; setting rate to 0 pauses it. "
            "``time`` is an ISO-8601 timestamp of the current fast-"
            "clock reading."
        ),
        methods=[
            MethodHelp(
                "get", "Current fast-clock reading + rate.",
                example_request='{"type": "time"}',
            ),
            MethodHelp(
                "post", "Set the time or the rate.",
                example_request=(
                    '{"type": "time", "method": "post",\n'
                    ' "data": {"rate": 4.0, "run": true}}'
                ),
            ),
        ],
        source="pmri_server.services.fast_clock.TimeService",
    ),
    "panel": TopicHelp(
        topic="panel",
        summary=(
            "Panel definitions -- the operator's on-screen layout "
            "diagram. Currently exposes the list of loaded panels; the "
            "full panel-render pipeline is a future item."
        ),
        methods=[
            MethodHelp(
                "list", "Every loaded panel in the profile.",
                example_request='{"type": "panel", "method": "list"}',
            ),
        ],
        source="pmri_server.services.panel.PanelService",
    ),
    "metadata": TopicHelp(
        topic="metadata",
        summary=(
            "Server metadata -- JMRI version, copyright, activation "
            "profile, Java version (N/A in our Python build), OS. "
            "Consumed by legacy JMRI clients for feature detection."
        ),
        methods=[
            MethodHelp("list", "Every metadata field."),
            MethodHelp(
                "get", "One metadata value by name.",
                example_request=(
                    '{"type": "metadata", "method": "get",\n'
                    ' "data": {"name": "JMRIVERCANON"}}'
                ),
            ),
        ],
        source="pmri_server.services.util.MetadataService",
    ),
    "networkService": TopicHelp(
        topic="networkService",
        summary=(
            "Advertised network services -- WiThrottle TCP port, "
            "WebSocket URL, HTTP root. Legacy JMRI clients enumerate "
            "these to auto-configure."
        ),
        methods=[
            MethodHelp("list", "Every advertised network service."),
        ],
        source="pmri_server.services.util.NetworkServiceService",
    ),
    "systemConnection": TopicHelp(
        topic="systemConnection",
        summary=(
            "The active hardware connection (NCE / LocoNet / DCC-EX / "
            "OpenLCB / internal). Read-only enumeration; connection "
            "config lives in ``ConnectionConfig.xml`` under the "
            "profile."
        ),
        methods=[
            MethodHelp("list", "Every configured system connection."),
        ],
        source="pmri_server.services.util.SystemConnectionService",
    ),

    # ---- rewrite-specific ---------------------------------------------
    "logs": TopicHelp(
        topic="logs",
        summary=(
            "Server log tail -- the last N lines from the running "
            "jmri.log. Used by the home-tab log strip and the /logs "
            "diagnostics page."
        ),
        methods=[
            MethodHelp(
                "list", "Last N lines from jmri.log.",
                example_request='{"type": "logs", "method": "list", "data": {"tail": 50}}',
            ),
        ],
        source="pmri_server.services.logs.LogsService",
        rewrite_extension=True,
    ),
    "configUser": TopicHelp(
        topic="configUser",
        summary=(
            "User management + self-service password/token change. "
            "Admin-only for list/get/put/delete; the ``change_password`` "
            "and ``regenerate_token`` post actions are self-service for "
            "any authenticated user (admin can act on anyone). Never "
            "returns the password hash -- only whether one exists."
        ),
        methods=[
            MethodHelp("list", "Every registered user with role + has-token."),
            MethodHelp(
                "put", "Create or update a user.",
                example_request=(
                    '{"type": "configUser", "method": "put", "data": {\n'
                    '  "username": "alice",\n'
                    '  "password": "secret",\n'
                    '  "role": "operator"\n'
                    "}}"
                ),
            ),
            MethodHelp(
                "delete", "Remove a user. Refuses self-delete + last-admin.",
            ),
            MethodHelp(
                "post",
                "action=change_password -> change your own or (as admin) "
                "someone else's password. action=regenerate_token -> mint "
                "a new API token; the old one stops working immediately.",
                example_request=(
                    '{"type": "configUser", "method": "post", "data": {\n'
                    '  "action": "regenerate_token",\n'
                    '  "username": "alice"\n'
                    "}}"
                ),
            ),
        ],
        source="pmri_server.services.config_user.ConfigUserService",
        rewrite_extension=True,
    ),
    "configProfile": TopicHelp(
        topic="configProfile",
        summary=(
            "Create / delete / list JMRI profiles under the settings "
            "root. Delete only unregisters the profile from "
            "``profiles.xml`` -- the on-disk directory is left in "
            "place so an accidental UI click can't destroy layout "
            "data."
        ),
        methods=[
            MethodHelp("list", "Every registered profile."),
            MethodHelp(
                "put", "Create a new profile.",
                example_request=(
                    '{"type": "configProfile", "method": "put",\n'
                    ' "data": {"name": "production"}}'
                ),
            ),
            MethodHelp(
                "delete", "Unregister a profile (dir left on disk).",
                example_request=(
                    '{"type": "configProfile", "method": "delete",\n'
                    ' "data": {"id": "production.abcd1234"}}'
                ),
            ),
        ],
        source="pmri_server.services.config_profile.ConfigProfileService",
        rewrite_extension=True,
    ),
    "beanTransfer": TopicHelp(
        topic="beanTransfer",
        summary=(
            "Copy beans (turnouts / sensors / lights) between "
            "profiles. Preview shows what would be added; apply "
            "commits the copy. When the target profile is the active "
            "one, live managers refresh so the change is visible "
            "immediately without a profile reload."
        ),
        methods=[
            MethodHelp("list", "Every profile available as source/target."),
            MethodHelp(
                "post",
                "Preview or apply a transfer. ``action=preview`` returns "
                "the diff; ``action=apply`` commits it.",
                example_request=(
                    '{"type": "beanTransfer", "method": "post", "data":\n'
                    ' {"action": "preview", "source": "dev.abcd",\n'
                    '  "target": "prod.efgh", "kinds": ["turnout"]}}'
                ),
            ),
        ],
        source="pmri_server.services.bean_transfer.BeanTransferService",
        rewrite_extension=True,
    ),
    "scripts": TopicHelp(
        topic="scripts",
        summary=(
            "Python scripts loaded from ``<profile>/python/`` and "
            "``<settings>/python/``. List, save, start, stop, delete. "
            "Shared scripts (settings-root) are operator-managed and "
            "can't be deleted through the API."
        ),
        methods=[
            MethodHelp(
                "list", "Running scripts, or with data={\"library\": true}, "
                "the whole on-disk library.",
                example_request='{"type": "scripts", "method": "list"}',
            ),
            MethodHelp(
                "get", "One script's status by name.",
                example_request=(
                    '{"type": "scripts", "method": "get",\n'
                    ' "data": {"name": "morning_setup"}}'
                ),
            ),
            MethodHelp(
                "put", "Start a script from inline source. "
                "``save=true`` also writes it to the profile's "
                "``python/`` dir so it autoloads next boot.",
                example_request=(
                    '{"type": "scripts", "method": "put", "data":\n'
                    ' {"name": "hello", "source": "print(\\"hi\\")",\n'
                    '  "save": true}}'
                ),
            ),
            MethodHelp(
                "post",
                "action=stop -> stop a running script; "
                "action=delete -> also remove the on-disk file.",
            ),
        ],
        source="pmri_server.services.scripts.ScriptsService",
        rewrite_extension=True,
    ),
}


def get(topic: str) -> TopicHelp | None:
    """Return the help entry for *topic*, or None if none is registered.

    The web layer falls back to a "no help authored yet" panel when
    this returns None -- the topic still renders as a badge, just
    without content. That way a new service can land without the
    help wire-up blocking it.
    """
    return TOPIC_HELP.get(topic)
