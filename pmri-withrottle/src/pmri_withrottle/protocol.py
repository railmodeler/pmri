"""WiThrottle line protocol -- parsing + encoding.

WiThrottle is a line-based ASCII protocol on TCP. Newline-terminated
messages flow in both directions, with mostly self-describing first
characters:

* ``VN`` -- protocol version
* ``HT`` -- server (railroad) name
* ``RL`` -- roster list
* ``PPA`` -- track power: ``PPA1`` on, ``PPA0`` off
* ``PFT`` -- fast clock
* ``M{T,S}...`` -- throttle multiplex commands; ``T`` is the primary
  throttle, ``S`` is the secondary on the same client
* ``*`` -- heartbeat tick from client
* ``Q`` -- quit

We implement enough of the spec to drive a loco from Engine Driver:
acquire/release a throttle, set speed/direction/functions, e-stop,
heartbeat. The fancier bits (consist building, signal cues,
turnout/route control over WiThrottle, panel data, fast-clock sync)
land in follow-up sprints.

References:
* https://www.jmri.org/help/en/package/jmri/jmrit/withrottle/Protocol.shtml
* Engine Driver's source (Android) for real-world client behaviour.
"""
from __future__ import annotations

from dataclasses import dataclass


# Default heartbeat interval the server requests of clients. Engine
# Driver's default poll interval matches this.
DEFAULT_HEARTBEAT_S = 10

# Maximum function number we acknowledge. WiThrottle supports F0..F28.
MAX_FUNCTION = 28

# Default function labels sent when no roster labels are supplied.
# Engine Driver's manual-address acquire path uses its *own* client-side
# defaults ("Light", "Bell", "Horn" for F0..F2, generic Fn thereafter);
# roster-based acquires take the labels from the wire. Matching those
# three keeps both acquire paths cosmetically consistent.
_DEFAULT_FUNCTION_LABELS = (
    ["Light", "Bell", "Horn"]
    + [f"F{i}" for i in range(3, MAX_FUNCTION + 1)]
)


# ----- typed inbound messages ----------------------------------------------


@dataclass(slots=True)
class HelloName:
    """Client sent its display name. ``N<name>``."""
    name: str


@dataclass(slots=True)
class HelloHardwareId:
    """Client sent its hardware id. ``HU<id>``."""
    hardware_id: str


@dataclass(slots=True)
class Heartbeat:
    """Client sent the periodic ``*`` tick."""


@dataclass(slots=True)
class Quit:
    """Client is disconnecting (``Q``)."""


@dataclass(slots=True)
class AcquireLoco:
    """Client requested a loco on a throttle (``M{T,S}+<addr><;><label>``).

    *throttle* is the single-character throttle id (typically ``T``
    or ``S``). *address_token* is the raw address string from the wire,
    e.g. ``L3601`` (long 3601) or ``S3`` (short 3) or ``3601`` (legacy).
    """
    throttle: str
    address_token: str
    label: str


@dataclass(slots=True)
class ReleaseLoco:
    """Client released a loco (``M{T,S}-<addr><;>``)."""
    throttle: str
    address_token: str


@dataclass(slots=True)
class ThrottleAction:
    """A V/R/F/X action on an acquired loco.

    ``M{T,S}A<address_token><;><action><value>``:

    * ``V<0..126>`` -- set speed (126 = full, 1 = e-stop sentinel, 0 = stop)
    * ``R<0|1>`` -- direction (1 = forward)
    * ``F<state><fn>`` -- function: state 1 = press, 0 = release
    * ``f<state><fn>`` -- function force-state (set explicitly)
    * ``X`` -- e-stop
    * ``I`` -- idle (speed 0)
    * ``L`` -- set speed steps; we ignore for alpha
    """
    throttle: str
    address_token: str
    action: str   # one of: "V", "R", "F", "f", "X", "I", "L"
    value: str    # remainder of the action; may be empty (e.g. for "X")


@dataclass(slots=True)
class TurnoutAction:
    """Client asked us to change a turnout: ``PTA<action><sysName>``.

    * ``action == "T"`` -- throw
    * ``action == "C"`` -- close
    * ``action == "2"`` -- toggle
    """
    action: str          # "T" | "C" | "2"
    system_name: str


@dataclass(slots=True)
class RouteAction:
    """Client asked us to fire a route: ``PRA<action><sysName>``.

    JMRI's RouteController accepts ``2`` (set / fire) as the sole
    meaningful action -- routes are one-shot. Other values are
    recorded for symmetry with the turnout path but the server-side
    handler treats non-``2`` as a no-op.
    """
    action: str
    system_name: str


@dataclass(slots=True)
class Unknown:
    """An inbound line we don't recognise -- preserve it for logging."""
    raw: str


# ----- turnout state codes (match jmri.Turnout Java constants) -------------

TURNOUT_UNKNOWN      = 1
TURNOUT_CLOSED       = 2
TURNOUT_THROWN       = 4
TURNOUT_INCONSISTENT = 8


# ----- decoder -------------------------------------------------------------


def decode_line(line: str) -> object:
    """Parse one WiThrottle inbound line. Returns one of the typed messages."""
    line = line.rstrip("\r\n")
    if not line:
        return Unknown(raw="")
    head = line[0]
    if head == "*":
        return Heartbeat()
    if head == "Q":
        return Quit()
    if head == "N" and len(line) > 1:
        return HelloName(name=line[1:])
    if head == "H" and line.startswith("HU"):
        return HelloHardwareId(hardware_id=line[2:])
    if head == "M":
        return _decode_throttle(line)
    if line.startswith("PTA") and len(line) >= 4:
        return TurnoutAction(action=line[3], system_name=line[4:])
    if line.startswith("PRA") and len(line) >= 4:
        return RouteAction(action=line[3], system_name=line[4:])
    return Unknown(raw=line)


def _decode_throttle(line: str) -> object:
    """``M{T,S}<op><addr><;><payload>``."""
    if len(line) < 4:
        return Unknown(raw=line)
    throttle = line[1]
    op = line[2]
    rest = line[3:]
    # The wire uses "<;>" (the literal three characters) as a separator.
    sep = "<;>"
    if sep in rest:
        addr_token, payload = rest.split(sep, 1)
    else:
        addr_token, payload = rest, ""
    if op == "+":
        return AcquireLoco(throttle=throttle, address_token=addr_token, label=payload)
    if op == "-":
        return ReleaseLoco(throttle=throttle, address_token=addr_token)
    if op == "A":
        if not payload:
            return Unknown(raw=line)
        action = payload[0]
        value = payload[1:]
        return ThrottleAction(
            throttle=throttle, address_token=addr_token,
            action=action, value=value,
        )
    return Unknown(raw=line)


# ----- address-token parsing ----------------------------------------------


def parse_address_token(token: str) -> tuple[int, bool]:
    """Decode WiThrottle's address-token format.

    * ``L<n>`` -- explicit long address ``n``
    * ``S<n>`` -- explicit short address ``n``
    * bare digits -- NMRA convention: addresses ``1..127`` are short,
      ``>=128`` are long. Older clients (and some legacy code paths in
      Engine Driver itself) drop the prefix; matching JMRI's behaviour,
      we infer the type from the numeric range so high-address locos
      get the correct DCC encoding.

    Returns ``(address, is_long)``.
    """
    if not token:
        raise ValueError("empty address token")
    if token[0] in ("L", "l"):
        return int(token[1:]), True
    if token[0] in ("S", "s"):
        return int(token[1:]), False
    addr = int(token)
    # NMRA: short DCC range is 1..127; anything higher must be long.
    return addr, addr >= 128


def format_address_token(address: int, long_address: bool) -> str:
    """Encode an address as a WiThrottle wire token."""
    return f"{'L' if long_address else 'S'}{address}"


# ----- encoders (server -> client) -----------------------------------------


def greeting(*,
    name: str = "Python JMRI",
    heartbeat_s: int = DEFAULT_HEARTBEAT_S,
    web_port: int = 8000,
) -> list[str]:
    """Initial handshake lines a server sends right after accept."""
    return [
        "VN2.0",                  # protocol version 2.0
        f"HT{name}",              # railroad name
        f"HtPython JMRI {name}",  # display title
        f"PW{web_port}",          # JSON server port (optional advertisement)
        f"*{heartbeat_s}",        # request heartbeat at this cadence
        "PPA0",                   # initial power state (clients refresh later)
    ]


def power_state(on: bool) -> str:
    return f"PPA{1 if on else 0}"


def roster_list(entries: list[dict]) -> str:
    """``RL<count>]\\[<id><}|><address>]\\[<long|short>]\\[<id>...``.

    *entries* are roster-service dicts (id, dccAddress, longAddress).
    """
    parts: list[str] = [f"RL{len(entries)}"]
    for e in entries:
        eid = str(e.get("id") or "")
        addr = str(e.get("dccAddress") or "")
        long = "L" if e.get("longAddress") else "S"
        # Wire form per JMRI's WiThrottle docs.
        parts.append(f"{eid}}}|{{{addr}}}|{{{long}")
    # JMRI uses "]\\[" (the literal three characters) between entries.
    return "]\\[".join(parts)


def throttle_acquired(throttle: str, address_token: str) -> str:
    """Acknowledge an M+ request."""
    return f"M{throttle}+{address_token}<;>"


def throttle_released(throttle: str, address_token: str) -> str:
    return f"M{throttle}-{address_token}<;>"


def throttle_speed(throttle: str, address_token: str, speed: int) -> str:
    return f"M{throttle}A{address_token}<;>V{speed}"


def throttle_direction(throttle: str, address_token: str, forward: bool) -> str:
    return f"M{throttle}A{address_token}<;>R{1 if forward else 0}"


def throttle_function(throttle: str, address_token: str, fn: int, on: bool) -> str:
    """``F<state><fn>`` -- note state is *before* the function number."""
    return f"M{throttle}A{address_token}<;>F{1 if on else 0}{fn}"


def throttle_speed_step_mode(
    throttle: str, address_token: str, mode: int = 1,
) -> str:
    """``M<mux>A<addr><;>s<mode>`` -- 1=128, 2=28, 4=27, 8=14, 16=Motorola.

    JMRI's ``MultiThrottleController.encodeSpeedStepMode``. Sent after
    the function states so Engine Driver / iOS know how to scale the
    slider (128-step by default on modern DCC).
    """
    return f"M{throttle}A{address_token}<;>s{mode}"


def throttle_function_labels(
    throttle: str, address_token: str, labels: list[str] | None = None,
) -> str:
    """``M<mux>L<addr><;>]\\[label0]\\[label1]...]\\[label28]``.

    JMRI's ``MultiThrottleController.sendFunctionLabels`` -- this is the
    line Engine Driver and WiThrottle iOS need after acquire so they can
    render the F0..F28 button bar.

    The label values matter more than they look:

    * **Empty** labels are interpreted by Engine Driver as "no function
      configured" and the bar stays hidden.
    * **Missing line** works only for manual (``L<addr>``) acquires; for
      roster (``E<id>``) acquires the client expects labels and hides
      the bar when none arrive.

    So we always send the line with non-empty defaults (``F0``..``F28``).
    Pass explicit *labels* when you have roster data -- anything falsy in
    the list (empty string, ``None``) falls back to the default for that
    slot.
    """
    if labels is None:
        wire_labels = list(_DEFAULT_FUNCTION_LABELS)
    else:
        # Merge caller-supplied labels over defaults; empty slots keep
        # the default so the client always sees something.
        wire_labels = list(_DEFAULT_FUNCTION_LABELS)
        for i, lbl in enumerate(labels[: MAX_FUNCTION + 1]):
            if lbl:
                wire_labels[i] = lbl
    return f"M{throttle}L{address_token}<;>" + "".join(
        f"]\\[{lbl}" for lbl in wire_labels
    )


def fast_clock(local_unix_seconds: int, rate: float) -> str:
    """``PFT<local-unix-seconds><;><rate>``.

    JMRI's ``FastClockController.sendFastTimeAndRate`` sends
    ``(UTC_millis + TZ_offset) / 1000`` -- the fast-clock time as if
    it were a unix timestamp *in local time* (i.e. UTC unix seconds
    biased by the local timezone offset so ``client_hour == local_hour``
    when the client renders it as UTC).

    A rate of ``0.0`` is the "stopped" signal -- JMRI sends a second
    ``PFT<time><;>0.0`` after the normal broadcast when the clock is
    held; Engine Driver / WiThrottle iOS use that to pause their own
    display.
    """
    return f"PFT{int(local_unix_seconds)}<;>{rate}"


def turnout_titles() -> str:
    """``PTT`` -- turnout state-label header.

    Sent once per session, before the turnout list. Tells the client
    which display labels to attach to each numeric state code so the
    UI can render "Closed"/"Thrown" (or a translation) instead of
    raw digits.

    Format matches JMRI's ``TurnoutController.sendTitles``:
    ``PTT]\\[<menu-title>}|{Turnout]\\[Closed}|{2]\\[Thrown}|{4]\\[Unknown}|{1]\\[Inconsistent}|{8``
    """
    return (
        "PTT"
        + "]\\[Turnouts}|{Turnout"
        + f"]\\[Closed}}|{{{TURNOUT_CLOSED}"
        + f"]\\[Thrown}}|{{{TURNOUT_THROWN}"
        + f"]\\[Unknown}}|{{{TURNOUT_UNKNOWN}"
        + f"]\\[Inconsistent}}|{{{TURNOUT_INCONSISTENT}"
    )


def turnout_list(entries: list[dict]) -> str:
    """``PTL]\\[<sysName>}|{<userName>}|{<state>]\\[...``

    JMRI's ``TurnoutController.sendList``. Each entry is a dict with:

    * ``system_name`` -- required, e.g. ``NT12``
    * ``user_name`` -- optional friendly label; empty string if absent
    * ``state`` -- one of :data:`TURNOUT_UNKNOWN/CLOSED/THROWN/INCONSISTENT`
    """
    parts: list[str] = ["PTL"]
    for e in entries:
        sys_name = str(e.get("system_name") or "")
        user_name = str(e.get("user_name") or "")
        state = int(e.get("state") or TURNOUT_UNKNOWN)
        parts.append(f"]\\[{sys_name}}}|{{{user_name}}}|{{{state}")
    return "".join(parts)


def turnout_state(system_name: str, state: int) -> str:
    """``PTA<state><sysName>`` -- broadcast a turnout state change to the client.

    JMRI's ``TurnoutController.sendTurnoutState``. Called whenever a
    turnout's ``KnownState`` changes -- either from a client action, a
    layout cab, or an internal script.
    """
    return f"PTA{state}{system_name}"


# ----- Route states & encoders ---------------------------------------------
#
# Mirrors JMRI's ``RouteController.sendTitles`` / ``sendList`` output.
# Routes report as four pseudo-states even though the underlying
# ``Route`` bean is really "idle" vs "firing" -- the WiThrottle
# contract maps those onto sensor-shaped states so the client UI
# shares the same badge code path with sensors.

ROUTE_UNKNOWN      = 0   # resting / default
ROUTE_ACTIVE       = 2   # the route was just fired
ROUTE_INACTIVE     = 4
ROUTE_INCONSISTENT = 8


def route_titles() -> str:
    """``PRT`` -- route state-label header.

    Sent once per session, before the route list. Mirrors
    ``RouteController.sendTitles`` so Engine Driver / WiThrottle iOS
    render the Routes tab with the same badge vocabulary as Turnouts.
    """
    return (
        "PRT"
        + "]\\[Routes}|{Route"
        + f"]\\[Active}}|{{{ROUTE_ACTIVE}"
        + f"]\\[Inactive}}|{{{ROUTE_INACTIVE}"
        + f"]\\[Inconsistent}}|{{{ROUTE_INCONSISTENT}"
        + f"]\\[Unknown}}|{{{ROUTE_UNKNOWN}"
    )


def route_list(entries: list[dict]) -> str:
    """``PRL]\\[<sysName>}|{<userName>}|{<state>]\\[...``

    JMRI's ``RouteController.sendList``. Each entry is a dict with:

    * ``system_name`` -- required, e.g. ``IO:FREIGHT_YARD``
    * ``user_name`` -- optional friendly label; empty string if absent
    * ``state`` -- one of :data:`ROUTE_UNKNOWN/ACTIVE/INACTIVE/INCONSISTENT`;
      defaults to ``ROUTE_UNKNOWN`` for a quiescent route.
    """
    parts: list[str] = ["PRL"]
    for e in entries:
        sys_name = str(e.get("system_name") or "")
        user_name = str(e.get("user_name") or "")
        state = int(e.get("state") or ROUTE_UNKNOWN)
        parts.append(f"]\\[{sys_name}}}|{{{user_name}}}|{{{state}")
    return "".join(parts)


def route_state(system_name: str, state: int) -> str:
    """``PRA<state><sysName>`` -- broadcast a route state change.

    Called when a route fires (state goes ACTIVE) and when the client
    requested fire completes. The client's own ``PRA`` message uses
    the same format for the "please fire this route" verb; dispatch
    distinguishes direction by context (inbound vs outbound).
    """
    return f"PRA{state}{system_name}"


def fast_clock_local_unix_seconds(
    hours: int, minutes: int, seconds: int = 0,
    *, now: object = None,
) -> int:
    """Convert an ``HH:MM:SS`` fast-clock time to the local-unix value
    JMRI's ``PFT`` payload expects.

    We fold today's date onto the given HH:MM:SS to build a full
    datetime, then bias UTC unix seconds by the current timezone offset
    (matching JMRI's ``(fastClockTime + timeZoneOffset) / 1000L``).
    """
    import datetime  # noqa: PLC0415
    import time as _time  # noqa: PLC0415
    ref = now if isinstance(now, datetime.datetime) else datetime.datetime.now()
    ref = ref.replace(hour=hours, minute=minutes, second=seconds, microsecond=0)
    utc_unix = ref.timestamp()
    # `time.timezone` is seconds west of UTC (positive); we want the
    # opposite sign. `altzone` applies when DST is in effect.
    if _time.localtime(utc_unix).tm_isdst > 0 and _time.daylight:
        offset = -_time.altzone
    else:
        offset = -_time.timezone
    return int(utc_unix + offset)
