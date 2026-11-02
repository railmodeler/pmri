"""Row/panel templates render passive (``writable: false``) beans
with an "observed" badge and NO state-change buttons.

Rendering goes through Jinja directly rather than the HTTP stack to
keep the test independent of which driver happens to be loaded in
the integration fixture (``jmri-ncemonitor`` isn't a hard dep of
``jmri-web``).
"""
from __future__ import annotations

import jinja2


_env = jinja2.Environment(
    loader=jinja2.PackageLoader("jmri_web", "templates"),
    autoescape=jinja2.select_autoescape(["html"]),
)


# ---- turnout row ----------------------------------------------------------


def _writable_turnout(name: str = "LT1") -> dict:
    """Shape matching ``_common_named_bean`` output for a writable bean --
    the ``writable`` key is absent, which is how the field appears for
    every non-passive bean."""
    return {"name": name, "userName": "", "state": 2}


def _observed_turnout(name: str = "MT1") -> dict:
    return {"name": name, "userName": "", "state": 2, "writable": False}


def test_writable_turnout_row_renders_state_buttons() -> None:
    """Baseline: a normal row keeps Close + Throw buttons."""
    html = _env.get_template("_turnout_row.html").render(t=_writable_turnout())
    assert ">Close</button>" in html
    assert ">Throw</button>" in html
    assert "observed" not in html


def test_observed_turnout_row_hides_state_buttons_and_shows_badge() -> None:
    """Passive turnout: no Close/Throw buttons and the "observed" badge
    sits next to the state badge. Edit + Delete remain available so the
    operator can still rename / remove the bean."""
    html = _env.get_template("_turnout_row.html").render(t=_observed_turnout())
    assert ">Close</button>" not in html
    assert ">Throw</button>" not in html
    assert "observed" in html
    assert "data-observed=\"true\"" in html
    # Edit / Delete are not writes to the layout, so they stay.
    assert ">Edit</button>" in html
    assert ">Delete</button>" in html


# ---- sensor row -----------------------------------------------------------


def test_writable_sensor_row_renders_state_buttons() -> None:
    html = _env.get_template("_sensor_row.html").render(
        s={"name": "LS1", "userName": "", "state": 2},
    )
    assert ">Active</button>" in html
    assert ">Inactive</button>" in html
    assert "observed" not in html


def test_observed_sensor_row_hides_state_buttons_and_shows_badge() -> None:
    html = _env.get_template("_sensor_row.html").render(
        s={"name": "MS1", "userName": "", "state": 2, "writable": False},
    )
    assert ">Active</button>" not in html
    assert ">Inactive</button>" not in html
    assert "observed" in html
    assert "data-observed=\"true\"" in html


# ---- power panel ----------------------------------------------------------


def test_writable_power_panel_renders_on_off_buttons() -> None:
    html = _env.get_template("_power_panel.html").render(
        power={"name": "L", "state": 2},
    )
    assert ">Turn ON</button>" in html
    assert ">Turn OFF</button>" in html
    assert "observed" not in html


def test_observed_power_panel_hides_on_off_buttons() -> None:
    """When the connection is passive (``jmri-ncemonitor``), the panel
    shows power state but no ON/OFF buttons -- the driver can't drive
    track power."""
    html = _env.get_template("_power_panel.html").render(
        power={"name": "M", "state": 2, "writable": False},
    )
    assert ">Turn ON</button>" not in html
    assert ">Turn OFF</button>" not in html
    assert "observed" in html
