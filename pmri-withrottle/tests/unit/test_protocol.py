"""WiThrottle line protocol -- encode/decode coverage."""
from __future__ import annotations

import pytest

from pmri_withrottle import protocol


# ----- decoder -------------------------------------------------------------


def test_heartbeat_decodes() -> None:
    assert isinstance(protocol.decode_line("*"), protocol.Heartbeat)


def test_quit_decodes() -> None:
    assert isinstance(protocol.decode_line("Q"), protocol.Quit)


def test_hello_name_decodes() -> None:
    msg = protocol.decode_line("NEngineDriver")
    assert isinstance(msg, protocol.HelloName)
    assert msg.name == "EngineDriver"


def test_hello_hardware_id_decodes() -> None:
    msg = protocol.decode_line("HUaabbccdd")
    assert isinstance(msg, protocol.HelloHardwareId)
    assert msg.hardware_id == "aabbccdd"


def test_acquire_loco_decodes_long_address() -> None:
    msg = protocol.decode_line("MT+L3601<;>UP_3601")
    assert isinstance(msg, protocol.AcquireLoco)
    assert msg.throttle == "T"
    assert msg.address_token == "L3601"
    assert msg.label == "UP_3601"


def test_acquire_loco_short_throttle_S() -> None:
    msg = protocol.decode_line("MS+S3<;>")
    assert isinstance(msg, protocol.AcquireLoco)
    assert msg.throttle == "S"
    assert msg.address_token == "S3"
    assert msg.label == ""


def test_release_loco_decodes() -> None:
    msg = protocol.decode_line("MT-L3601<;>")
    assert isinstance(msg, protocol.ReleaseLoco)
    assert msg.throttle == "T"
    assert msg.address_token == "L3601"


def test_speed_action_decodes() -> None:
    msg = protocol.decode_line("MTAL3601<;>V64")
    assert isinstance(msg, protocol.ThrottleAction)
    assert msg.action == "V"
    assert msg.value == "64"


def test_direction_action_decodes() -> None:
    msg = protocol.decode_line("MTAL3601<;>R1")
    assert isinstance(msg, protocol.ThrottleAction)
    assert msg.action == "R"
    assert msg.value == "1"


def test_function_action_decodes_two_digit_fn() -> None:
    msg = protocol.decode_line("MTAL3601<;>F128")
    assert isinstance(msg, protocol.ThrottleAction)
    assert msg.action == "F"
    assert msg.value == "128"


def test_estop_action_decodes() -> None:
    msg = protocol.decode_line("MTAL3601<;>X")
    assert isinstance(msg, protocol.ThrottleAction)
    assert msg.action == "X"
    assert msg.value == ""


def test_unknown_line_preserved() -> None:
    msg = protocol.decode_line("ZWHATEVER")
    assert isinstance(msg, protocol.Unknown)
    assert msg.raw == "ZWHATEVER"


def test_decoder_strips_newlines() -> None:
    msg = protocol.decode_line("Q\r\n")
    assert isinstance(msg, protocol.Quit)


# ----- address tokens ------------------------------------------------------


def test_parse_long_token() -> None:
    assert protocol.parse_address_token("L3601") == (3601, True)


def test_parse_short_token() -> None:
    assert protocol.parse_address_token("S3") == (3, False)


def test_parse_bare_low_digits_is_short() -> None:
    """``3`` is in the NMRA short range (1..127) -> short."""
    assert protocol.parse_address_token("3") == (3, False)


def test_parse_bare_high_digits_is_long() -> None:
    """``3601`` is >=128 -> long (NMRA convention, mirrors JMRI)."""
    assert protocol.parse_address_token("3601") == (3601, True)


def test_parse_bare_127_boundary_is_short() -> None:
    """127 is the last short address."""
    assert protocol.parse_address_token("127") == (127, False)


def test_parse_bare_128_boundary_is_long() -> None:
    """128 is the first long address."""
    assert protocol.parse_address_token("128") == (128, True)


def test_parse_empty_raises() -> None:
    with pytest.raises(ValueError):
        protocol.parse_address_token("")


def test_format_token_long() -> None:
    assert protocol.format_address_token(3601, True) == "L3601"


def test_format_token_short() -> None:
    assert protocol.format_address_token(3, False) == "S3"


# ----- encoders ------------------------------------------------------------


def test_greeting_contains_protocol_version() -> None:
    lines = protocol.greeting()
    assert any(l.startswith("VN") for l in lines)
    assert any(l.startswith("*") for l in lines)
    assert any(l.startswith("PPA") for l in lines)


def test_power_state() -> None:
    assert protocol.power_state(True) == "PPA1"
    assert protocol.power_state(False) == "PPA0"


def test_roster_list_with_entries() -> None:
    out = protocol.roster_list([
        {"id": "UP_3601", "dccAddress": "3601", "longAddress": True},
        {"id": "Y_5",     "dccAddress": "5",    "longAddress": False},
    ])
    assert out.startswith("RL2")
    assert "UP_3601" in out
    assert "Y_5" in out
    # The wire delimiter is "]\\[".
    assert "]\\[" in out


def test_roster_list_empty() -> None:
    assert protocol.roster_list([]) == "RL0"


def test_route_titles_matches_pmri_shape() -> None:
    """PRT carries the four state labels in a fixed order. Engine
    Driver / WiThrottle iOS look up labels by position; a shuffled
    list renders the badges as "Unknown" for every route."""
    out = protocol.route_titles()
    assert out.startswith("PRT")
    assert "Routes" in out
    assert "Active}|{2" in out
    assert "Inactive}|{4" in out
    assert "Inconsistent}|{8" in out
    assert "Unknown}|{0" in out


def test_route_list_with_entries() -> None:
    out = protocol.route_list([
        {"system_name": "IO1", "user_name": "Yard", "state": 2},
        {"system_name": "IO2", "user_name": "", "state": 0},
    ])
    assert out.startswith("PRL")
    assert "IO1}|{Yard}|{2" in out
    assert "IO2}|{}|{0" in out
    assert "]\\[" in out


def test_route_list_empty_is_prl_header_only() -> None:
    assert protocol.route_list([]) == "PRL"


def test_route_state_broadcast_format() -> None:
    assert protocol.route_state("IO42", protocol.ROUTE_ACTIVE) == "PRA2IO42"


def test_decode_pra_builds_route_action() -> None:
    """``PRA2IO1`` must decode to a RouteAction with action="2" and
    system_name="IO1". The parser follows the same positional shape
    as PTA so Engine Driver's identical format works end-to-end."""
    msg = protocol.decode_line("PRA2IO1")
    assert isinstance(msg, protocol.RouteAction)
    assert msg.action == "2"
    assert msg.system_name == "IO1"


def test_throttle_state_encoders() -> None:
    assert protocol.throttle_acquired("T", "L3601") == "MT+L3601<;>"
    assert protocol.throttle_released("T", "L3601") == "MT-L3601<;>"
    assert protocol.throttle_speed("T", "L3601", 64) == "MTAL3601<;>V64"
    assert protocol.throttle_direction("T", "L3601", True) == "MTAL3601<;>R1"
    assert protocol.throttle_direction("T", "L3601", False) == "MTAL3601<;>R0"
    assert protocol.throttle_function("T", "L3601", 28, True) == "MTAL3601<;>F128"


def test_throttle_function_labels_default_matches_engine_driver_manual_defaults() -> None:
    """The label line makes Engine Driver render the F0..F28 bar. Defaults
    mirror Engine Driver's own manual-acquire labels (Light/Bell/Horn for
    F0..F2, generic Fn thereafter) so roster and manual acquisition paths
    render cosmetically identical buttons."""
    out = protocol.throttle_function_labels("T", "L3601")
    assert out.startswith("MTLL3601<;>")
    # 29 slot separators, one per function.
    assert out.count("]\\[") == 29
    # F0..F2 use Engine Driver's built-in cosmetic defaults.
    assert "Light" in out and "Bell" in out and "Horn" in out
    # F3..F28 use generic slot names.
    assert "F3" in out and "F28" in out
    # F0..F2 are named -- the string "F0" (as a whole slot) should not
    # appear because the slot is now "Light" not "F0".
    assert "]\\[F0]" not in out
    # No empty slots.
    assert "]\\[]\\[" not in out


def test_throttle_function_labels_supplied_labels_take_precedence() -> None:
    """Explicit roster labels override defaults but empty slots fall back
    to the built-in defaults."""
    out = protocol.throttle_function_labels(
        "T", "L3601", ["Marker", "Coupler", ""],
    )
    assert "Marker" in out
    assert "Coupler" in out
    # Slot 2 was empty -> falls back to the default "Horn".
    assert "Horn" in out
    assert out.count("]\\[") == 29


# ----- turnouts ------------------------------------------------------------


def test_turnout_titles_carries_state_labels() -> None:
    out = protocol.turnout_titles()
    assert out.startswith("PTT")
    # Every state code the client cares about, with its display label.
    assert "Closed}|{2" in out
    assert "Thrown}|{4" in out
    assert "Unknown}|{1" in out
    assert "Inconsistent}|{8" in out


def test_turnout_list_encodes_entries() -> None:
    out = protocol.turnout_list([
        {"system_name": "NT1", "user_name": "West Yard", "state": protocol.TURNOUT_CLOSED},
        {"system_name": "NT2", "user_name": "", "state": protocol.TURNOUT_THROWN},
    ])
    assert out.startswith("PTL")
    assert "]\\[NT1}|{West Yard}|{2" in out
    assert "]\\[NT2}|{}|{4" in out


def test_turnout_list_empty_input_produces_bare_prefix() -> None:
    assert protocol.turnout_list([]) == "PTL"


def test_turnout_state_broadcast_encoder() -> None:
    assert protocol.turnout_state("NT12", protocol.TURNOUT_THROWN) == "PTA4NT12"
    assert protocol.turnout_state("NT12", protocol.TURNOUT_CLOSED) == "PTA2NT12"


def test_decode_turnout_action_throw() -> None:
    msg = protocol.decode_line("PTATNT12")
    assert isinstance(msg, protocol.TurnoutAction)
    assert msg.action == "T"
    assert msg.system_name == "NT12"


def test_decode_turnout_action_close() -> None:
    msg = protocol.decode_line("PTACNT12")
    assert isinstance(msg, protocol.TurnoutAction)
    assert msg.action == "C"
    assert msg.system_name == "NT12"


def test_decode_turnout_action_toggle() -> None:
    msg = protocol.decode_line("PTA2NT12")
    assert isinstance(msg, protocol.TurnoutAction)
    assert msg.action == "2"
    assert msg.system_name == "NT12"


def test_fast_clock_encoder() -> None:
    out = protocol.fast_clock(1_700_000_000, 4.0)
    assert out.startswith("PFT")
    assert "1700000000" in out
    assert "4.0" in out


def test_fast_clock_local_unix_seconds_folds_hh_mm_on_today() -> None:
    """Given HH:MM:SS, we produce a local-unix timestamp for *today*
    at that time. Sanity check: the seconds-portion of the resulting
    timestamp must match the input."""
    import datetime  # noqa: PLC0415
    ref = datetime.datetime(2026, 7, 7, 0, 0, 0)  # anchor to a fixed date
    unix = protocol.fast_clock_local_unix_seconds(7, 30, 0, now=ref)
    # Convert back: as UTC (JMRI's convention), the hour should read as 7.
    dt = datetime.datetime.fromtimestamp(unix, tz=datetime.timezone.utc)
    assert dt.hour == 7
    assert dt.minute == 30
