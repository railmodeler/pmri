"""Startup connection picker -- shared by GUI and headless paths.

When the active profile's ``profile.xml`` has >1 connection and none
is flagged ``default="true"``, the launcher prompts the operator to
pick which one the web service should bring up. Zero or one
connections short-circuit the prompt via
:meth:`ProfileSettings.default_connection` -- those paths never land
here.

Two front-ends share the resolver:

* :func:`pick_connection_cli` -- stdin/stdout numeric prompt. Used by
  ``--headless``. Returning None (operator pressed ^C or picked a
  non-existent index three times) aborts the launcher.
* :func:`pick_connection_dialog` -- modal customtkinter dialog. Used
  by the GUI. Imported lazily so a headless install without
  customtkinter still runs.
"""
from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pmri_core.persistence import ConnectionDef, ProfileSettings
    from pmri_core.persistence.profile_registry import ProfileRef

_log = logging.getLogger(__name__)


def resolve_connection(
    settings: ProfileSettings,
    *,
    override_name: str | None = None,
    headless: bool,
) -> ConnectionDef | None:
    """One-stop resolver for the picked connection.

    Order:

    1. ``override_name`` (``--connection NAME`` CLI flag) wins
       unconditionally -- useful for scripting and tests.
    2. If the profile has 0 connections, returns None so the
       launcher falls back to InternalConnection.
    3. If exactly one connection is defined OR one is flagged
       ``default=true``, that one wins silently.
    4. Otherwise, prompt via the appropriate front-end.

    Returning None at step 4 means the operator cancelled; the caller
    should abort the launcher (not fall back to Internal) so the
    operator isn't surprised by a run against the wrong driver.
    """
    if override_name is not None:
        picked = settings.connection_by_name(override_name)
        if picked is None:
            _log.error(
                "--connection %r not found in profile.xml; aborting",
                override_name,
            )
            return None
        return picked

    default = settings.default_connection()
    if default is not None:
        return default
    if not settings.connections:
        return None

    # Multiple connections, no default -> prompt.
    if headless:
        return pick_connection_cli(settings.connections)
    return pick_connection_dialog(settings.connections)


def pick_connection_cli(
    choices: list[ConnectionDef],
) -> ConnectionDef | None:
    """Numeric prompt on stdin. Returns the picked ConnectionDef or
    None after three bad attempts / EOF."""
    print("Multiple connections defined in profile.xml; pick one:", file=sys.stderr)
    for i, c in enumerate(choices, start=1):
        print(f"  [{i}] {c.name}  (type={c.type})", file=sys.stderr)
    for _ in range(3):
        try:
            raw = input("Pick #: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("", file=sys.stderr)
            return None
        try:
            idx = int(raw)
        except ValueError:
            print(f"  {raw!r} isn't an integer", file=sys.stderr)
            continue
        if 1 <= idx <= len(choices):
            return choices[idx - 1]
        print(f"  pick 1..{len(choices)}", file=sys.stderr)
    print("Three bad attempts; giving up.", file=sys.stderr)
    return None


def pick_connection_dialog(
    choices: list[ConnectionDef],
) -> ConnectionDef | None:
    """Modal customtkinter dialog. Imported lazily so headless
    installs without customtkinter still work via
    :func:`pick_connection_cli`."""
    try:
        import customtkinter as ctk  # noqa: PLC0415
    except ImportError:
        _log.warning(
            "customtkinter not installed; falling back to CLI picker",
        )
        return pick_connection_cli(choices)

    picked: list[ConnectionDef | None] = [None]

    root = ctk.CTk()
    root.title("PMRI -- pick a connection")
    root.geometry("360x200")
    label = ctk.CTkLabel(
        root,
        text=(
            "profile.xml defines multiple connections.\n"
            "Pick which one to bring up on this run:"
        ),
    )
    label.pack(padx=20, pady=10)

    names = [f"{c.name}  (type={c.type})" for c in choices]
    choice_var = ctk.StringVar(value=names[0])
    menu = ctk.CTkOptionMenu(root, variable=choice_var, values=names)
    menu.pack(padx=20, pady=10)

    def _on_ok() -> None:
        picked[0] = choices[names.index(choice_var.get())]
        root.destroy()

    def _on_cancel() -> None:
        picked[0] = None
        root.destroy()

    btns = ctk.CTkFrame(root)
    btns.pack(padx=20, pady=10)
    ctk.CTkButton(btns, text="OK", command=_on_ok, width=100).pack(
        side="left", padx=5,
    )
    ctk.CTkButton(btns, text="Cancel", command=_on_cancel, width=100).pack(
        side="left", padx=5,
    )
    root.protocol("WM_DELETE_WINDOW", _on_cancel)
    root.mainloop()
    return picked[0]


# ---- profile picker (used by pmri_pmri.discovery step 4) ----------------
#
# Mirrors the connection picker's shape: shared CLI + Tk front-ends and
# the same "three bad attempts" / "EOF -> None" conventions.


def pick_profile_cli(
    choices: list[ProfileRef],
) -> ProfileRef | None:
    """Numeric prompt for a profile on stdin. Used by ``--headless``
    (and by test harnesses). Returns the picked ProfileRef or None
    after three bad attempts / EOF."""
    print(
        "Multiple profiles defined in profiles.xml; pick one:", file=sys.stderr,
    )
    for i, p in enumerate(choices, start=1):
        print(f"  [{i}] {p.name}  (id={p.id})", file=sys.stderr)
    for _ in range(3):
        try:
            raw = input("Pick #: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("", file=sys.stderr)
            return None
        try:
            idx = int(raw)
        except ValueError:
            print(f"  {raw!r} isn't an integer", file=sys.stderr)
            continue
        if 1 <= idx <= len(choices):
            return choices[idx - 1]
        print(f"  pick 1..{len(choices)}", file=sys.stderr)
    print("Three bad attempts; giving up.", file=sys.stderr)
    return None


def pick_profile_dialog(
    choices: list[ProfileRef],
) -> ProfileRef | None:
    """Modal customtkinter dialog for picking a profile. Imported
    lazily so a headless install without customtkinter still works
    via :func:`pick_profile_cli`."""
    try:
        import customtkinter as ctk  # noqa: PLC0415
    except ImportError:
        _log.warning(
            "customtkinter not installed; falling back to CLI picker",
        )
        return pick_profile_cli(choices)

    picked: list[ProfileRef | None] = [None]

    root = ctk.CTk()
    root.title("PMRI -- pick a profile")
    root.geometry("400x220")
    label = ctk.CTkLabel(
        root,
        text=(
            "profiles.xml defines multiple profiles and none is marked\n"
            "active. Pick which one to open on this run (the choice\n"
            "will be remembered for next launch):"
        ),
    )
    label.pack(padx=20, pady=10)

    names = [f"{p.name}  (id={p.id})" for p in choices]
    choice_var = ctk.StringVar(value=names[0])
    menu = ctk.CTkOptionMenu(root, variable=choice_var, values=names)
    menu.pack(padx=20, pady=10)

    def _on_ok() -> None:
        picked[0] = choices[names.index(choice_var.get())]
        root.destroy()

    def _on_cancel() -> None:
        picked[0] = None
        root.destroy()

    btns = ctk.CTkFrame(root)
    btns.pack(padx=20, pady=10)
    ctk.CTkButton(btns, text="OK", command=_on_ok, width=100).pack(
        side="left", padx=5,
    )
    ctk.CTkButton(btns, text="Cancel", command=_on_cancel, width=100).pack(
        side="left", padx=5,
    )
    root.protocol("WM_DELETE_WINDOW", _on_cancel)
    root.mainloop()
    return picked[0]
