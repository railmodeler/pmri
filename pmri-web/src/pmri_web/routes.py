"""Template routes -- every human-facing page lives here.

The routes call into the registered :class:`JsonService` instances directly
rather than going over HTTP, since we're in the same process. htmx-driven
mutations (POST endpoints) update the state and return a small HTML
fragment that replaces just the affected table row or status block.
"""
from __future__ import annotations

import re
from html import escape as _html_escape
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from fastapi import Body, FastAPI, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse
from pmri_server.envelope import JmriRequest
from pmri_server.service import JsonServiceError

if TYPE_CHECKING:
    from fastapi.templating import Jinja2Templates
    from pmri_server.app import AppHandle


# ---------- helpers ---------------------------------------------------------


import time as _time  # noqa: E402
_STATIC_BUILD_TAG = str(int(_time.time()))


def _topics_with_help(handle: AppHandle) -> list[dict[str, Any]]:
    """List every JSON topic paired with its :class:`TopicHelp` entry.

    Feeds the accordion under the "Topics available on the JSON API"
    card on the home tab. Topics with no registered help still show
    as expandable badges -- the panel just says "no help authored yet"
    -- so a newly-added service without a help entry doesn't hide
    from the operator.
    """
    from pmri_server import topic_help  # noqa: PLC0415
    out: list[dict[str, Any]] = []
    for topic in handle.registry.topics():
        entry = topic_help.get(topic)
        out.append({
            "topic": topic,
            "help": entry,
        })
    return out


_LOGIXNG_DEFAULT_FIELDS: dict[str, dict[str, Any]] = {
    # Each entry is the minimum field set a decoder needs to construct
    # the node from a dict. Values are chosen to be valid-but-blank so
    # the user Edits them after Add. Containers get empty child slots.
    "action:set-turnout":    {"turnout": "", "state": "CLOSED"},
    "action:set-sensor":     {"sensor": "", "state": "INACTIVE"},
    "action:set-light":      {"light": "", "state": "OFF"},
    "action:set-memory":     {"memory": "", "value": ""},
    "action:log":            {"message": "", "level": "info"},
    "action:delay":          {"seconds": 1.0},
    "action:write-cv":       {"cv": 1, "value": 0},
    "action:read-cv":        {"cv": 1},
    "action:script":         {"source": ""},
    "action:sequence":       {"actions": []},
    "action:if-then-else":   {
        "condition": {"type": "expr:const-bool", "value": False},
        "then": {"type": "action:log", "message": ""},
    },
    "action:set-memory-from-expression": {
        "memory": "",
        "source": {"type": "expr:const-str", "value": ""},
    },
    "logixng:conditional":   {"enabled": True, "actions": []},
    "expr:const-bool":       {"value": False},
    "expr:const-num":        {"value": 0.0},
    "expr:const-str":        {"value": ""},
    "expr:sensor-active":    {"sensor": ""},
    "expr:turnout-closed":   {"turnout": ""},
    "expr:light-on":         {"light": ""},
    "expr:memory-equals":    {"memory": "", "value": ""},
    "expr:memory-value-num": {"memory": ""},
    "expr:memory-value-str": {"memory": ""},
    "expr:and":              {"children": []},
    "expr:or":               {"children": []},
    "expr:not":              {"child": {"type": "expr:const-bool", "value": False}},
    "expr:num-op":           {
        "op": "add",
        "left": {"type": "expr:const-num", "value": 0.0},
        "right": {"type": "expr:const-num", "value": 0.0},
    },
    "expr:num-compare":      {
        "op": "eq",
        "left": {"type": "expr:const-num", "value": 0.0},
        "right": {"type": "expr:const-num", "value": 0.0},
    },
    "expr:string-compare":   {
        "op": "eq",
        "left": {"type": "expr:const-str", "value": ""},
        "right": {"type": "expr:const-str", "value": ""},
    },
    "expr:string-concat":    {"children": []},
}


def _logixng_default_child(type_tag: str) -> dict[str, Any]:
    """Return a decoder-valid default dict for *type_tag*. Falls back
    to just ``{"type": type_tag}`` for leaf types with no required
    fields. Used by the add-child route when the picker form omits
    specific field inputs."""
    base = {"type": type_tag}
    base.update(_LOGIXNG_DEFAULT_FIELDS.get(type_tag, {}))
    return base


def _qualifier_exists_want_present(raw: str) -> bool:
    """Decode a ``<qualifier relation=exists><value>…`` field.

    JMRI's schema types ``<value>`` as ``xs:int`` and JMRI Java's
    ``ArithmeticQualifier`` reads non-zero as "require present" and
    zero as "require absent". Some decoder files use the strings
    ``yes``/``no`` or ``true``/``false`` instead; accept both.
    Empty/malformed defaults to "require present" (the natural
    reading of a bare ``<value/>``-less exists qualifier).
    """
    if raw is None or raw == "":
        return True
    s = str(raw).strip().lower()
    if s in ("0", "no", "false"):
        return False
    try:
        return int(s) != 0
    except ValueError:
        return True


def _variable_qualifiers_pass(
    var: Any,
    pid_by_item: dict[str, Any],
    pid_cv_bytes: dict[str, int],
) -> bool:
    """True iff every ``<qualifier>`` on *var* passes.

    Qualifier gates per JMRI's ``ArithmeticQualifier``:

    * ``exists`` -- presence check against *pid_by_item*; see
      :func:`_qualifier_exists_want_present` for the value coercion.
    * ``eq`` / ``ne`` / ``gt`` / ``ge`` / ``lt`` / ``le`` -- compare the
      ref variable's current value (extracted from *pid_cv_bytes*) to
      the integer ``<value>``.

    Permissive-by-default on malformed input (unreadable ref value,
    non-integer ``<value>`` for a numeric relation, ref filtered out
    by productID for a numeric relation) -- matches JMRI's tolerance
    for decoder files that share qualifier snippets across models.
    The ``exists`` branch is NOT permissive: that's its whole job.

    Factored out of :meth:`_pane_context_from_definition` so the
    filter can be unit-tested without the full pane-render stack.
    """
    # Local imports: this module's import graph includes pmri_server
    # internals; the two names below are the only decoder-xml-level
    # dependencies the filter needs.
    from pmri_core.persistence.decoder_xml import (  # noqa: PLC0415
        UnsupportedVariableKind,
        variable_extract_from_store,
    )
    for q in var.qualifiers:
        ref_name = q.get("variableref", "")
        rel = q.get("relation", "")
        raw_value = q.get("value", "")
        ref = pid_by_item.get(ref_name)

        if rel == "exists":
            want_present = _qualifier_exists_want_present(raw_value)
            if (ref is not None) != want_present:
                return False
            continue

        if ref is None:
            continue
        try:
            current = variable_extract_from_store(ref, pid_cv_bytes)
        except UnsupportedVariableKind:
            continue
        if current is None:
            continue
        try:
            target = int(raw_value)
        except (TypeError, ValueError):
            continue
        if rel == "eq" and current != target: return False
        if rel == "ne" and current == target: return False
        if rel == "gt" and current <= target: return False
        if rel == "ge" and current <  target: return False
        if rel == "lt" and current >= target: return False
        if rel == "le" and current >  target: return False
    return True


def _panel_xml_bytes(ctx: Any, name: str) -> bytes | None:
    """Return the raw XML for a profile-stored panel, or None.

    Shares the resolver with :class:`~pmri_server.services.panel.PanelService`
    (``_panels_dir``) so uploads via the JSON API, listings on /panel,
    and SVG renders on /panels/{name} all see the same directory on
    disk. *name* is treated as a filesystem basename; path separators
    and traversal segments are rejected before touching the FS.
    """
    import pathlib  # noqa: PLC0415

    from pmri_server.services.panel import _panels_dir  # noqa: PLC0415

    if "/" in name or "\\" in name or name in ("", ".", ".."):
        return None
    panels_dir = _panels_dir(ctx)
    # ``_panels_dir`` consults ctx._panels_dir, then
    # ctx.config_store.profile.panels_dir, then Profile.default().
    # UI tests often wire only ``_roster_profile``; honour that too.
    if panels_dir is None:
        profile = getattr(ctx, "_roster_profile", None)
        if profile is None:
            return None
        panels_dir = pathlib.Path(profile.profile_dir) / "panels"
        if not panels_dir.is_dir():
            return None
    for cand in (panels_dir / f"{name}.xml", panels_dir / name):
        if cand.is_file():
            try:
                return cand.read_bytes()
            except OSError:
                return None
    return None


def _panel_bean_state_for(ctx: Any) -> Callable[[str, str], int | None]:
    """Build a ``bean_state(kind, name) -> int | None`` closure over
    the active connection's bean managers. Returned function is cheap
    enough to call per-icon during SVG rendering."""
    conn = getattr(ctx, "connection", None)

    def lookup(kind: str, name: str) -> int | None:
        if conn is None or not name:
            return None
        if kind == "sensor":
            mgr = getattr(conn, "sensors", None)
            if mgr is None:
                return None
            bean = mgr.get(name)
            if bean is None:
                return None
            return int(bean.known_state)
        if kind == "turnout":
            mgr = getattr(conn, "turnouts", None)
            if mgr is None:
                return None
            bean = mgr.get(name)
            if bean is None:
                return None
            return int(bean.known_state)
        return None

    return lookup


def _logixng_coerce_field(key: str, raw: Any, type_tag: str) -> Any:
    """Convert a form value to the shape ``from_dict`` expects.

    HTML forms deliver everything as strings. The LogixNG decoders
    expect booleans, ints, and floats to be typed. Rules:

    * Checkbox fields (``enabled``, ``longAddress``, ``long_address``,
      ``inverted``, ``case_insensitive``) -> ``bool`` (presence = True).
    * Int-shaped field names (``cv``, ``value`` on CV actions,
      ``address``, ``dt_factor``, ``seconds`` on action:delay as
      float) -> appropriate numeric type.
    * All other scalars round-trip as strings; the decoder validates
      per field downstream.

    Unknown keys pass through so a new decoder field doesn't need to
    update this coercion table.
    """
    raw_s = raw if isinstance(raw, str) else str(raw)
    # Treat a form-echoed "on" / "true" / "1" on checkbox-ish fields
    # as boolean. HTML checkboxes send "on" (or whatever value= set)
    # only when checked -- the server never sees them when unchecked.
    bool_keys = {
        "enabled", "longAddress", "long_address", "inverted",
        "case_insensitive",
    }
    if key in bool_keys:
        return raw_s not in ("", "0", "false", "False", "off")
    if key == "value" and type_tag in (
        "expr:const-bool",
    ):
        return raw_s not in ("", "0", "false", "False")
    if key == "value" and type_tag == "expr:const-num":
        try:
            return float(raw_s)
        except ValueError:
            return 0.0
    if key in ("cv",) or (
        key == "value" and type_tag in (
            "action:write-cv", "action:read-cv",
        )
    ):
        try:
            return int(raw_s)
        except ValueError:
            return 0
    if key == "address" and type_tag in (
        "action:write-cv", "action:read-cv",
    ):
        # Blank address field means service-mode (None).
        if raw_s == "":
            return None
        try:
            return int(raw_s)
        except ValueError:
            return None
    if key in ("seconds",) and type_tag == "action:delay":
        try:
            return float(raw_s)
        except ValueError:
            return 0.0
    if key in ("dt_factor",):
        try:
            return int(raw_s)
        except ValueError:
            return 1
    return raw_s


def _base_context(handle: AppHandle, active: str) -> dict[str, Any]:
    m = handle.ctx.metadata
    return {
        "active": active,
        "railroad": m.railroad,
        "pmri_version": m.pmri_version,
        # Cache-busting query string appended to /static/* URLs. Every
        # server restart bumps it so browsers always fetch the fresh
        # JS/CSS regardless of their cache policy -- otherwise a code
        # change on our side is invisible until the user does a hard
        # refresh (which some setups have their own problems with).
        "static_v": _STATIC_BUILD_TAG,
    }


async def _dispatch(
    handle: AppHandle, request: Request | None = None, **kwargs: Any,
) -> Any:
    req = JmriRequest.model_validate(kwargs)
    try:
        # When auth is enabled the middleware populates
        # ``request.state.role`` and ``request.state.user``;
        # forward both so the same gating rules apply to
        # web-driven mutations as to raw JSON calls, and
        # self-service topics (``configUser``) can attribute
        # the request to the current user. Without a request
        # (some internal calls) the registry defaults apply.
        if request is not None:
            role = getattr(request.state, "role", None)
            user = getattr(request.state, "user", None)
            username = user.username if user is not None else None
            if role is not None:
                resp = await handle.registry.dispatch(
                    req, role=role, username=username,
                )
            else:
                resp = await handle.registry.dispatch(
                    req, username=username,
                )
        else:
            resp = await handle.registry.dispatch(req)
    except JsonServiceError as e:
        raise HTTPException(status_code=e.code, detail=e.message) from None
    return resp.data


# ---------- routes ---------------------------------------------------------


def install_template_routes(
    app: FastAPI, handle: AppHandle, templates: Jinja2Templates
) -> None:
    # ---- auth: /login + /logout ----------------------------------------
    # These routes only do something meaningful when
    # ``JMRI_AUTH_ENABLED=1``. With auth off, ``ctx.auth`` is None and
    # /login shows a friendly "auth is disabled" message rather than
    # a form that couldn't do anything anyway.

    from pmri_server.auth.middleware import SESSION_COOKIE  # noqa: PLC0415

    def _csrf_token(request: Request) -> str:
        """Get-or-set a CSRF token in the ``JMRICSRF`` cookie.

        Double-submit-cookie CSRF pattern: the same random token is
        embedded in the form AND in a cookie; on POST the server
        checks they match. A cross-site attacker can't read the
        cookie (SameSite=Lax) so can't forge a matching form field.
        """
        existing = request.cookies.get("JMRICSRF")
        if existing:
            return existing
        import secrets  # noqa: PLC0415
        return secrets.token_urlsafe(24)

    def _verify_csrf(request: Request, submitted: str) -> bool:
        cookie = request.cookies.get("JMRICSRF", "")
        if not cookie or not submitted:
            return False
        import hmac  # noqa: PLC0415
        return hmac.compare_digest(cookie, submitted)

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(
        request: Request, next: str = "/",
    ) -> Any:  # type: ignore[valid-type]
        auth = getattr(handle.ctx, "auth", None)
        error = None
        if auth is None:
            error = "Auth is disabled on this server (JMRI_AUTH_ENABLED is unset). Every request runs as admin."
        token = _csrf_token(request)
        response = templates.TemplateResponse(
            request, "login.html",
            _base_context(handle, "login") | {
                "csrf_token": token,
                "next": next,
                "error": error,
            },
        )
        # SameSite=Lax so cross-site GETs don't leak the token; the
        # form POST is same-origin so it lines up.
        response.set_cookie(
            "JMRICSRF", token, samesite="lax",
            httponly=False, max_age=3600,
        )
        return response

    @app.post("/login", response_class=HTMLResponse)
    async def login_submit(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
        next: str = Form("/"),
        csrf: str = Form("", alias="_csrf"),
    ) -> Any:  # type: ignore[valid-type]
        from fastapi.responses import RedirectResponse  # noqa: PLC0415
        auth = getattr(handle.ctx, "auth", None)
        if auth is None:
            # Auth off -- no login needed; just bounce to home.
            return RedirectResponse(url=next or "/", status_code=303)
        if not _verify_csrf(request, csrf):
            token = _csrf_token(request)
            response = templates.TemplateResponse(
                request, "login.html",
                _base_context(handle, "login") | {
                    "csrf_token": token,
                    "next": next,
                    "error": "Session expired -- try again.",
                },
                status_code=400,
            )
            response.set_cookie(
                "JMRICSRF", token, samesite="lax",
                httponly=False, max_age=3600,
            )
            return response
        result = auth.login_password(username, password)
        if result is None:
            token = _csrf_token(request)
            response = templates.TemplateResponse(
                request, "login.html",
                _base_context(handle, "login") | {
                    "csrf_token": token,
                    "next": next,
                    "error": "Invalid username or password.",
                },
                status_code=401,
            )
            response.set_cookie(
                "JMRICSRF", token, samesite="lax",
                httponly=False, max_age=3600,
            )
            return response
        response = RedirectResponse(url=next or "/", status_code=303)
        cookie_value = auth.mint_session_cookie(result.user)
        # httponly so JS can't touch it (defends against XSS
        # session theft). secure=False for now since we run on
        # plain HTTP LANs; flip to True when we grow HTTPS.
        response.set_cookie(
            SESSION_COOKIE, cookie_value,
            httponly=True, samesite="lax",
            max_age=7 * 24 * 3600,
        )
        return response

    @app.post("/logout")
    async def logout(request: Request) -> Any:  # type: ignore[valid-type]
        from fastapi.responses import RedirectResponse  # noqa: PLC0415
        response = RedirectResponse(url="/login", status_code=303)
        response.delete_cookie(SESSION_COOKIE)
        return response

    # ---- /users admin + self-service -----------------------------------

    async def _render_users(
        request: Request,
        *,
        notice: str | None = None,
        new_token: str | None = None,
        new_token_user: str | None = None,
    ) -> Any:
        """Fetch the user list (as admin, if the caller is admin) and
        render the /users template. Non-admin callers see an empty
        list -- the template hides the admin sections in that case."""
        from pmri_server.auth.models import Role  # noqa: PLC0415
        role = getattr(request.state, "role", Role.ADMIN)
        is_admin = role is Role.ADMIN
        users: list[dict[str, Any]] = []
        if is_admin:
            try:
                users = await _dispatch(
                    handle, request, type="configUser", method="list",
                )
            except HTTPException:
                users = []
        token = _csrf_token(request)
        response = templates.TemplateResponse(
            request, "users.html",
            _base_context(handle, "users") | {
                "users": users,
                "is_admin": is_admin,
                "csrf_token": token,
                "notice": notice,
                "new_token": new_token,
                "new_token_user": new_token_user,
            },
        )
        response.set_cookie(
            "JMRICSRF", token, samesite="lax",
            httponly=False, max_age=3600,
        )
        return response

    @app.get("/users", response_class=HTMLResponse)
    async def users_home(request: Request) -> Any:  # type: ignore[valid-type]
        return await _render_users(request)

    @app.post("/users/create", response_class=HTMLResponse)
    async def users_create(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
        role: str = Form("operator"),
        csrf: str = Form("", alias="_csrf"),
    ) -> Any:  # type: ignore[valid-type]
        if not _verify_csrf(request, csrf):
            return await _render_users(request, notice="Session expired -- try again.")
        try:
            await _dispatch(
                handle, request, type="configUser", method="put",
                data={"username": username, "password": password, "role": role},
            )
        except HTTPException as exc:
            return await _render_users(request, notice=f"Create failed: {exc.detail}")
        return await _render_users(request, notice=f"Created user {username}.")

    # ---- self-service routes register FIRST so the /users/{username}
    # ---- parameterized routes below don't shadow /users/self/* with
    # ---- username="self". Starlette matches routes in registration
    # ---- order; the more specific paths must come first.

    @app.post("/users/self/change-password", response_class=HTMLResponse)
    async def users_self_change_password_early(
        request: Request,
        new_password: str = Form(...),
        csrf: str = Form("", alias="_csrf"),
    ) -> Any:  # type: ignore[valid-type]
        if not _verify_csrf(request, csrf):
            return await _render_users(request, notice="Session expired.")
        user = getattr(request.state, "user", None)
        if user is None:
            return await _render_users(
                request, notice="Not logged in -- password change unavailable.",
            )
        try:
            await _dispatch(
                handle, request, type="configUser", method="post",
                data={
                    "action": "change_password",
                    "username": user.username,
                    "new_password": new_password,
                },
            )
        except HTTPException as exc:
            return await _render_users(
                request, notice=f"Password change failed: {exc.detail}",
            )
        return await _render_users(
            request, notice="Password updated.",
        )

    @app.post("/users/self/regenerate-token", response_class=HTMLResponse)
    async def users_self_regen_token_early(
        request: Request,
        csrf: str = Form("", alias="_csrf"),
    ) -> Any:  # type: ignore[valid-type]
        if not _verify_csrf(request, csrf):
            return await _render_users(request, notice="Session expired.")
        user = getattr(request.state, "user", None)
        if user is None:
            return await _render_users(
                request, notice="Not logged in -- token regenerate unavailable.",
            )
        try:
            resp = await _dispatch(
                handle, request, type="configUser", method="post",
                data={
                    "action": "regenerate_token",
                    "username": user.username,
                },
            )
        except HTTPException as exc:
            return await _render_users(
                request, notice=f"Regenerate failed: {exc.detail}",
            )
        return await _render_users(
            request,
            new_token=resp["token"],
            new_token_user=user.username,
        )

    @app.post("/users/{username}/delete", response_class=HTMLResponse)
    async def users_delete(
        request: Request,
        username: str,
        csrf: str = Form("", alias="_csrf"),
    ) -> Any:  # type: ignore[valid-type]
        if not _verify_csrf(request, csrf):
            return await _render_users(request, notice="Session expired.")
        try:
            await _dispatch(
                handle, request, type="configUser", method="delete",
                data={"username": username},
            )
        except HTTPException as exc:
            return await _render_users(request, notice=f"Delete failed: {exc.detail}")
        return await _render_users(request, notice=f"Deleted user {username}.")

    @app.post("/users/{username}/set-role", response_class=HTMLResponse)
    async def users_set_role(
        request: Request,
        username: str,
        role: str = Form(...),
        csrf: str = Form("", alias="_csrf"),
    ) -> Any:  # type: ignore[valid-type]
        if not _verify_csrf(request, csrf):
            return await _render_users(request, notice="Session expired.")
        try:
            await _dispatch(
                handle, request, type="configUser", method="put",
                data={"username": username, "role": role},
            )
        except HTTPException as exc:
            return await _render_users(
                request, notice=f"Role change failed: {exc.detail}",
            )
        return await _render_users(
            request, notice=f"{username} -> {role}",
        )

    @app.post("/users/{username}/regenerate-token", response_class=HTMLResponse)
    async def users_regen_token(
        request: Request,
        username: str,
        csrf: str = Form("", alias="_csrf"),
    ) -> Any:  # type: ignore[valid-type]
        if not _verify_csrf(request, csrf):
            return await _render_users(request, notice="Session expired.")
        try:
            resp = await _dispatch(
                handle, request, type="configUser", method="post",
                data={"action": "regenerate_token", "username": username},
            )
        except HTTPException as exc:
            return await _render_users(
                request, notice=f"Regenerate failed: {exc.detail}",
            )
        return await _render_users(
            request,
            new_token=resp["token"],
            new_token_user=username,
        )

    # The decoder catalog is used by both the roster page (to gate the
    # Program button) and the programmer routes further down. Kept on
    # ``handle.ctx._decoder_catalog`` so tests / other subsystems can
    # populate the catalog directly. Routes fetch it via ``_decoder_catalog``
    # on every request so a runtime swap is honoured.
    from pmri_decoderpro import DecoderCatalog  # noqa: PLC0415

    if not hasattr(handle.ctx, "_decoder_catalog"):
        handle.ctx._decoder_catalog = DecoderCatalog()  # type: ignore[attr-defined]

    def _catalog() -> DecoderCatalog:
        return handle.ctx._decoder_catalog  # type: ignore[attr-defined,no-any-return]

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request) -> Any:
        # Diagnostic timing -- turned on with ``JMRI_TIME_HOME=1``. Logs
        # per-dispatch and total render time at WARNING so it shows up
        # in ``uvicorn.log`` without cranking log verbosity globally.
        # Off by default so production users see no extra chatter.
        import time as _time  # noqa: PLC0415
        from pmri_core.env import time_home as _time_home  # noqa: PLC0415
        _diag = _time_home()
        _t0 = _time.monotonic() if _diag else 0.0
        power = await _dispatch(handle, type="power", method="get")
        _t1 = _time.monotonic() if _diag else 0.0
        fast_clock = await _dispatch(handle, type="time", method="get")
        _t2 = _time.monotonic() if _diag else 0.0
        if _diag:
            import logging as _logging  # noqa: PLC0415
            _logging.getLogger("pmri_web.timing").warning(
                "GET / dispatch  power=%.0fms  time=%.0fms",
                (_t1 - _t0) * 1000, (_t2 - _t1) * 1000,
            )
        registry = getattr(handle.ctx, "profile_registry", None)
        profiles: list[dict[str, Any]] = []
        active_id: str | None = None
        settings_root_str: str | None = None
        if registry is not None:
            active = registry.active()
            active_id = active.id if active else None
            profiles = [
                {"id": p.id, "name": p.name, "path": p.path}
                for p in registry.profiles
            ]
            settings_root_str = str(registry.settings_root)
        _t3 = _time.monotonic() if _diag else 0.0
        log_tail_ctx = await _log_tail_context(handle)
        resp = templates.TemplateResponse(
            request, "home.html",
            _base_context(handle, "home") | {
                "power": power,
                "fast_clock": fast_clock,
                "connection": handle.ctx.connection,
                "command_station_on": bool(
                    getattr(handle.ctx.connection,
                            "is_command_station_on", False)
                ),
                "topics": _topics_with_help(handle),
                "profiles": profiles,
                "active_profile_id": active_id,
                "settings_root": settings_root_str,
            } | log_tail_ctx,
        )
        if _diag:
            _t4 = _time.monotonic()
            import logging as _logging  # noqa: PLC0415
            _logging.getLogger("pmri_web.timing").warning(
                "GET / render  ctx=%.0fms  tmpl=%.0fms  TOTAL=%.0fms",
                (_t3 - _t2) * 1000, (_t4 - _t3) * 1000,
                (_t4 - _t0) * 1000,
            )
        return resp

    @app.post("/power/set", response_class=HTMLResponse)
    async def set_power(request: Request, state: int = Form(...)) -> Any:
        data = await _dispatch(handle, type="power", method="post", data={"state": state})
        return templates.TemplateResponse(
            request, "_power_panel.html", {"power": data},
        )

    @app.post("/profiles/load", response_class=HTMLResponse)
    async def load_profile(request: Request, profile_id: str = Form(...)) -> Any:
        """Switch the active profile. Clears in-memory beans, re-points
        ``ConfigStore`` and the roster store, repopulates from the new
        profile's ``PanelPro.xml``. Hardware connection is untouched."""
        from pmri_server.app import switch_active_profile  # noqa: PLC0415
        # Diagnostic timing -- opt in with ``JMRI_TIME_HOME=1``. Logs
        # per-phase durations at WARNING so they surface in uvicorn.log
        # without dialing up global log verbosity. Off by default.
        import logging as _logging  # noqa: PLC0415
        import time as _time  # noqa: PLC0415
        from pmri_core.env import time_home as _time_home  # noqa: PLC0415
        _diag = _time_home()
        _log = _logging.getLogger("pmri_web.timing")
        _t0 = _time.monotonic() if _diag else 0.0
        try:
            await switch_active_profile(handle, profile_id)
        except (KeyError, RuntimeError) as e:
            raise HTTPException(status_code=400, detail=str(e)) from None
        _t1 = _time.monotonic() if _diag else 0.0
        # Re-render the home page with the new active profile.
        power = await _dispatch(handle, type="power", method="get")
        _t2 = _time.monotonic() if _diag else 0.0
        fast_clock = await _dispatch(handle, type="time", method="get")
        _t3 = _time.monotonic() if _diag else 0.0
        registry = getattr(handle.ctx, "profile_registry", None)
        active = registry.active() if registry is not None else None
        if _diag:
            _log.warning(
                "POST /profiles/load  switch=%.0fms  power=%.0fms  time=%.0fms",
                (_t1 - _t0) * 1000, (_t2 - _t1) * 1000, (_t3 - _t2) * 1000,
            )
        return templates.TemplateResponse(
            request, "home.html",
            _base_context(handle, "home") | {
                "power": power,
                "fast_clock": fast_clock,
                "connection": handle.ctx.connection,
                "command_station_on": bool(
                    getattr(handle.ctx.connection,
                            "is_command_station_on", False)
                ),
                "topics": _topics_with_help(handle),
                "profiles": [
                    {"id": p.id, "name": p.name, "path": p.path}
                    for p in (registry.profiles if registry is not None else [])
                ],
                "active_profile_id": active.id if active else None,
                "load_notice": f"Loaded profile: {active.name}" if active else None,
            },
        )

    # ----- throttle ---------------------------------------------------------

    @app.get("/throttle", response_class=HTMLResponse)
    async def throttle(
        request: Request,
        address: int | None = None,
        long: int | None = None,
    ) -> Any:
        """Throttle page. Without query args, returns to the most recently
        acquired throttle (single-user alpha; ``ctx._last_throttle`` stores
        ``(address, long_address)``). Falls back to address 3 if no history.
        """
        last = getattr(handle.ctx, "_last_throttle", None)
        if address is None:
            if isinstance(last, tuple) and len(last) == 2:
                address, last_long = int(last[0]), bool(last[1])
                if long is None:
                    long = 1 if last_long else 0
            else:
                address = 3
        # NMRA default: addresses >= 128 are long unless explicitly overridden.
        long_address = (address >= 128) if long is None else bool(long)
        data = await _dispatch(
            handle, type="throttle", method="put",
            data={
                "address": address,
                "name": str(address),
                "isLongAddress": long_address,
            },
        )
        # Remember this acquisition so the next visit lands here.
        handle.ctx._last_throttle = (address, long_address)  # type: ignore[attr-defined]
        # Surface roster entries so the page can build a picker.
        roster = await _dispatch(handle, type="roster", method="list")
        return templates.TemplateResponse(
            request, "throttle.html",
            _base_context(handle, "throttle") | {
                "address": address,
                "long_address": long_address,
                "throttle": data,
                "roster": roster,
            },
        )

    @app.post("/throttle/{address}/state", response_class=HTMLResponse)
    async def throttle_set_state(
        request: Request, address: int,
        speed: float = Form(None),
        forward: int = Form(None),
    ) -> Any:
        payload: dict[str, Any] = {"name": str(address)}
        if speed is not None:
            payload["speed"] = speed
        if forward is not None:
            payload["forward"] = bool(forward)
        data = await _dispatch(handle, type="throttle", method="post", data=payload)
        return templates.TemplateResponse(
            request, "_throttle_status.html", {"throttle": data},
        )

    @app.post("/throttle/{address}/function/{index}", response_class=HTMLResponse)
    async def throttle_set_function(
        request: Request, address: int, index: int, on: int = Form(...),
    ) -> Any:
        payload = {"name": str(address), f"F{index}": bool(on)}
        data = await _dispatch(handle, type="throttle", method="post", data=payload)
        return templates.TemplateResponse(
            request, "_throttle_status.html", {"throttle": data},
        )

    @app.post("/throttle/{address}/release", response_class=HTMLResponse)
    async def throttle_release(request: Request, address: int) -> Any:
        await _dispatch(
            handle, type="throttle", method="delete", data={"name": str(address)},
        )
        return templates.TemplateResponse(
            request, "_throttle_status.html",
            {"throttle": {"name": str(address), "released": True}},
        )

    # ----- turnouts ---------------------------------------------------------

    @app.get("/turnouts", response_class=HTMLResponse)
    async def turnouts(request: Request) -> Any:
        items = await _dispatch(handle, type="turnout", method="list")
        return templates.TemplateResponse(
            request, "turnouts.html",
            _base_context(handle, "turnouts") | {"turnouts": items},
        )

    @app.post("/turnouts/create", response_class=HTMLResponse)
    async def create_turnout(request: Request, name: str = Form(...)) -> Any:
        await _dispatch(handle, type="turnout", method="put", data={"name": name})
        items = await _dispatch(handle, type="turnout", method="list")
        return templates.TemplateResponse(
            request, "_turnouts_table.html", {"turnouts": items},
        )

    @app.post("/turnouts/{name}/state", response_class=HTMLResponse)
    async def set_turnout_state(request: Request, name: str, state: int = Form(...)) -> Any:
        data = await _dispatch(
            handle, type="turnout", method="post",
            data={"name": name, "state": state},
        )
        return templates.TemplateResponse(
            request, "_turnout_row.html", {"t": data},
        )

    @app.get("/turnouts/{name}/row", response_class=HTMLResponse)
    async def turnout_row(request: Request, name: str) -> Any:
        """Read-only row partial. Used by the Cancel button to revert an edit."""
        data = await _dispatch(
            handle, type="turnout", method="get", data={"name": name},
        )
        return templates.TemplateResponse(request, "_turnout_row.html", {"t": data})

    @app.get("/turnouts/{name}/edit", response_class=HTMLResponse)
    async def turnout_edit_row(request: Request, name: str) -> Any:
        data = await _dispatch(
            handle, type="turnout", method="get", data={"name": name},
        )
        return templates.TemplateResponse(
            request, "_turnout_row_edit.html", {"t": data},
        )

    @app.post("/turnouts/{name}/update", response_class=HTMLResponse)
    async def turnout_update(
        request: Request,
        name: str,
        userName: str = Form(""),
        inverted: int = Form(0),
    ) -> Any:
        payload: dict[str, Any] = {
            "name": name,
            "userName": userName or None,
            "inverted": bool(inverted),
        }
        data = await _dispatch(handle, type="turnout", method="post", data=payload)
        return templates.TemplateResponse(request, "_turnout_row.html", {"t": data})

    @app.delete("/turnouts/{name}", response_class=HTMLResponse)
    async def turnout_delete(request: Request, name: str) -> Any:
        await _dispatch(handle, type="turnout", method="delete", data={"name": name})
        # HTMX swap target is the row; returning empty deletes it from the DOM.
        return HTMLResponse("")

    # ----- sensors ----------------------------------------------------------

    @app.get("/sensors", response_class=HTMLResponse)
    async def sensors(request: Request) -> Any:
        items = await _dispatch(handle, type="sensor", method="list")
        return templates.TemplateResponse(
            request, "sensors.html",
            _base_context(handle, "sensors") | {"sensors": items},
        )

    @app.post("/sensors/create", response_class=HTMLResponse)
    async def create_sensor(request: Request, name: str = Form(...)) -> Any:
        await _dispatch(handle, type="sensor", method="put", data={"name": name})
        items = await _dispatch(handle, type="sensor", method="list")
        return templates.TemplateResponse(
            request, "_sensors_table.html", {"sensors": items},
        )

    @app.post("/sensors/{name}/state", response_class=HTMLResponse)
    async def set_sensor_state(request: Request, name: str, state: int = Form(...)) -> Any:
        """For the internal-connection simulator: force a sensor active/inactive."""
        data = await _dispatch(
            handle, type="sensor", method="post",
            data={"name": name, "state": state},
        )
        return templates.TemplateResponse(
            request, "_sensor_row.html", {"s": data},
        )

    @app.get("/sensors/{name}/row", response_class=HTMLResponse)
    async def sensor_row(request: Request, name: str) -> Any:
        data = await _dispatch(
            handle, type="sensor", method="get", data={"name": name},
        )
        return templates.TemplateResponse(request, "_sensor_row.html", {"s": data})

    @app.get("/sensors/{name}/edit", response_class=HTMLResponse)
    async def sensor_edit_row(request: Request, name: str) -> Any:
        data = await _dispatch(
            handle, type="sensor", method="get", data={"name": name},
        )
        return templates.TemplateResponse(
            request, "_sensor_row_edit.html", {"s": data},
        )

    @app.post("/sensors/{name}/update", response_class=HTMLResponse)
    async def sensor_update(
        request: Request,
        name: str,
        userName: str = Form(""),
        inverted: int = Form(0),
    ) -> Any:
        payload: dict[str, Any] = {
            "name": name,
            "userName": userName or None,
            "inverted": bool(inverted),
        }
        data = await _dispatch(handle, type="sensor", method="post", data=payload)
        return templates.TemplateResponse(request, "_sensor_row.html", {"s": data})

    @app.delete("/sensors/{name}", response_class=HTMLResponse)
    async def sensor_delete(request: Request, name: str) -> Any:
        await _dispatch(handle, type="sensor", method="delete", data={"name": name})
        return HTMLResponse("")

    # ----- lights -----------------------------------------------------------

    @app.get("/lights", response_class=HTMLResponse)
    async def lights(request: Request) -> Any:
        items = await _dispatch(handle, type="light", method="list")
        return templates.TemplateResponse(
            request, "lights.html",
            _base_context(handle, "lights") | {"lights": items},
        )

    @app.post("/lights/create", response_class=HTMLResponse)
    async def create_light(request: Request, name: str = Form(...)) -> Any:
        await _dispatch(handle, type="light", method="put", data={"name": name})
        items = await _dispatch(handle, type="light", method="list")
        return templates.TemplateResponse(
            request, "_lights_table.html", {"lights": items},
        )

    @app.post("/lights/{name}/state", response_class=HTMLResponse)
    async def set_light_state(request: Request, name: str, state: int = Form(...)) -> Any:
        data = await _dispatch(
            handle, type="light", method="post",
            data={"name": name, "state": state},
        )
        return templates.TemplateResponse(
            request, "_light_row.html", {"l": data},
        )

    @app.get("/lights/{name}/row", response_class=HTMLResponse)
    async def light_row(request: Request, name: str) -> Any:
        data = await _dispatch(
            handle, type="light", method="get", data={"name": name},
        )
        return templates.TemplateResponse(request, "_light_row.html", {"l": data})

    @app.get("/lights/{name}/edit", response_class=HTMLResponse)
    async def light_edit_row(request: Request, name: str) -> Any:
        data = await _dispatch(
            handle, type="light", method="get", data={"name": name},
        )
        return templates.TemplateResponse(
            request, "_light_row_edit.html", {"l": data},
        )

    @app.post("/lights/{name}/update", response_class=HTMLResponse)
    async def light_update(
        request: Request,
        name: str,
        userName: str = Form(""),
    ) -> Any:
        payload: dict[str, Any] = {
            "name": name,
            "userName": userName or None,
        }
        data = await _dispatch(handle, type="light", method="post", data=payload)
        return templates.TemplateResponse(request, "_light_row.html", {"l": data})

    @app.delete("/lights/{name}", response_class=HTMLResponse)
    async def light_delete(request: Request, name: str) -> Any:
        await _dispatch(handle, type="light", method="delete", data={"name": name})
        return HTMLResponse("")

    # ----- roster -----------------------------------------------------------

    async def _roster_view(request: Request, format: str | None = None) -> Any:
        """Engine Driver fetches the roster as XML at ``/roster?format=xml``;
        the web UI fetches the same data as HTML. Branch on the query."""
        if format == "xml":
            # Serve the on-disk roster.xml verbatim if it exists; that's
            # what JMRI's RosterServlet would emit. Engine Driver parses
            # the ``<roster-config>`` shape directly.
            profile = getattr(handle.ctx, "_roster_profile", None)
            if profile is not None and profile.roster_index.exists():
                return Response(
                    content=profile.roster_index.read_bytes(),
                    media_type="application/xml",
                )
            # No roster.xml yet -- synthesize an empty one.
            return Response(
                content=b'<?xml version="1.0" encoding="UTF-8"?>\n'
                        b'<roster-config><roster/></roster-config>\n',
                media_type="application/xml",
            )
        items = await _dispatch(handle, type="roster", method="list")
        return templates.TemplateResponse(
            request, "roster.html",
            _base_context(handle, "roster") | {
                "roster": items,
                "catalog_loaded": len(_catalog()) > 0,
                # For the decoder-picker dropdown. Empty when catalog
                # isn't loaded -- the template falls back to a hint.
                "manufacturers": _catalog().manufacturers(),
            },
        )

    # Both the canonical and trailing-slash forms -- Engine Driver issues
    # ``GET /roster/?format=xml`` and won't follow the FastAPI 307 redirect
    # that the default routing emits for a slash mismatch.
    @app.get("/roster", response_class=HTMLResponse)
    async def roster(request: Request, format: str | None = None) -> Any:
        return await _roster_view(request, format)

    @app.get("/roster/", response_class=HTMLResponse, include_in_schema=False)
    async def roster_slashed(request: Request, format: str | None = None) -> Any:
        return await _roster_view(request, format)

    # ----- Engine Driver / WiThrottle side-channel HTTP endpoints ----------

    @app.get("/prefs/{app_name}/{file_name}", include_in_schema=False)
    async def withrottle_prefs(app_name: str, file_name: str) -> Response:
        """Engine Driver pulls/pushes its own preferences from the server.

        JMRI's WiThrottle server stores these at ``<profile>/preferences/
        engine_driver/auto_preferences.ed`` and serves them back. For the
        alpha we return an empty 200 instead of 404 so Engine Driver
        doesn't panic -- its later "save" will not be persisted, but the
        connection stays healthy.
        """
        profile = getattr(handle.ctx, "_roster_profile", None)
        if profile is not None:
            path = profile.preferences_dir / app_name / file_name
            if path.exists():
                return Response(
                    content=path.read_bytes(),
                    media_type="application/octet-stream",
                )
        return Response(content=b"", media_type="application/octet-stream")

    @app.put("/prefs/{app_name}/{file_name}", include_in_schema=False)
    async def withrottle_prefs_put(
        request: Request, app_name: str, file_name: str,
    ) -> Response:
        """Accept (and persist) Engine Driver's saved preferences."""
        profile = getattr(handle.ctx, "_roster_profile", None)
        body = await request.body()
        if profile is not None:
            target_dir = profile.preferences_dir / app_name
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / file_name).write_bytes(body)
        return Response(status_code=204)

    @app.post("/roster/create", response_class=HTMLResponse)
    async def roster_create(
        request: Request,
        id: str = Form(...),
        dccAddress: str = Form("3"),
        longAddress: int = Form(0),
        decoderFamily: str = Form(""),
        decoderModel: str = Form(""),
        roadName: str = Form(""),
        roadNumber: str = Form(""),
        mfg: str = Form(""),
        model: str = Form(""),
        owner: str = Form(""),
        maxSpeedPct: int = Form(100),
    ) -> Any:
        await _dispatch(handle, type="roster", method="put", data={
            "id": id,
            "dccAddress": dccAddress,
            "longAddress": bool(longAddress),
            "decoderFamily": decoderFamily,
            "decoderModel": decoderModel,
            "roadName": roadName,
            "roadNumber": roadNumber,
            "mfg": mfg,
            "model": model,
            "owner": owner,
            "maxSpeedPct": maxSpeedPct,
        })
        items = await _dispatch(handle, type="roster", method="list")
        return templates.TemplateResponse(
            request, "_roster_table.html",
            {"roster": items, "catalog_loaded": len(_catalog()) > 0},
        )

    @app.get("/roster/{entry_id}/row", response_class=HTMLResponse)
    async def roster_row(request: Request, entry_id: str) -> Any:
        data = await _dispatch(
            handle, type="roster", method="get", data={"id": entry_id},
        )
        return templates.TemplateResponse(
            request, "_roster_row.html",
            {"e": data, "catalog_loaded": len(_catalog()) > 0},
        )

    @app.get("/roster/{entry_id}/edit", response_class=HTMLResponse)
    async def roster_edit_row(request: Request, entry_id: str) -> Any:
        data = await _dispatch(
            handle, type="roster", method="get", data={"id": entry_id},
        )
        return templates.TemplateResponse(
            request, "_roster_row_edit.html", {"e": data},
        )

    @app.post("/roster/{entry_id}/update", response_class=HTMLResponse)
    async def roster_update(
        request: Request,
        entry_id: str,
        dccAddress: str = Form(""),
        longAddress: int = Form(0),
        decoderFamily: str = Form(""),
        decoderModel: str = Form(""),
        roadName: str = Form(""),
        roadNumber: str = Form(""),
        mfg: str = Form(""),
        model: str = Form(""),
        owner: str = Form(""),
        maxSpeedPct: int = Form(100),
    ) -> Any:
        data = await _dispatch(handle, type="roster", method="post", data={
            "id": entry_id,
            "dccAddress": dccAddress,
            "longAddress": bool(longAddress),
            "decoderFamily": decoderFamily,
            "decoderModel": decoderModel,
            "roadName": roadName,
            "roadNumber": roadNumber,
            "mfg": mfg,
            "model": model,
            "owner": owner,
            "maxSpeedPct": maxSpeedPct,
        })
        return templates.TemplateResponse(
            request, "_roster_row.html",
            {"e": data, "catalog_loaded": len(_catalog()) > 0},
        )

    @app.delete("/roster/{entry_id}", response_class=HTMLResponse)
    async def roster_delete(request: Request, entry_id: str) -> Any:
        await _dispatch(handle, type="roster", method="delete", data={"id": entry_id})
        return HTMLResponse("")

    # ----- panel viewer -----------------------------------------------------

    @app.get("/panel", response_class=HTMLResponse)
    async def panel(request: Request) -> Any:
        try:
            panels = await _dispatch(handle, type="panel", method="list")
        except HTTPException:
            panels = []
        return templates.TemplateResponse(
            request, "panel.html",
            _base_context(handle, "panel") | {
                "panel_summary": None,
                "profile_panels": panels,
            },
        )

    # ----- decoder programmer -----------------------------------------------

    from pmri_core.persistence.decoder_xml import (  # noqa: PLC0415
        DecoderVariable,
        UnsupportedVariableKind,
        VariableKind,
        find_composite_current_choice,
        variable_apply_to_state,
        variable_cv_list,
        variable_extract_from_state,
        variable_extract_from_store,
        variable_is_indexed,
        variable_primary_cv,
        variable_store_key,
        variable_wire_read_plan,
        variable_wire_write_plan,
    )
    from pmri_decoderpro import (  # noqa: PLC0415
        compose_panes,
        find_pane_by_slug,
    )

    def _programmer_context() -> dict[str, Any]:
        """The header/badge state for both the main /programmer page and
        the tabbed per-loco view.

        ``decoder_xml_default`` pre-fills the catalog-load input from
        ``JMRI_DECODER_XML_ROOT`` when set; unset -> empty string,
        matching the pre-Tier-3 behaviour where the operator typed
        the path each time.

        ``load_in_progress`` (+ ``loaded`` / ``total`` / ``directory``)
        surface when either the ``JMRI_DECODER_XML_ROOT`` autoload or
        a manual ``/programmer/catalog/load`` is running. The catalog
        fragment renders the thermometer instead of the "Catalog
        empty" hint in that state, so a user who lands on
        ``/programmer`` mid-autoload doesn't see a stale message
        prompting them to re-click Load.
        """
        from pmri_core.env import decoder_xml_root  # noqa: PLC0415
        default_root = decoder_xml_root()
        progress = getattr(handle.ctx, "_catalog_load_progress", None)
        load_in_progress = bool(
            progress and not progress.get("done") and not progress.get("error"),
        )
        ctx: dict[str, Any] = {
            "catalog_size": len(_catalog()),
            "manufacturers": _catalog().manufacturers(),
            "programmer_count": len(_catalog().programmers),
            "decoder_xml_default": str(default_root) if default_root else "",
            "load_in_progress": load_in_progress,
        }
        if load_in_progress:
            ctx["loaded"] = progress.get("loaded", 0)
            ctx["total"] = progress.get("total", 0)
            ctx["directory"] = progress.get("directory", "")
        return ctx

    @app.get("/programmer", response_class=HTMLResponse)
    async def programmer_home(request: Request) -> Any:
        try:
            avail = await _dispatch(handle, type="programmer", method="list")
        except HTTPException:
            avail = []
        return templates.TemplateResponse(
            request, "programmer.html",
            _base_context(handle, "programmer")
            | _programmer_context()
            | {"availability": avail},
        )

    # ----- per-locomotive programmer (tabbed, DecoderPro-style) -------------

    def _pick_programmer_config():
        """Return the programmer definition used for tab composition.

        Preference (matches JMRI's default): ``Comprehensive`` > any
        other loaded programmer > None.
        """
        if not _catalog().programmers:
            return None
        return (
            _catalog().programmers.get("Comprehensive")
            or next(iter(_catalog().programmers.values()), None)
        )

    def _format_split_datetime(
        stored: int | None, base: str, factor: int, unit: str, display: str,
    ) -> str:
        """Render a SPLIT_DATETIME variable's current value for the pane.

        Mirrors JMRI Java's :class:`SplitDateTimeVariableValue`:
        ``base + stored * factor`` in units of :class:`ChronoUnit`,
        formatted per *display*. ``dateOnly`` / ``timeOnly`` show just
        that half; anything else shows the full date-time.

        * Returns ``"--"`` when *stored* is None (no CV read yet).
        * Returns the raw integer when *base* is empty / unparseable --
          lets a decoder XML with an incomplete declaration still show
          something meaningful rather than crashing.
        * Supports every ChronoUnit JMRI uses in the shipped XSD set:
          SECONDS / MINUTES / HOURS / DAYS / WEEKS / MONTHS / YEARS.
          MONTHS and YEARS use a calendar-aware step (dateutil
          ``relativedelta`` when available; else month=30d, year=365d
          approximation). The others use :class:`timedelta`.
        """
        from datetime import datetime, timedelta  # noqa: PLC0415

        if stored is None:
            return "--"
        if not base:
            return str(stored)
        try:
            base_dt = datetime.fromisoformat(base)
        except ValueError:
            return str(stored)
        amount = stored * (factor or 1)
        unit_u = (unit or "").upper()
        try:
            if unit_u in ("SECONDS", "SECOND"):
                dt = base_dt + timedelta(seconds=amount)
            elif unit_u in ("MINUTES", "MINUTE"):
                dt = base_dt + timedelta(minutes=amount)
            elif unit_u in ("HOURS", "HOUR"):
                dt = base_dt + timedelta(hours=amount)
            elif unit_u in ("DAYS", "DAY"):
                dt = base_dt + timedelta(days=amount)
            elif unit_u in ("WEEKS", "WEEK"):
                dt = base_dt + timedelta(weeks=amount)
            elif unit_u in ("MONTHS", "MONTH"):
                dt = base_dt + timedelta(days=amount * 30)
            elif unit_u in ("YEARS", "YEAR"):
                dt = base_dt + timedelta(days=amount * 365)
            elif unit_u in ("MILLIS", "MILLISECONDS"):
                dt = base_dt + timedelta(milliseconds=amount)
            else:
                return str(stored)
        except (OverflowError, ValueError):
            return str(stored)
        if display == "dateOnly":
            return dt.date().isoformat()
        if display == "timeOnly":
            return dt.time().isoformat(timespec="seconds")
        return dt.isoformat(sep=" ", timespec="seconds")

    def _build_variable_row(
        var, cv_bytes: dict[str, int], current_value: int | None,
        *, definition=None,
    ) -> dict[str, Any]:
        """Turn a :class:`DecoderVariable` into the dict the pane template
        renders. Enum/composite choices are unpacked to plain dicts so
        Jinja can iterate them without knowing pydantic.

        For composite variables, we also compute:

        * ``current_choice`` -- which preset's settings match the
          current sub-variable state (None if no match).
        * ``composite_choices[i].settings`` -- the sub-variable writes
          each preset expands into. Templates carry this as
          ``data-settings`` JSON so client-side batch collection can
          fan a composite pick out into per-sub-variable BatchWrite
          entries without a server round-trip per choice.
        """
        cv_list = variable_cv_list(var)
        cv_display = ".".join(str(c) for c in cv_list) if cv_list else var.cv
        primary_cv = variable_primary_cv(var)
        # For composite: settings map to sub-variable writes.
        composite_current_choice: str | None = None
        composite_choices: list[dict[str, Any]] = []
        if var.kind is VariableKind.COMPOSITE and definition is not None:
            by_item = {v.item: v for v in definition.variables}
            composite_current_choice = find_composite_current_choice(
                var, by_item, cv_bytes,
            )
            for cc in var.composite_choices:
                composite_choices.append({
                    "choice": cc.choice,
                    "settings": [
                        {"item": s.label, "value": s.value}
                        for s in cc.settings
                    ],
                })
        else:
            composite_choices = [
                {"choice": c.choice, "settings": []}
                for c in var.composite_choices
            ]
        datetime_display: str | None = None
        if var.kind is VariableKind.SPLIT_DATETIME:
            datetime_display = _format_split_datetime(
                current_value, var.dt_base, var.dt_factor,
                var.dt_unit, var.dt_display,
            )
        return {
            "item": var.item,
            "label": var.label,
            "tooltip": var.tooltip,
            "cv": primary_cv,
            "cv_display": cv_display,
            "kind": var.kind.value,
            "read_only": var.read_only,
            "write_only": var.write_only,
            "current_value": current_value,
            "current_choice": composite_current_choice,
            "wire_max": (1 << var.bit_mask.width) - 1 if var.kind in (
                VariableKind.DECIMAL, VariableKind.HEXADECIMAL, VariableKind.ENUM,
            ) else 65535,
            "enum_choices": [
                {"value": c.value, "choice": c.choice}
                for c in var.enum_choices
            ],
            "composite_choices": composite_choices,
            "constant_value": var.constant_value,
            "datetime_display": datetime_display,
        }

    def _resolve_cv_bytes(
        variables, cv_values: dict,
    ) -> dict[str, int]:
        """Build a string-keyed ``{store_key: byte}`` snapshot for the loco.

        Layers, low -> high precedence:

        1. Per-variable defaults from the catalog: for each variable
           that declares a ``default`` (already-*masked* value), we
           apply it via :func:`variable_apply_to_state` so bits from
           multiple variables share the same CV byte correctly (e.g.
           CV 29's Address Format bit 5, Speed Steps bit 1, Power
           Source Conv bit 2 all compose into one default byte).
           Indexed variables get their default byte written under the
           compound key ``bank.index.cv``.
        2. Roster ``<values>`` from ``cv_values`` (already str-keyed)
           -- a stored byte overrides whatever the default layer
           produced.

        Result is fed to :func:`variable_extract_from_store` which
        looks up plain-int-string keys for non-indexed variables and
        compound keys for indexed ones.
        """
        result: dict[str, int] = {}
        for var in variables:
            if var.default is None:
                continue
            if var.kind in (
                VariableKind.SPEED_TABLE,
                VariableKind.COMPOSITE,
                VariableKind.SPLIT_DATETIME,
                VariableKind.CONSTANT,
            ):
                # These kinds don't participate in the mask-composition
                # model. Speed table has one CV per step (defaults are
                # per-step and handled by the speed-table row builder).
                # Composite and split-datetime aren't wire-value coded.
                # Constants don't live in the byte snapshot at all.
                continue
            # Fetch the current byte under the variable's store key
            # (needed for masked RMW). Then apply the default and put
            # the result back under the same key.
            if variable_is_indexed(var):
                # Indexed: apply per byte, keyed by compound name.
                # Multi-byte indexed splits get one key per byte.
                assert var.split is not None
                base = var.split.primary_cv
                for i in range(var.split.byte_count):
                    key = variable_store_key(var, base + i)
                    # Best-effort byte-slice of a multi-byte human default.
                    shift = 8 * (var.split.byte_count - 1 - i)
                    result[key] = (int(var.default) >> shift) & 0xFF
                continue
            try:
                # Non-indexed: use the legacy int-keyed helper against
                # an int-keyed view of ``result`` for backward-compat.
                int_view = {
                    int(k): v for k, v in result.items() if k.isdigit()
                }
                updates = variable_apply_to_state(var, var.default, int_view)
            except Exception:  # noqa: BLE001
                continue
            for cv_int, byte in updates.items():
                result[str(cv_int)] = byte
        # Overlay roster/stored values -- they win over defaults.
        for k, v in cv_values.items():
            try:
                result[str(k)] = int(v)
            except (TypeError, ValueError):
                continue
        return result

    def _build_speed_table_rows(
        var, cv_values: dict,
    ) -> list[dict[str, Any]]:
        """Expand a ``<speedTable>`` variable into one row per step.

        NMRA layout: ``entries`` consecutive CVs starting at the
        variable's primary CV (typically CV67 -> step 1, CV68 -> step 2,
        ..., CV94 -> step 28). Each row is an independent 0..255 decimal
        input; dirty tracking + batch writes treat them as separate
        writes so the queue can retry a single failed step without
        re-sending the whole curve.
        """
        base_cv = variable_primary_cv(var)
        entries = max(int(var.speed_table_entries or 28), 1)
        rows: list[dict[str, Any]] = []
        for step_idx in range(entries):
            cv = base_cv + step_idx
            current = cv_values.get(str(cv))
            try:
                current_int = int(current) if current is not None else None
            except (TypeError, ValueError):
                current_int = None
            # Each step becomes its own pseudo-variable so downstream
            # dirty tracking uses a per-step ``item`` id.
            step_label = f"Speed Step {step_idx + 1}"
            step_item = f"{var.item}[{step_idx + 1}]"
            rows.append({
                "item": step_item,
                "label": step_label,
                "tooltip": var.tooltip,
                "cv": cv,
                "cv_display": str(cv),
                "kind": "decimal",
                "read_only": var.read_only,
                "write_only": var.write_only,
                "current_value": current_int,
                "wire_max": 255,
                "enum_choices": [],
                "composite_choices": [],
                "constant_value": None,
            })
        return rows

    _FNMAP_ITEM_RE = re.compile(
        r"^(?P<fn>F[^ ]+) controls output (?P<out>\d+)$",
    )

    def _fnmap_fn_sort_key(fn: str) -> tuple[int, int, str]:
        """JMRI ordering: STOP, DRIVE, FWD, REV, FL(f), FL(r),
        F0..F28, then anything else (sensors S0.., etc.) trailing.
        """
        specials = {
            "STOP": (0, 0), "DRIVE": (0, 1),
            "FWD": (0, 2), "REV": (0, 3),
            "FL": (0, 4), "FL(f)": (0, 5), "FL(r)": (0, 6),
            "F0F": (0, 7), "F0R": (0, 8),
        }
        if fn in specials:
            band, idx = specials[fn]
            return (band, idx, "")
        m = re.match(r"^F(\d+)$", fn)
        if m:
            return (1, int(m.group(1)), "")
        m = re.match(r"^S(\d+)$", fn)
        if m:
            return (3, int(m.group(1)), "")
        return (2, 0, fn)

    def _layout_tree_has_fnmapping(tree: dict) -> bool:
        """Walk *tree* looking for any ``fnmapping`` leaf node."""
        if tree.get("type") == "fnmapping":
            return True
        for child in tree.get("children", ()):
            if _layout_tree_has_fnmapping(child):
                return True
        return False

    def _build_fnmap_matrix(
        definition, cv_bytes: dict[str, int],
        row_by_item: dict[str, dict[str, Any]],
        *, variables=None,
    ) -> dict[str, Any]:
        """Collect all ``Fx controls output y`` variables into a matrix.

        Mirrors JMRI's ``FnMapPanel`` layout:

        * Rows are functions in natural order -- ``FL(f)``, ``FL(r)``,
          ``F0``, ``F1``...``F28``, plus any special names the decoder
          uses (STOP/DRIVE/FWD/REV -- rare, but supported).
        * Columns are physical outputs. Only outputs referenced by
          at least one variable become columns; outputs a decoder
          doesn't wire are suppressed so wide decoders (15+ outputs)
          don't get pointless empty columns.
        * Each column carries a two-line header -- the numeric
          ``Output N`` and the decoder-declared output label (e.g.
          "Airhorn", "Bell", "FX3"). The label comes from
          ``<output name="N" label="..."/>`` on the model definition;
          JMRI's authoring convention is to use ``|`` as a line break
          inside the label, so we split on it for the sub-lines.

        Returns ``{"fn_labels": [...], "columns": [{"num": N,
        "label_lines": [...]}], "cells": {(fn, out): row}}``.
        """
        fn_labels: list[str] = []
        outputs: set[int] = set()
        cells: dict[tuple[str, int], dict[str, Any]] = {}
        seen_fn: set[str] = set()
        if variables is None:
            variables = definition.variables if definition else ()
        for var in variables:
            m = _FNMAP_ITEM_RE.match(var.item)
            if not m:
                continue
            fn = m.group("fn")
            try:
                out = int(m.group("out"))
            except ValueError:
                continue
            row = row_by_item.get(var.item)
            if row is None:
                try:
                    current_int = variable_extract_from_store(var, cv_bytes)
                except UnsupportedVariableKind:
                    current_int = None
                row = _build_variable_row(
                    var, cv_bytes, current_int, definition=definition,
                )
            cells[(fn, out)] = row
            outputs.add(out)
            if fn not in seen_fn:
                seen_fn.add(fn)
                fn_labels.append(fn)
        fn_labels.sort(key=_fnmap_fn_sort_key)

        # Column header labels come from the decoder's <output> elements.
        # These live either on the model (JMRI's official spec) or on
        # the family -- SoundTraxx Tsunami2 and Digitrax put them at
        # family level so the labels apply to every model. Model wins
        # when both define the same output; family fills the gaps.
        output_label_by_num: dict[int, str] = {}
        if definition is not None:
            output_sources = []
            if definition.family is not None:
                output_sources.append(definition.family.outputs)
            if definition.model is not None:
                output_sources.append(definition.model.outputs)
            for source in output_sources:
                for o in source:
                    try:
                        n = int(o.name)
                    except (TypeError, ValueError):
                        continue
                    if o.label:
                        output_label_by_num[n] = o.label
        columns: list[dict[str, Any]] = []
        for out in sorted(outputs):
            raw = output_label_by_num.get(out, "")
            # ``|`` is JMRI's authoring convention for line breaks in
            # output labels ("Airhorn| ", "Dynamic|Brakes").
            lines = [
                seg.strip() for seg in raw.split("|") if seg.strip()
            ] if raw else []
            columns.append({"num": out, "label_lines": lines})
        return {
            "fn_labels": fn_labels,
            "columns": columns,
            "cells": cells,
        }

    def _build_pane_view(
        composed_pane, definition, cv_values: dict,
    ) -> dict[str, Any]:
        """Assemble the render context for one pane's HTML fragment.

        Baseline value shown per row = roster ``<values>`` if the CV is
        set there, otherwise the decoder catalog's default. This merged
        baseline is what dirty tracking compares against; the JS on the
        page marks a row dirty when the user's input diverges from it.

        SPEED_TABLE variables (``<speedTable entries="N"/>``) expand
        into N rows via :func:`_build_speed_table_rows` -- one row per
        step CV so the batch queue can write each step independently.
        """
        if composed_pane.is_synthetic:
            return {}
        # Filter variables against the current model's productID and
        # any ``<qualifier>`` conditions on other variables.
        #
        # * productID gate: ``<variable include="pid1,pid2,...">`` /
        #   ``exclude="..."`` limits the variable to specific decoder
        #   revisions.
        # * Qualifier gate: ``<qualifier>`` triples reference another
        #   variable's current value (``<variableref>X</variableref>
        #   <relation>ge</relation><value>7</value>``). All qualifiers
        #   on a variable must pass (AND) for it to be visible.
        #
        # SoundTraxx Tsunami2 uses both heavily: HEP Mode / HEP Mode2 /
        # Turbine Start-Stop share CV 16.1.303 and take turns being
        # applicable depending on the loco model (productID) and prime
        # mover selection (``Sound Option 5``). Without the two gates
        # all three end up piled into the same grid cell.
        current_pid = (
            definition.model.product_id
            if definition is not None and definition.model is not None
            else ""
        )
        def _pid_applies(var) -> bool:
            if var.include_product_ids and current_pid not in var.include_product_ids:
                return False
            if var.exclude_product_ids and current_pid in var.exclude_product_ids:
                return False
            return True
        pid_vars = tuple(
            v for v in (definition.variables if definition else ())
            if _pid_applies(v)
        )
        # Build a preliminary by-item map from productID-applicable
        # variables and a preliminary cv_bytes so qualifiers can look
        # up their referenced variables' current values.
        pid_by_item = {v.item: v for v in pid_vars}
        pid_cv_bytes = _resolve_cv_bytes(pid_vars, cv_values)
        filtered_vars = tuple(
            v for v in pid_vars
            if _variable_qualifiers_pass(v, pid_by_item, pid_cv_bytes)
        )
        by_item = {v.item: v for v in filtered_vars}
        rows: list[dict[str, Any]] = []
        # ``<speedtable/>`` widget: JMRI's Speed Table pane uses this
        # instead of per-step <display> items. Find the decoder's
        # SPEED_TABLE variable and expand it here (before iterating
        # items_available) so the pane renders even when no explicit
        # <display item="Speed Table"/> reference exists. Track which
        # items we've expanded to avoid double-rendering if the pane
        # also lists them via <display>.
        expanded_items: set[str] = set()
        pane_decl = composed_pane.pane
        # Two triggers for speed-table expansion:
        #  (a) ``<speedtable/>`` widget detected by the parser, OR
        #  (b) the composed pane's name/slug identifies it as the Speed
        #      Table tab -- a name-based fallback so a real JMRI install
        #      that ships an unexpected widget variant still expands
        #      the decoder's SPEED_TABLE variable when the user opens
        #      the "Speed Table" tab.
        pane_is_speed_table = (
            (pane_decl is not None and pane_decl.has_speed_table)
            or composed_pane.slug == "speed-table"
            or composed_pane.name.strip().lower() == "speed table"
        )
        if pane_is_speed_table:
            found_speed_table_var = False
            for v in (definition.variables if definition else ()):
                if v.kind == VariableKind.SPEED_TABLE:
                    rows.extend(_build_speed_table_rows(v, cv_values))
                    expanded_items.add(v.item)
                    found_speed_table_var = True
            if not found_speed_table_var:
                # No SPEED_TABLE variable in the catalog -- many decoder
                # XMLs omit it, relying on the NMRA convention that CV
                # 67..CV 94 are the 28-step table (bytes 1..28). Emit a
                # synthetic 28-row expansion so the user can still write
                # each step. Use the class:`DecoderVariable` model with
                # the anchor CV and default entries so
                # :func:`_build_speed_table_rows` handles it.
                synthetic = DecoderVariable.model_validate({
                    "CV": "67",
                    "item": "Speed Table",
                    "kind": VariableKind.SPEED_TABLE,
                    "speed_table_entries": 28,
                })
                rows.extend(_build_speed_table_rows(synthetic, cv_values))
        # Build an int-keyed CV byte snapshot, with defaults filling in
        # for CVs that haven't been read/written yet. For bit-mask
        # variables (Address Format, Speed Steps, Power Source Conv --
        # all sharing CV 29), we must call ``variable_extract_from_state``
        # so the row's "current" shows the *masked* value, not the raw
        # byte. Otherwise every enum on a shared CV compares its wire
        # value (0/1/2/...) against the whole byte (e.g. 2) and dirty-
        # tracks a false positive; and switching panes shows the same
        # raw byte across all variables, looking wildly inconsistent.
        cv_bytes = _resolve_cv_bytes(filtered_vars, cv_values)
        for item_name in composed_pane.items_available:
            if item_name in expanded_items:
                continue
            var = by_item.get(item_name)
            if var is None:
                continue
            if var.kind == VariableKind.SPEED_TABLE:
                rows.extend(_build_speed_table_rows(var, cv_values))
                continue
            try:
                current_int = variable_extract_from_store(var, cv_bytes)
            except UnsupportedVariableKind:
                current_int = None
            rows.append(_build_variable_row(
                var, cv_bytes, current_int, definition=definition,
            ))
        # On the Speed Table pane we synthesize CV 67..CV 94 rows before
        # iterating items_available, so the natural row order interleaves
        # oddly (67..94 first, then whatever items_available lists like
        # CV 65 Kickstart / CV 66 Forward Trim). Sort by CV number here
        # so the pane reads top-to-bottom in CV order. Only applies to
        # the speed-table pane -- other panes rely on JMRI's explicit
        # display ordering.
        if pane_is_speed_table:
            def _cv_sort_key(row: dict[str, Any]) -> int:
                try:
                    return int(row.get("cv") or 0)
                except (TypeError, ValueError):
                    return 0
            rows.sort(key=_cv_sort_key)

        # Raw CV table (CVs pane) -- enumerate every CV the decoder
        # defines, merged with whatever's been read/written into the
        # store. Each row is displayed with its stored value (if any)
        # and gives the user a Read/Write handle on it. Matches JMRI
        # Java's CVs tab which lists everything up front, not just
        # what's been touched.
        raw_cv_rows: list[dict[str, Any]] = []
        if pane_decl is not None and pane_decl.has_cvtable:
            catalog_names: set[str] = set()
            for var in (definition.variables if definition else ()):
                if var.kind in (
                    VariableKind.CONSTANT, VariableKind.SPEED_TABLE,
                    VariableKind.COMPOSITE, VariableKind.SPLIT_DATETIME,
                ):
                    continue
                if variable_is_indexed(var):
                    assert var.split is not None
                    base = var.split.primary_cv
                    for i in range(var.split.byte_count):
                        catalog_names.add(variable_store_key(var, base + i))
                else:
                    for cv in variable_cv_list(var):
                        catalog_names.add(str(cv))
            # Also include anything in the store that isn't in the
            # catalog (e.g. legacy roster entries, raw CVs the user
            # added via the "Add CV" input in a prior session).
            all_names = catalog_names | set(cv_values.keys())
            def _raw_sort_key(name: str) -> tuple[int, int, int, str]:
                # Sort plain CVs first (by number), then indexed by
                # (bank, index, base). Falls through to string compare
                # for anything that doesn't parse.
                parts = name.split(".")
                try:
                    nums = [int(p) for p in parts]
                except ValueError:
                    return (2, 0, 0, name)
                if len(nums) == 1:
                    return (0, nums[0], 0, "")
                if len(nums) == 3:
                    return (1, nums[0], nums[1] * 1000 + nums[2], "")
                return (1, 0, 0, name)
            for name in sorted(all_names, key=_raw_sort_key):
                v = cv_values.get(name)
                try:
                    v_int = int(v) if v is not None else None
                except (TypeError, ValueError):
                    v_int = None
                raw_cv_rows.append({
                    "name": name,
                    "value": v_int,
                    "hex": f"{v_int:02X}" if v_int is not None else "",
                })
        # Rich layout tree -- the JMRI-authored column/row/label/
        # separator structure. The template renders it recursively via
        # the ``layout_node`` macro. Each ``<display>`` leaf resolves
        # to a variable row (looked up by item), which the macro emits
        # inline with its label + input + Read button. When the pane
        # doesn't have a raw XML element (synthetic tests, or older
        # panes), the fallback tree in ``pane_layout_tree`` gives a
        # flat column of the pane's ``items`` list -- same widgets as
        # the legacy table view, just wrapped in divs.
        from pmri_core.persistence.decoder_xml import pane_layout_tree  # noqa: PLC0415
        layout_tree = None
        row_by_item: dict[str, dict[str, Any]] = {}
        if composed_pane.pane is not None:
            layout_tree = pane_layout_tree(composed_pane.pane)
            row_by_item = {r["item"]: r for r in rows if r.get("item")}

        # ----- Function Map matrix -----
        # JMRI's ``<fnmapping/>`` marker is an app-generated grid of
        # checkboxes keyed by every ``Fx controls output y`` variable
        # the decoder defines. The pane XML doesn't list these
        # individually (that's the point of the marker), so we collect
        # them here from ``definition.variables`` and shape them into
        # a matrix keyed by function label + output number. When the
        # pane has no fnmapping marker we skip the whole thing.
        fnmap_matrix: dict[str, Any] | None = None
        if layout_tree is not None and _layout_tree_has_fnmapping(layout_tree):
            fnmap_matrix = _build_fnmap_matrix(
                definition, cv_bytes, row_by_item, variables=filtered_vars,
            )
        return {
            "variable_rows": rows,
            "raw_cv_rows": raw_cv_rows,
            "layout_tree": layout_tree,
            "row_by_item": row_by_item,
            "is_speed_table_pane": pane_is_speed_table,
            "fnmap_matrix": fnmap_matrix,
        }

    @app.get("/programmer/loco/{loco_id}", response_class=HTMLResponse)
    async def programmer_loco(
        request: Request, loco_id: str, tab: str | None = None,
    ) -> Any:
        """Per-loco tabbed CV programmer.

        Roster entry drives the tab set: the loco's decoder family/model
        picks a :class:`DecoderDefinition` from the catalog, then
        ``compose_panes`` unions the standard programmer panes (from
        Comprehensive.xml if loaded) with any decoder-declared extras.
        """
        try:
            loco = await _dispatch(
                handle, type="roster", method="get", data={"id": loco_id},
            )
        except HTTPException as e:
            return HTMLResponse(
                f"no such roster entry: {loco_id}", status_code=e.status_code,
            )

        family = loco.get("decoderFamily") or ""
        model = loco.get("decoderModel") or ""
        definition = (
            _catalog().find(family, model) if (family and model) else None
        )
        programmer_cfg = _pick_programmer_config()

        variables = definition.variables if definition else ()
        decoder_panes = definition.panes if definition else ()
        tab_panes = compose_panes(list(variables), programmer_cfg, list(decoder_panes))

        # Per-loco CV state. If the store sees CVs in both the JMRI-
        # canonical roster/<id>.xml <values> block AND our legacy
        # rosters/<id>.xml, ask the user which is authoritative before
        # rendering panes with potentially-wrong values.
        file_name = loco.get("fileName") or None
        from pmri_server.services.programmer import _loco_cv_store  # noqa: PLC0415
        from pmri_server.loco_cv_store import AmbiguousCvSourceError  # noqa: PLC0415
        store = _loco_cv_store(handle.ctx)
        try:
            state = store.get(loco_id, file_name)
        except AmbiguousCvSourceError as ambig:
            return templates.TemplateResponse(
                request, "programmer_cv_source_choose.html",
                _base_context(handle, "programmer")
                | _programmer_context()
                | {
                    "loco": loco,
                    "loco_id": loco_id,
                    "roster_path": str(ambig.roster_path),
                    "legacy_path": str(ambig.legacy_path),
                },
            )

        # Default active tab = Roster Entry. Query-param override lets
        # deep links land on a specific tab.
        active_slug = tab or (tab_panes[0].slug if tab_panes else "roster-entry")
        active_pane = find_pane_by_slug(tab_panes, active_slug) or (
            tab_panes[0] if tab_panes else None
        )
        if active_pane is not None:
            active_slug = active_pane.slug

        pane_ctx = _build_pane_view(active_pane, definition, state.cv_values) if active_pane else {}

        return templates.TemplateResponse(
            request, "programmer_loco.html",
            _base_context(handle, "programmer")
            | _programmer_context()
            | {
                "loco": loco,
                "loco_id": loco_id,
                "definition": definition,
                "family": family,
                "model": model,
                "cv_values": state.cv_values,
                "tab_panes": tab_panes,
                "active_slug": active_slug,
                "pane": active_pane,
                **pane_ctx,
            },
        )

    @app.post(
        "/programmer/loco/{loco_id}/resolve-source",
        response_class=HTMLResponse,
    )
    async def programmer_loco_resolve_source(
        request: Request, loco_id: str, source: str = Form(...),
    ) -> Any:
        """Persist the user's pick for which CV-state source is
        authoritative on this loco, then bounce back to the programmer
        view so the panes render with the chosen source."""
        from pmri_server.loco_cv_store import CvSource  # noqa: PLC0415
        from pmri_server.services.programmer import _loco_cv_store  # noqa: PLC0415
        try:
            picked = CvSource(source)
        except ValueError:
            raise HTTPException(400, f"unknown source: {source}") from None
        _loco_cv_store(handle.ctx).set_preferred_source(loco_id, picked)
        # 303 See Other -> re-run the GET so panes render.
        return HTMLResponse(
            "", status_code=303,
            headers={"Location": f"/programmer/loco/{loco_id}"},
        )

    @app.get(
        "/programmer/loco/{loco_id}/pane/{pane_slug}",
        response_class=HTMLResponse,
    )
    async def programmer_loco_pane(
        request: Request, loco_id: str, pane_slug: str,
    ) -> Any:
        """Return one pane's HTML fragment for HTMX tab-swap."""
        try:
            loco = await _dispatch(
                handle, type="roster", method="get", data={"id": loco_id},
            )
        except HTTPException as e:
            return HTMLResponse(str(e.detail), status_code=e.status_code)

        family = loco.get("decoderFamily") or ""
        model = loco.get("decoderModel") or ""
        definition = (
            _catalog().find(family, model) if (family and model) else None
        )
        programmer_cfg = _pick_programmer_config()

        variables = definition.variables if definition else ()
        decoder_panes = definition.panes if definition else ()
        tab_panes = compose_panes(list(variables), programmer_cfg, list(decoder_panes))
        active_pane = find_pane_by_slug(tab_panes, pane_slug)
        if active_pane is None:
            return HTMLResponse("no such pane", status_code=404)

        file_name = loco.get("fileName") or None
        from pmri_server.services.programmer import _loco_cv_store  # noqa: PLC0415
        store = _loco_cv_store(handle.ctx)
        state = store.get(loco_id, file_name)

        pane_ctx = _build_pane_view(active_pane, definition, state.cv_values)
        return templates.TemplateResponse(
            request, "_programmer_pane_content.html",
            {
                "loco": loco,
                "loco_id": loco_id,
                "family": family,
                "model": model,
                "active_slug": pane_slug,
                "pane": active_pane,
                "cv_values": state.cv_values,
                **pane_ctx,
            },
        )

    @app.post("/programmer/loco/{loco_id}/read")
    async def programmer_loco_read(
        request: Request, loco_id: str,
        payload: dict[str, Any] = Body(...),
    ) -> Any:
        """JSON-shaped single-CV read.

        When the payload includes the row's ``item`` and the catalog
        knows a variable for that name, the response also carries the
        variable's *extracted* masked value ("humanValue") -- the JS
        needs that to update the row's ``data-current`` correctly.
        Otherwise a masked-bit read (Address Format, Speed Steps, etc.)
        would put the raw CV byte into ``data-current`` and every
        subsequent dirty-check on that row would compare the byte
        against the enum's masked-value option and flip false-dirty.
        """
        cv = payload.get("cv")
        if not isinstance(cv, int):
            raise HTTPException(400, "read payload requires integer 'cv'")
        item = str(payload.get("item") or "")
        dispatch_payload: dict[str, Any] = {
            "cv": cv,
            "mode": str(payload.get("mode", "service")),
            "locoId": loco_id,
        }
        file_name = payload.get("fileName") or payload.get("file_name")
        if file_name:
            dispatch_payload["fileName"] = file_name
        address = payload.get("address")
        if address is not None:
            dispatch_payload["address"] = int(address)
            dispatch_payload["isLongAddress"] = bool(payload.get("isLongAddress"))
        try:
            data = await _dispatch(
                handle, type="programmer", method="get", data=dispatch_payload,
            )
        except HTTPException as e:
            raise HTTPException(e.status_code, e.detail) from None
        wire_value = int(data.get("value", 0))
        human_value: int | None = None
        var = _find_variable_for_write(loco_id, item) if item else None
        if var is not None:
            # Snapshot the post-write CV bytes (the read already pushed
            # this CV into the store) and extract per-variable so the
            # client can set ``data-current`` to the masked value.
            from pmri_server.services.programmer import _loco_cv_store  # noqa: PLC0415
            store = _loco_cv_store(handle.ctx)
            state = store.get(loco_id, file_name)
            # String-keyed snapshot supports indexed CVs (``16.1.275``)
            # coexisting with plain CVs (``275``) -- the extract helper
            # picks the right key per variable.
            cv_bytes = {str(k): int(v) for k, v in state.cv_values.items()}
            try:
                human_value = variable_extract_from_store(var, cv_bytes)
            except UnsupportedVariableKind:
                human_value = None
        return {
            "cv": cv,
            "value": wire_value,
            "humanValue": human_value if human_value is not None else wire_value,
            "mode": str(data.get("mode", "service_direct")),
        }

    @app.post("/programmer/loco/{loco_id}/write", response_class=HTMLResponse)
    async def programmer_loco_write(
        request: Request, loco_id: str,
        cv: int = Form(...), value: int = Form(...),
        mode: str = Form("service"), address: int | None = Form(None),
        file_name: str = Form(""),
    ) -> Any:
        payload: dict[str, Any] = {
            "cv": cv, "value": value, "mode": mode, "locoId": loco_id,
        }
        if file_name:
            payload["fileName"] = file_name
        if address is not None:
            payload["address"] = address
        try:
            data = await _dispatch(handle, type="programmer", method="post", data=payload)
        except HTTPException as e:
            return templates.TemplateResponse(
                request, "_programmer_cv_result.html",
                {"cv": cv, "error": e.detail}, status_code=e.status_code,
            )
        return templates.TemplateResponse(
            request, "_programmer_cv_result.html",
            {"cv": cv, "value": data["value"], "mode": data["mode"], "loco_id": loco_id},
        )

    # ----- raw CV access on the CVs tab ---------------------------------

    from pmri_core.persistence.decoder_xml import SplitCv  # noqa: PLC0415

    def _parse_raw_cv_name(name: str) -> tuple[int, SplitCv | None]:
        """Split a raw-CV table entry into ``(wire_cv, split_or_None)``.

        ``"275"`` -> (275, None) -- plain CV, no indexed setup.
        ``"16.1.275"`` -> (275, SplitCv(bank=16, index=1, primary_cv=275))
          -- compound CV, caller must prepend CV31/CV32 setup writes.

        Raises :class:`ValueError` on unparseable input so the caller
        can 400 out with a clear message.
        """
        name = name.strip()
        if not name:
            raise ValueError("CV name must not be empty")
        if "." in name:
            split = SplitCv.parse(name)
            if split.primary_cv <= 0 or split.bank is None:
                raise ValueError(f"malformed indexed CV name: {name!r}")
            return split.primary_cv, split
        try:
            return int(name), None
        except ValueError as e:
            raise ValueError(f"CV name must be int or bank.index.base, got {name!r}") from e

    @app.post("/programmer/loco/{loco_id}/cv/read")
    async def programmer_loco_raw_cv_read(
        loco_id: str, payload: dict[str, Any] = Body(...),
    ) -> Any:
        """Read one CV on the CVs tab. Accepts either plain (``275``)
        or compound (``16.1.275``) names.

        For compound CVs, dispatches CV31 = bank and CV32 = index setup
        writes before the read, and stashes the result in the roster
        under the compound key so subsequent renders show it.
        """
        cv_name = str(payload.get("cvName") or "")
        try:
            wire_cv, split = _parse_raw_cv_name(cv_name)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        mode = str(payload.get("mode", "service"))
        address = payload.get("address")
        file_name = payload.get("fileName")

        common: dict[str, Any] = {"mode": mode}
        if address is not None:
            common["address"] = int(address)
            common["isLongAddress"] = bool(payload.get("isLongAddress"))
        if file_name:
            common["fileName"] = file_name

        # Compound: prepend CV31/CV32 setup as service writes (no store).
        if split is not None:
            for setup_cv, setup_byte in ((31, split.bank), (32, split.index or 0)):
                await _dispatch(
                    handle, type="programmer", method="post",
                    data={**common, "cv": setup_cv, "value": int(setup_byte or 0)},
                )
        # Read the base CV; skip service-side persistence, we'll set the
        # store manually with the correct key below.
        try:
            data = await _dispatch(
                handle, type="programmer", method="get",
                data={**common, "cv": wire_cv},
            )
        except HTTPException as e:
            raise HTTPException(e.status_code, e.detail) from None
        value = int(data.get("value", 0))
        from pmri_server.services.programmer import _loco_cv_store  # noqa: PLC0415
        _loco_cv_store(handle.ctx).record_read_result(
            loco_id, cv_name, value,
            file_name if isinstance(file_name, str) else None,
        )
        return {"cvName": cv_name, "value": value}

    @app.post("/programmer/loco/{loco_id}/cv/write")
    async def programmer_loco_raw_cv_write(
        loco_id: str, payload: dict[str, Any] = Body(...),
    ) -> Any:
        """Write one CV on the CVs tab. Plain or compound; same shape
        as the read endpoint but with a ``value`` byte.

        Compound writes prepend CV31/CV32 setup writes and persist the
        payload under the compound key (matching JMRI Java's roster
        format ``<CVvalue name="16.1.275" value="X"/>``)."""
        cv_name = str(payload.get("cvName") or "")
        try:
            wire_cv, split = _parse_raw_cv_name(cv_name)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        try:
            value = int(payload["value"])
        except (KeyError, TypeError, ValueError):
            raise HTTPException(400, "value must be an int 0..255") from None
        if not 0 <= value <= 255:
            raise HTTPException(400, "value out of range 0..255")

        mode = str(payload.get("mode", "service"))
        address = payload.get("address")
        file_name = payload.get("fileName")
        common: dict[str, Any] = {"mode": mode}
        if address is not None:
            common["address"] = int(address)
            common["isLongAddress"] = bool(payload.get("isLongAddress"))
        if file_name:
            common["fileName"] = file_name

        if split is not None:
            for setup_cv, setup_byte in ((31, split.bank), (32, split.index or 0)):
                await _dispatch(
                    handle, type="programmer", method="post",
                    data={**common, "cv": setup_cv, "value": int(setup_byte or 0)},
                )
        # Base-CV write. Omit ``locoId`` so the service's built-in store
        # update doesn't fire under the plain int key -- we do it below
        # with the compound (or plain) name.
        try:
            data = await _dispatch(
                handle, type="programmer", method="post",
                data={**common, "cv": wire_cv, "value": value},
            )
        except HTTPException as e:
            raise HTTPException(e.status_code, e.detail) from None
        from pmri_server.services.programmer import _loco_cv_store  # noqa: PLC0415
        _loco_cv_store(handle.ctx).set_cv(
            loco_id, cv_name, value,
            file_name if isinstance(file_name, str) else None,
        )
        return {"cvName": cv_name, "value": int(data.get("value", value))}

    # ----- programmer step 4: dirty-tracked batch writes ----------------

    from pmri_server.programming_queue import (  # noqa: PLC0415
        BatchInFlightError,
        BatchWrite,
        get_or_create_queue,
    )

    def _make_write_callback(loco_id: str) -> Any:
        """Build the callback the queue calls per CV.

        Two responsibilities on top of just dispatching to
        ``programmer.post``:

        * **Read-modify-write for masked variables.** The client sends
          the variable's *human* value (e.g. Address Format=1 for long).
          The wire needs the full CV byte with the other variables'
          bits preserved. We look up the variable by ``item`` name in
          the decoder catalog, compute the full byte via
          :func:`variable_apply_to_state` against the current CV
          snapshot, and issue writes for the resulting CV bytes.
        * **Split-CV / long-address writes.** ``variable_apply_to_state``
          returns one entry per touched CV; long-address writes two
          bytes (CV 17 + CV 18) -- we dispatch each so the wire sees a
          real per-CV WRITE_DIR_CV_CMD.

        A write that can't be resolved to a catalog variable (no
        catalog loaded, item name missing) falls back to writing the
        raw value to the raw CV -- matches the pre-fix behaviour so
        single-CV panes / raw-CV writes still work.
        """
        async def _dispatch_one(
            cv: int, value: int, extra: dict[str, Any],
        ) -> int:
            payload: dict[str, Any] = {
                "cv": cv, "value": value,
                "mode": extra.get("mode", "service"),
                "locoId": loco_id,
            }
            file_name = extra.get("file_name")
            if file_name:
                payload["fileName"] = file_name
            addr = extra.get("address")
            if addr is not None:
                payload["address"] = addr
                payload["isLongAddress"] = bool(extra.get("long_address"))
            data = await _dispatch(
                handle, type="programmer", method="post", data=payload,
            )
            return int(data.get("value", value))

        async def _dispatch_wire_only(cv: int, value: int, extra: dict[str, Any]) -> int:
            """Write to the wire without persisting to the CV store.

            Used for indexed-CV setup writes (CV31=bank, CV32=index).
            Those bytes are physically written but must NOT show up in
            the roster's per-CV tracking -- they're transient page-
            selector state, not user data. Omitting ``locoId`` from
            the payload tells the programmer service to skip the store
            update.
            """
            payload: dict[str, Any] = {
                "cv": cv, "value": value,
                "mode": extra.get("mode", "service"),
            }
            addr = extra.get("address")
            if addr is not None:
                payload["address"] = addr
                payload["isLongAddress"] = bool(extra.get("long_address"))
            data = await _dispatch(
                handle, type="programmer", method="post", data=payload,
            )
            return int(data.get("value", value))

        async def _dispatch_with_key(
            cv: int, value: int, store_key: str, extra: dict[str, Any],
        ) -> int:
            """Write to wire AND persist under ``store_key`` (used for
            indexed CVs whose store name is ``bank.index.cv``, not
            plain ``str(cv)``). We deliberately bypass the programmer
            service's built-in store update and do it here so we can
            control the key."""
            wire = await _dispatch_wire_only(cv, value, extra)
            from pmri_server.services.programmer import _loco_cv_store  # noqa: PLC0415
            _loco_cv_store(handle.ctx).set_cv(
                loco_id, store_key, value, extra.get("file_name"),
            )
            return wire

        async def _write(cv: int, value: int, item: str, extra: dict[str, Any]) -> int:
            var = _find_variable_for_write(loco_id, item)
            if var is None:
                # Composite fan-out sends ``cv=0`` as a sentinel meaning
                # "resolve the wire CV from ``item``". If we couldn't
                # resolve the item, that's a broken composite reference
                # or catalog mismatch -- bail out with a clear error so
                # the batch reports the failure rather than silently
                # writing to CV 0.
                if cv == 0 and item:
                    raise ValueError(
                        f"composite sub-variable {item!r} not found in catalog",
                    )
                # No catalog metadata -> treat as a raw single-CV write.
                return await _dispatch_one(cv, value, extra)
            # Snapshot current CV bytes from the store so read-modify-write
            # preserves bits owned by other variables on the same CV.
            # Keys are strings so indexed CVs (``16.1.275``) can coexist
            # with plain ones (``275``) without collision.
            from pmri_server.services.programmer import _loco_cv_store  # noqa: PLC0415
            store = _loco_cv_store(handle.ctx)
            state = store.get(loco_id, extra.get("file_name"))
            cv_bytes = {str(k): int(v) for k, v in state.cv_values.items()}
            try:
                plan = variable_wire_write_plan(var, value, cv_bytes)
            except Exception:  # noqa: BLE001
                # Unsupported kind (speed table, composite, ...) --
                # fall back to a raw write on the primary CV.
                return await _dispatch_one(cv, value, extra)
            if not plan:
                return int(value)   # read-only / constant -> no-op
            # Issue physical writes in order. Setup ops (CV31/CV32)
            # go through _dispatch_wire_only so they don't pollute
            # the roster; value ops go through _dispatch_with_key so
            # they land under the correct compound key for indexed
            # variables.
            last_committed = value
            for op in plan:
                if op.store_key is None:
                    last_committed = await _dispatch_wire_only(
                        op.wire_cv, op.byte_value, extra,
                    )
                else:
                    last_committed = await _dispatch_with_key(
                        op.wire_cv, op.byte_value, op.store_key, extra,
                    )
            return last_committed
        return _write

    def _make_read_callback(loco_id: str) -> Any:
        """Build the read callback the queue calls per CV.

        Delegates to the JSON ``programmer.get`` service -- same wire as
        the single-CV Read button. The service stashes each read into
        the loco CV store, so after the batch completes the client can
        refresh the pane and see freshly-extracted values.
        """
        async def _dispatch_read(
            cv: int, extra: dict[str, Any], *, store_key: str | None,
        ) -> int:
            """Wire read + optional store update under *store_key*.

            When *store_key* is None (indexed CV31/CV32 setup writes
            aren't reads -- this shouldn't be called), or when the read
            is for a simple CV that keys plainly as ``str(cv)``, we
            just include ``locoId`` in the payload and let the
            programmer service persist via its own ``record_read_result``
            path. When the caller wants a compound store key, we omit
            ``locoId`` (service skips persistence) and set the value
            manually with the right key.
            """
            payload: dict[str, Any] = {
                "cv": cv,
                "mode": extra.get("mode", "service"),
            }
            file_name = extra.get("file_name")
            if file_name:
                payload["fileName"] = file_name
            addr = extra.get("address")
            if addr is not None:
                payload["address"] = addr
                payload["isLongAddress"] = bool(extra.get("long_address"))
            # For plain reads we can let the service persist directly.
            if store_key is None or store_key == str(cv):
                payload["locoId"] = loco_id
                data = await _dispatch(
                    handle, type="programmer", method="get", data=payload,
                )
                return int(data.get("value", 0))
            # Compound key path (indexed CV): dispatch without locoId,
            # then manually record under the compound key.
            data = await _dispatch(
                handle, type="programmer", method="get", data=payload,
            )
            value = int(data.get("value", 0))
            from pmri_server.services.programmer import _loco_cv_store  # noqa: PLC0415
            _loco_cv_store(handle.ctx).record_read_result(
                loco_id, store_key, value, file_name,
            )
            return value

        async def _read(cv: int, item: str, extra: dict[str, Any]) -> int:
            var = _find_variable_for_write(loco_id, item)
            if var is None or not variable_is_indexed(var):
                # Plain read -- service persists under ``str(cv)``.
                return await _dispatch_read(cv, extra, store_key=str(cv))
            # Indexed read: CV31/CV32 setup (writes, no persistence),
            # then read the base CV and persist under compound key.
            assert var.split is not None
            # For a read op, we need to issue writes to CV31/CV32 too.
            # Those aren't in the "read wire plan" we can call
            # directly -- write the setup manually using the
            # write-side dispatch helper. Fetch it via a lexical
            # closure -- we're inside the same _make_read_callback
            # scope but the write helpers were defined in
            # _make_write_callback; reach through the queue by
            # calling the JSON write path directly here to avoid
            # crossing closures.
            async def _wire_write(target_cv: int, target_byte: int) -> None:
                setup_payload: dict[str, Any] = {
                    "cv": target_cv, "value": target_byte,
                    "mode": extra.get("mode", "service"),
                }
                addr = extra.get("address")
                if addr is not None:
                    setup_payload["address"] = addr
                    setup_payload["isLongAddress"] = bool(
                        extra.get("long_address"),
                    )
                await _dispatch(
                    handle, type="programmer", method="post", data=setup_payload,
                )

            await _wire_write(31, var.split.bank or 0)
            await _wire_write(32, var.split.index or 0)
            store_key = variable_store_key(var, cv)
            return await _dispatch_read(cv, extra, store_key=store_key)
        return _read

    def _find_variable_for_write(loco_id: str, item: str) -> Any:
        """Look up the decoder variable named *item* for *loco_id*.

        Returns ``None`` when the catalog isn't loaded, the loco has no
        decoder family/model, or the item name doesn't match anything
        in the definition. Callers should fall back to a raw CV write
        in that case.
        """
        if not item:
            return None
        try:
            from pmri_server.services.roster import (  # noqa: PLC0415
                _roster_service_get_sync,
            )
            entry = _roster_service_get_sync(handle.ctx, loco_id)
        except Exception:  # noqa: BLE001
            return None
        if not entry:
            return None
        family = entry.get("decoderFamily") or ""
        model = entry.get("decoderModel") or ""
        if not family or not model:
            return None
        defn = _catalog().find(family, model)
        if defn is None:
            return None
        for v in defn.variables:
            if v.item == item:
                return v
        return None

    # CV 29's Address Format bit -- set means "use long address", clear
    # means "use short". Any variable whose mask overlaps this bit will
    # flip the decoder's address mode when written.
    _CV29_ADDRESS_FORMAT_BIT = 0x20

    def _ops_mode_address_change_reason(
        write: "BatchWrite", loco_id: str,
    ) -> str | None:
        """Return a human-readable reason if *write* would change the
        decoder's operating address, or ``None`` if it's safe in ops mode.

        Ops-mode POM writes reach the decoder via a DCC packet targeted
        at the current DCC address. When the write itself changes that
        address (long-address CVs 17/18, short-address CV1, or the CV29
        Address Format bit), the wire packet is fine, but *subsequent*
        writes in the same batch aim at the old address and go
        unanswered. Long-address specifically requires two atomic CV
        writes (CV17 + CV18); doing them via POM leaves the decoder in
        an inconsistent state between the two.

        We refuse rather than warn -- the safe alternative is to run the
        exact same edit on the programming track (service mode), which
        the user can do by flipping the picker at the top of the pane.
        """
        var = _find_variable_for_write(loco_id, write.item)
        if var is not None:
            if var.kind is VariableKind.LONG_ADDRESS:
                return (
                    "Long Address writes touch CV17 and CV18 atomically; "
                    "ops mode can't do that safely because the address "
                    "changes between the two packets. Use service mode."
                )
            if var.kind is VariableKind.SHORT_ADDRESS:
                return (
                    "Short Address writes change the decoder's address; "
                    "subsequent ops-mode packets would target the old "
                    "address. Use service mode."
                )
            # Any masked variable on CV29 whose ``V`` region overlaps
            # bit 5 flips the address-format mode. That'd break follow-
            # up POM packets in the same batch.
            primary = variable_primary_cv(var)
            if (
                primary == 29
                and var.bit_mask.mask_value & _CV29_ADDRESS_FORMAT_BIT
            ):
                return (
                    "Changing CV29's Address Format bit switches the "
                    "decoder between short and long addressing; "
                    "subsequent ops-mode packets would target the old "
                    "address. Use service mode."
                )
        return None

    @app.post("/programmer/loco/{loco_id}/write-batch")
    async def programmer_loco_write_batch(
        request: Request, loco_id: str,
        payload: dict[str, Any] = Body(...),
    ) -> Any:
        """Kick off a batch of CV writes for *loco_id*.

        Payload shape::

            {
              "writes": [{"cv": 1, "value": 3, "item": "Short Address"}, ...],
              "mode": "service" | "ops",
              "address": <int>,          // ops mode only
              "isLongAddress": true|false,
              "fileName": "1.xml"        // optional; JMRI roster fileName
            }

        Returns 202 + the batch id on success, or 409 if a batch is
        already in flight on this loco. Refuses with 400 when the
        batch is ops-mode and any write would change the decoder's
        operating address mid-batch (long address, short address, CV29
        Address Format flip).
        """
        raw_writes = payload.get("writes") or []
        if not isinstance(raw_writes, list) or not raw_writes:
            raise HTTPException(400, "writes must be a non-empty list")
        writes: list[BatchWrite] = []
        for w in raw_writes:
            try:
                writes.append(BatchWrite(
                    cv=int(w["cv"]),
                    value=int(w["value"]),
                    item=str(w.get("item", "")),
                ))
            except (KeyError, TypeError, ValueError) as e:
                raise HTTPException(400, f"malformed write row: {e}") from None

        mode = str(payload.get("mode", "service"))
        address = payload.get("address")
        long_address = bool(payload.get("isLongAddress", False))
        file_name = payload.get("fileName") or None

        # Ops-mode safety gate: refuse any write that would change the
        # loco's operating address mid-batch. Check up front so we
        # don't start a batch that'd fail halfway through (CV17
        # written, CV18 aimed at wrong address).
        if mode == "ops":
            for w in writes:
                reason = _ops_mode_address_change_reason(w, loco_id)
                if reason is not None:
                    raise HTTPException(
                        400,
                        f"cannot write {w.item or f'CV {w.cv}'} in ops mode: "
                        f"{reason}",
                    )

        queue = get_or_create_queue(
            handle.ctx, loco_id, _make_write_callback(loco_id),
            read_callback=_make_read_callback(loco_id),
        )
        try:
            batch = await queue.start(
                writes,
                mode=mode,
                address=int(address) if address is not None else None,
                long_address=long_address,
                file_name=file_name,
            )
        except BatchInFlightError as e:
            raise HTTPException(409, str(e)) from None
        from fastapi.responses import JSONResponse  # noqa: PLC0415
        return JSONResponse(
            {"batch_id": batch.batch_id}, status_code=202,
        )

    @app.post("/programmer/loco/{loco_id}/read-batch")
    async def programmer_loco_read_batch(
        request: Request, loco_id: str,
        payload: dict[str, Any] = Body(...),
    ) -> Any:
        """Kick off a batch of CV reads for *loco_id*.

        Payload shape::

            {
              "reads": [{"cv": 1, "item": "Short Address"}, ...],
              "mode": "service" | "ops",
              "address": <int>,          // ops mode only (rarely
                                         // useful; PowerPro can't read
                                         // ops-mode anyway)
              "fileName": "1.xml"
            }

        Returns 202 + the batch id, or 409 if a batch is already in
        flight. Duplicate CV numbers in the payload are deduplicated
        here -- reading the same CV twice in one batch is wasted time on
        a service-mode track (each read is 2-6 s), and the store already
        caches whichever value came back last.
        """
        raw_reads = payload.get("reads") or []
        if not isinstance(raw_reads, list) or not raw_reads:
            raise HTTPException(400, "reads must be a non-empty list")
        # Dedupe by CV, first-wins on the display label so the user
        # sees a sensible item name in the progress fragment.
        seen_cvs: set[int] = set()
        ops: list[BatchWrite] = []
        for r in raw_reads:
            try:
                cv = int(r["cv"])
            except (KeyError, TypeError, ValueError) as e:
                raise HTTPException(400, f"malformed read row: {e}") from None
            if cv in seen_cvs:
                continue
            seen_cvs.add(cv)
            ops.append(BatchWrite(
                cv=cv, kind="read", item=str(r.get("item", "")),
            ))

        mode = str(payload.get("mode", "service"))
        address = payload.get("address")
        long_address = bool(payload.get("isLongAddress", False))
        file_name = payload.get("fileName") or None

        queue = get_or_create_queue(
            handle.ctx, loco_id, _make_write_callback(loco_id),
            read_callback=_make_read_callback(loco_id),
        )
        try:
            batch = await queue.start(
                ops,
                mode=mode,
                address=int(address) if address is not None else None,
                long_address=long_address,
                file_name=file_name,
            )
        except BatchInFlightError as e:
            raise HTTPException(409, str(e)) from None
        from fastapi.responses import JSONResponse  # noqa: PLC0415
        return JSONResponse(
            {"batch_id": batch.batch_id}, status_code=202,
        )

    @app.get("/programmer/loco/{loco_id}/batch/status")
    async def programmer_loco_batch_status(loco_id: str) -> Any:
        """Poll the current batch's status for *loco_id*.

        Returns the batch's serializable snapshot or 404 if there's no
        active or recently-completed batch on this loco.
        """
        queues = getattr(handle.ctx, "_programming_queues", {})
        queue = queues.get(loco_id) if isinstance(queues, dict) else None
        if queue is None or queue.current is None:
            raise HTTPException(404, "no active batch")
        return queue.current.to_status()

    @app.post("/programmer/loco/{loco_id}/batch/cancel")
    async def programmer_loco_batch_cancel(loco_id: str) -> Any:
        """Request cancel on the current batch. The in-flight CV write
        finishes; subsequent queued writes are marked cancelled."""
        queues = getattr(handle.ctx, "_programming_queues", {})
        queue = queues.get(loco_id) if isinstance(queues, dict) else None
        if queue is None or queue.current is None:
            raise HTTPException(404, "no active batch")
        batch = queue.cancel()
        return batch.to_status() if batch else {"state": "no-op"}

    @app.post("/programmer/loco/{loco_id}/batch/clear")
    async def programmer_loco_batch_clear(loco_id: str) -> Any:
        """Drop a terminal-state batch so the UI can start fresh."""
        queues = getattr(handle.ctx, "_programming_queues", {})
        queue = queues.get(loco_id) if isinstance(queues, dict) else None
        if queue is None:
            return {"cleared": False}
        try:
            queue.clear()
        except BatchInFlightError as e:
            raise HTTPException(409, str(e)) from None
        return {"cleared": True}

    @app.post("/programmer/catalog/load", response_class=HTMLResponse)
    async def programmer_load_catalog(request: Request, directory: str = Form(...)) -> Any:
        """Kick off a catalog load in the background and return a
        polling widget. Actual load happens on a worker thread because
        the JMRI decoder tree has hundreds of files and parsing takes
        30-60 s on a Pi. See :func:`programmer_catalog_progress` for
        the polling endpoint the returned widget hits."""
        import asyncio  # noqa: PLC0415
        import pathlib as _pathlib  # noqa: PLC0415

        target = _pathlib.Path(directory).expanduser()
        if not target.exists():
            return templates.TemplateResponse(
                request, "_programmer_catalog.html",
                {"error": f"directory does not exist: {directory}"},
                status_code=400,
            )
        if not target.is_dir():
            return templates.TemplateResponse(
                request, "_programmer_catalog.html",
                {"error": f"not a directory: {directory}"},
                status_code=400,
            )

        # Shared state the loader worker writes and the poll route reads.
        # A single load is in flight at a time; overlapping loads are
        # rejected to keep progress state coherent.
        progress = getattr(handle.ctx, "_catalog_load_progress", None)
        if progress and not progress.get("done") and not progress.get("error"):
            return templates.TemplateResponse(
                request, "_programmer_catalog.html",
                {"error": "a catalog load is already in progress"},
                status_code=409,
            )

        # File-count precomputed so the widget can render a percentage.
        files = list(target.rglob("*.xml"))
        total = len(files)
        handle.ctx._catalog_load_progress = {   # type: ignore[attr-defined]
            "loaded": 0,
            "total": total,
            "done": False,
            "error": None,
            "directory": str(target),
        }

        def _worker() -> None:
            state = handle.ctx._catalog_load_progress  # type: ignore[attr-defined]
            try:
                # Fast metadata scan -- extracts only <family> and
                # <model> attrs per file. Variables/panes are parsed
                # lazily by DecoderCatalog.find() on first loco open.
                # See pmri_core.persistence.decoder_xml.iter_decoder_index.
                from pmri_core.persistence.decoder_xml import (  # noqa: PLC0415
                    iter_decoder_index,
                )
                catalog = DecoderCatalog()
                seen_files: set[pathlib.Path] = set()
                for entry in iter_decoder_index(target):
                    catalog.add_index_entry(entry)
                    # Progress ticks per unique source file so the
                    # thermometer maps to file count, not entry count
                    # (one file can emit many entries).
                    if entry.source_file not in seen_files:
                        seen_files.add(entry.source_file)
                        state["loaded"] += 1
                # Sync progress up to the total in case files were
                # skipped (non-decoder XML). The pane fragment reads
                # ``loaded/total`` to compute the percentage.
                state["loaded"] = state["total"]
                # Auto-load sibling programmers/ (same layout as
                # DecoderCatalog.from_directory).
                programmers_dir = target.parent / "programmers"
                if programmers_dir.is_dir():
                    from pmri_core.persistence.programmer_xml import (  # noqa: PLC0415
                        iter_programmer_configs,
                    )
                    catalog.programmers = {
                        pc.name: pc
                        for pc in iter_programmer_configs(programmers_dir)
                    }
                # NMRA manufacturer table sits at the ``xml/`` root as
                # ``decoderIndex.xml``. Load it so ``identify_by_cv``
                # has the authoritative 179-entry mfgID -> name mapping
                # instead of the small hardcoded stopgap.
                index_path = target.parent / "decoderIndex.xml"
                if index_path.is_file():
                    from pmri_decoderpro.catalog import _load_mfg_ids  # noqa: PLC0415
                    catalog.mfg_ids = _load_mfg_ids(index_path)
                handle.ctx._decoder_catalog = catalog  # type: ignore[attr-defined]
            except Exception as e:  # noqa: BLE001
                state["error"] = str(e)
            state["done"] = True

        # asyncio.to_thread runs the sync loader on the default thread
        # pool. Fire-and-forget; the poll endpoint watches ``progress``.
        asyncio.get_event_loop().create_task(asyncio.to_thread(_worker))

        return templates.TemplateResponse(
            request, "_programmer_catalog_progress.html",
            {"total": total, "directory": str(target)},
        )

    @app.get("/programmer/catalog/progress", response_class=HTMLResponse)
    async def programmer_catalog_progress(request: Request) -> Any:
        """HTMX polls this every 500 ms while a load is in flight.

        Returns:
        * a progress fragment (percentage bar) while ``done`` is False;
        * the final "Catalog loaded" fragment when ``done`` is True;
        * an error fragment when the worker set ``error``.
        """
        progress = getattr(handle.ctx, "_catalog_load_progress", None)
        if not progress:
            # No load in flight -- return the current catalog badge so
            # the poll loop stops (HTMX unbinds when the response no
            # longer includes the progress element).
            catalog = _catalog()
            return templates.TemplateResponse(
                request, "_programmer_catalog.html",
                {
                    "catalog_size": len(catalog),
                    "manufacturers": catalog.manufacturers(),
                    "programmer_count": len(catalog.programmers),
                },
            )
        if progress.get("error"):
            return templates.TemplateResponse(
                request, "_programmer_catalog.html",
                {"error": f"failed to load: {progress['error']}"},
                status_code=400,
            )
        if progress.get("done"):
            catalog = _catalog()
            return templates.TemplateResponse(
                request, "_programmer_catalog.html",
                {
                    "catalog_size": len(catalog),
                    "manufacturers": catalog.manufacturers(),
                    "programmer_count": len(catalog.programmers),
                },
            )
        return templates.TemplateResponse(
            request, "_programmer_catalog_progress.html",
            {
                "total": progress["total"],
                "loaded": progress["loaded"],
                "directory": progress["directory"],
            },
        )

    @app.get("/programmer/decoders", response_class=HTMLResponse)
    async def programmer_decoders(
        request: Request, mfg: str | None = None, family: str | None = None,
    ) -> Any:
        if family:
            models = _catalog().models(family)
            return templates.TemplateResponse(
                request, "_programmer_models.html",
                {"family": family, "models": models},
            )
        if mfg:
            families = _catalog().families(mfg)
            return templates.TemplateResponse(
                request, "_programmer_families.html",
                {"mfg": mfg, "families": families},
            )
        return HTMLResponse("")

    @app.get("/programmer/variables", response_class=HTMLResponse)
    async def programmer_variables(
        request: Request, family: str, model: str,
    ) -> Any:
        defn = _catalog().find(family, model)
        if defn is None:
            return HTMLResponse("decoder not found", status_code=404)
        return templates.TemplateResponse(
            request, "_programmer_variables.html",
            {"definition": defn, "family": family, "model": model},
        )

    @app.post("/programmer/identify", response_class=HTMLResponse)
    async def programmer_identify(request: Request) -> Any:
        """Read CV7 + CV8 via the service-mode programmer, then look up
        matching definitions in the loaded catalog.

        Failure modes surfaced clearly rather than as a spinning
        request:

        * Read errored / timed out -- the underlying error message goes
          into the fragment.
        * Read succeeded but CV8 isn't a known NMRA mfg ID (most often
          because the decoder didn't ack the read, so the CS returned
          its NoAck sentinel byte) -- user sees "no decoder / unknown
          manufacturer" instead of the catalog quietly iterating
          thousands of files.
        """
        from pmri_decoderpro import UnknownDecoderIdentityError  # noqa: PLC0415
        # NCE PowerPro service-mode reads are ~2-6 s each and
        # intermittently return the NoAck sentinel (0/48/255).
        # Auto-retry once on those to avoid making the user click
        # Identify twice for a transient wire hiccup. Any hard
        # HTTPException short-circuits to the error card as before.
        _WIRE_FAIL = {0, 48, 255}
        async def _read_cv_with_retry(cv: int) -> dict:
            first = await _dispatch(
                handle, type="programmer", method="get",
                data={"cv": cv, "mode": "service"},
            )
            if first.get("value") in _WIRE_FAIL:
                # Sentinel value -> very likely a transient read failure.
                # Do one silent retry; if it succeeds, use that.
                try:
                    second = await _dispatch(
                        handle, type="programmer", method="get",
                        data={"cv": cv, "mode": "service"},
                    )
                except HTTPException:
                    return first
                if second.get("value") not in _WIRE_FAIL:
                    return second
            return first
        try:
            cv7 = await _read_cv_with_retry(7)
            cv8 = await _read_cv_with_retry(8)
        except HTTPException as e:
            return templates.TemplateResponse(
                request, "_programmer_identify_result.html",
                {"error": e.detail}, status_code=e.status_code,
            )
        try:
            candidates = _catalog().identify_by_cv(cv7["value"], cv8["value"])
        except UnknownDecoderIdentityError as e:
            return templates.TemplateResponse(
                request, "_programmer_identify_result.html",
                {"cv7": cv7["value"], "cv8": cv8["value"], "error": str(e)},
            )
        # Manufacturer-specific productID probe. JMRI's real identify
        # is per-mfg: SoundTraxx encodes productID across CVs 253/255/
        # 256, other makers use other CVs. Follow the same protocol so
        # a Tsunami2 read collapses from ~54 families to a single
        # model (e.g. "TSU-1100 Baldwin Diesels", productID 2071).
        product_id: str | None = None
        product_id_error: str | None = None
        if candidates and "soundtraxx" in candidates[0].mfg.lower():
            try:
                cv253 = await _dispatch(
                    handle, type="programmer", method="get",
                    data={"cv": 253, "mode": "service"},
                )
                cv255 = await _dispatch(
                    handle, type="programmer", method="get",
                    data={"cv": 255, "mode": "service"},
                )
                cv256 = await _dispatch(
                    handle, type="programmer", method="get",
                    data={"cv": 256, "mode": "service"},
                )
                # JMRI Java's IdentifyDecoder formula:
                #   productID = CV256 | ((CV255 & 7) << 8) | (CV253 << 11)
                pid = (
                    (cv256["value"] & 0xFF)
                    | ((cv255["value"] & 0x07) << 8)
                    | ((cv253["value"] & 0xFF) << 11)
                )
                product_id = str(pid)
            except HTTPException as e:
                product_id_error = e.detail
        # Filter by productID when we have one that matches at least
        # one candidate; if nothing matches, keep the family list so
        # the user still has something to pick from.
        if product_id is not None:
            narrowed = [
                c for c in candidates
                if c.product_id and c.product_id == product_id
            ]
            if narrowed:
                candidates = narrowed
        # Group candidates by (mfg, family) so the picker doesn't render
        # one row per model -- a SoundTraxx match on a fully-loaded
        # catalog can produce 700+ models. The user picks the family
        # here and then the specific model from the Model dropdown
        # above (which is filtered to the chosen family).
        from collections import defaultdict  # noqa: PLC0415
        by_family: dict[tuple[str, str], dict[str, Any]] = {}
        for c in candidates:
            key = (c.mfg, c.family)
            slot = by_family.setdefault(key, {
                "mfg": c.mfg, "family": c.family,
                "model_count": 0, "models": [],
            })
            slot["model_count"] += 1
            slot["models"].append(c.model)
        family_candidates = [by_family[k] for k in sorted(by_family)]
        return templates.TemplateResponse(
            request, "_programmer_identify_result.html",
            {
                "cv7": cv7["value"], "cv8": cv8["value"],
                "candidates": family_candidates,
                "total_models": len(candidates),
                "product_id": product_id,
                "product_id_error": product_id_error,
            },
        )

    @app.post("/programmer/cv/read", response_class=HTMLResponse)
    async def programmer_read_cv(
        request: Request, cv: int = Form(...), mode: str = Form("service"),
        address: int | None = Form(None),
    ) -> Any:
        payload: dict[str, Any] = {"cv": cv, "mode": mode}
        if address is not None:
            payload["address"] = address
        try:
            data = await _dispatch(handle, type="programmer", method="get", data=payload)
        except HTTPException as e:
            return templates.TemplateResponse(
                request, "_programmer_cv_result.html",
                {"cv": cv, "error": e.detail},
                status_code=e.status_code,
            )
        return templates.TemplateResponse(
            request, "_programmer_cv_result.html",
            {"cv": cv, "value": data["value"], "mode": data["mode"]},
        )

    @app.post("/programmer/cv/write", response_class=HTMLResponse)
    async def programmer_write_cv(
        request: Request, cv: int = Form(...), value: int = Form(...),
        mode: str = Form("service"), address: int | None = Form(None),
    ) -> Any:
        payload: dict[str, Any] = {"cv": cv, "value": value, "mode": mode}
        if address is not None:
            payload["address"] = address
        try:
            data = await _dispatch(handle, type="programmer", method="post", data=payload)
        except HTTPException as e:
            return templates.TemplateResponse(
                request, "_programmer_cv_result.html",
                {"cv": cv, "error": e.detail},
                status_code=e.status_code,
            )
        return templates.TemplateResponse(
            request, "_programmer_cv_result.html",
            {"cv": cv, "value": data["value"], "mode": data["mode"], "wrote": True},
        )

    # ----- scripts ----------------------------------------------------------

    def _repl_session():
        """Return the shared REPL session, lazy-creating on first hit.

        The session lives on ``ctx._repl_session`` so it survives
        across every browser tab hitting the /scripts page -- matching
        JMRI Java's Jython window semantics (one shared interpreter).
        Rebuilt from scratch on ``/scripts/repl/reset``.
        """
        from pmri_scripting import ReplSession  # noqa: PLC0415
        cached = getattr(handle.ctx, "_repl_session", None)
        if cached is not None:
            return cached
        # Piggy-back on the ScriptRunner to reuse its shim binding --
        # the ScriptService lazy-constructs the runner on first use;
        # do the same dance here so both share one JmriShim, one
        # dispatch-stream install, one connection.
        runner = getattr(handle.ctx, "_script_runner", None)
        if runner is None:
            from pmri_scripting import ScriptRunner  # noqa: PLC0415
            import asyncio as _asyncio  # noqa: PLC0415
            runner = ScriptRunner.for_connection(
                _asyncio.get_event_loop(), handle.ctx.connection,
            )
            handle.ctx._script_runner = runner   # type: ignore[attr-defined]
        session = ReplSession(runner.shim)
        handle.ctx._repl_session = session       # type: ignore[attr-defined]
        return session

    @app.post("/scripts/repl", response_class=HTMLResponse)
    async def scripts_repl(
        request: Request, source: str = Form(""),
    ) -> Any:  # type: ignore[valid-type]
        """Submit *source* to the persistent interpreter, render
        the console fragment with the updated history.

        Empty submissions are no-ops (rendering just refreshes
        the console) -- matches the CPython REPL's "just hit
        Enter" behavior at the prompt."""
        session = _repl_session()
        pending: str | None = None
        if source.strip():
            # ``submit_async`` off-loads the exec to a worker
            # thread so shim calls that block on our event loop
            # don't deadlock -- the loop stays free to advance
            # the shim's scheduled coroutines.
            result = await session.submit_async(source)
            if result.incomplete:
                pending = source
        return templates.TemplateResponse(
            request, "_repl_console.html",
            {
                "history": session.history,
                "pending_continuation": pending,
            },
        )

    @app.post("/scripts/repl/reset", response_class=HTMLResponse)
    async def scripts_repl_reset(request: Request) -> Any:  # type: ignore[valid-type]
        session = _repl_session()
        session.reset()
        return templates.TemplateResponse(
            request, "_repl_console.html",
            {"history": session.history, "pending_continuation": None},
        )

    @app.get("/scripts", response_class=HTMLResponse)
    async def scripts_home(request: Request) -> Any:
        statuses = await _dispatch(
            handle, type="scripts", method="list",
            data={"library": True},
        )
        session = _repl_session()
        return templates.TemplateResponse(
            request, "scripts.html",
            _base_context(handle, "scripts") | {
                "statuses": statuses,
                "history": session.history,
                "pending_continuation": None,
            },
        )

    @app.post("/scripts/run", response_class=HTMLResponse)
    async def scripts_run(
        request: Request,
        name: str = Form(...),
        source: str = Form(...),
        save: str | None = Form(None),
    ) -> Any:
        await _dispatch(
            handle, type="scripts", method="put",
            data={"name": name, "source": source, "save": bool(save)},
        )
        # Give the worker thread a moment to populate status.
        import asyncio as _asyncio  # noqa: PLC0415
        await _asyncio.sleep(0.05)
        statuses = await _dispatch(
            handle, type="scripts", method="list", data={"library": True},
        )
        return templates.TemplateResponse(
            request, "_scripts_table.html", {"statuses": statuses},
        )

    @app.post("/scripts/run-file/{name}", response_class=HTMLResponse)
    async def scripts_run_file(request: Request, name: str) -> Any:
        """Run an on-disk script by name.

        Distinct from ``/scripts/run`` which takes fresh source
        from the textarea. This route re-reads the file from disk
        every call, so the debug loop (edit externally -> click
        Run -> see output) works. The runner replaces any
        same-named existing status entry so re-runs stack cleanly.
        """
        try:
            await _dispatch(
                handle, type="scripts", method="post",
                data={"name": name, "action": "run_file"},
            )
        except HTTPException as exc:
            # Surface the 404 / permission error as a plain-text
            # banner in the table region so htmx doesn't blank the
            # UI on a stray click.
            statuses = await _dispatch(
                handle, type="scripts", method="list",
                data={"library": True},
            )
            return templates.TemplateResponse(
                request, "_scripts_table.html",
                {"statuses": statuses, "run_error": exc.detail},
            )
        # Same 50ms grace as /scripts/run so a fast-finishing
        # script has a chance to populate its status before the
        # table renders.
        import asyncio as _asyncio  # noqa: PLC0415
        await _asyncio.sleep(0.05)
        statuses = await _dispatch(
            handle, type="scripts", method="list", data={"library": True},
        )
        return templates.TemplateResponse(
            request, "_scripts_table.html", {"statuses": statuses},
        )

    @app.post("/scripts/stop/{name}", response_class=HTMLResponse)
    async def scripts_stop(request: Request, name: str) -> Any:
        await _dispatch(
            handle, type="scripts", method="post",
            data={"name": name, "action": "stop"},
        )
        statuses = await _dispatch(
            handle, type="scripts", method="list", data={"library": True},
        )
        return templates.TemplateResponse(
            request, "_scripts_table.html", {"statuses": statuses},
        )

    @app.post("/scripts/delete/{name}", response_class=HTMLResponse)
    async def scripts_delete(request: Request, name: str) -> Any:
        """Stop the named script AND remove its on-disk file so the
        profile autoload doesn't bring it back."""
        await _dispatch(
            handle, type="scripts", method="post",
            data={"name": name, "action": "delete"},
        )
        statuses = await _dispatch(
            handle, type="scripts", method="list", data={"library": True},
        )
        return templates.TemplateResponse(
            request, "_scripts_table.html", {"statuses": statuses},
        )

    @app.get("/scripts/refresh", response_class=HTMLResponse)
    async def scripts_refresh(request: Request) -> Any:
        """Re-render the scripts table with the latest captured output.

        Called from the per-row Refresh button so a long-running
        automaton's accumulated ``print()`` output surfaces without the
        user having to trigger a stop/start.
        """
        statuses = await _dispatch(
            handle, type="scripts", method="list", data={"library": True},
        )
        return templates.TemplateResponse(
            request, "_scripts_table.html", {"statuses": statuses},
        )

    # ----- profile CRUD -----------------------------------------------------
    # Wraps the ``configProfile`` JSON topic. Post-mutation we redirect
    # back to ``/`` so the browser re-fetches the profile list -- a
    # cleaner UX than a partial fragment when the "Load profile" form
    # (already on the page) needs to reflect the new list too.

    @app.post("/profiles/create", response_class=HTMLResponse)
    async def profiles_create(
        request: Request,
        name: str = Form(...),
    ) -> Any:
        from fastapi.responses import RedirectResponse  # noqa: PLC0415
        try:
            await _dispatch(handle, type="configProfile", method="put",
                            data={"name": name})
            # Rebuild profile_registry from disk so subsequent calls see
            # the new entry. The in-memory registry is what we already
            # mutated, so this is optional -- but paranoid safety.
        except HTTPException as exc:
            # Render home with an error banner instead of a bare 4xx page.
            return await _home_with_error(request, exc.detail)
        return RedirectResponse(url="/", status_code=303)

    @app.post("/profiles/{profile_id}/delete", response_class=HTMLResponse)
    async def profiles_delete(request: Request, profile_id: str) -> Any:
        from fastapi.responses import RedirectResponse  # noqa: PLC0415
        try:
            await _dispatch(handle, type="configProfile", method="delete",
                            data={"id": profile_id})
        except HTTPException as exc:
            return await _home_with_error(request, exc.detail)
        return RedirectResponse(url="/", status_code=303)

    async def _home_with_error(request: Request, message: str) -> Any:
        """Re-render the home page carrying an error banner. Used by
        the profile-CRUD routes when the JSON service rejects a
        request (duplicate name, active profile delete, etc.)."""
        power = await _dispatch(handle, type="power", method="get")
        fast_clock = await _dispatch(handle, type="time", method="get")
        registry = getattr(handle.ctx, "profile_registry", None)
        active = registry.active() if registry is not None else None
        log_tail_ctx = await _log_tail_context(handle)
        return templates.TemplateResponse(
            request, "home.html",
            _base_context(handle, "home") | {
                "power": power,
                "fast_clock": fast_clock,
                "connection": handle.ctx.connection,
                "command_station_on": bool(
                    getattr(handle.ctx.connection,
                            "is_command_station_on", False)
                ),
                "topics": _topics_with_help(handle),
                "profiles": [
                    {"id": p.id, "name": p.name, "path": p.path}
                    for p in (registry.profiles if registry is not None else [])
                ],
                "active_profile_id": active.id if active else None,
                "settings_root": (
                    str(registry.settings_root) if registry is not None else None
                ),
                "profile_error": message,
            } | log_tail_ctx,
        )

    # ----- cross-profile bean transfer --------------------------------------

    def _resolve_profile_name(profile_id: str) -> str:
        """Look up the friendly name for a profile id via the
        registry. Falls back to the raw id when the registry hasn't
        been loaded (bare test setups)."""
        registry = getattr(handle.ctx, "profile_registry", None)
        if registry is None:
            return profile_id
        ref = registry.by_id(profile_id)
        return ref.name if ref is not None else profile_id

    def _target_is_active(profile_id: str) -> bool:
        registry = getattr(handle.ctx, "profile_registry", None)
        if registry is None:
            return False
        active = registry.active()
        return active is not None and active.id == profile_id

    @app.post("/profiles/transfer/preview", response_class=HTMLResponse)
    async def profiles_transfer_preview(
        request: Request,
        from_id: str = Form(...),
        to_id: str = Form(...),
        types: list[str] = Form(default=[]),
    ) -> Any:
        try:
            data = await _dispatch(handle, type="beanTransfer", method="post", data={
                "action": "preview",
                "from": from_id, "to": to_id, "types": types,
            })
        except HTTPException as exc:
            return templates.TemplateResponse(
                request, "_bean_transfer_preview.html",
                {"error": exc.detail},
            )
        return templates.TemplateResponse(
            request, "_bean_transfer_preview.html",
            {
                "counts": data.get("counts", {}),
                "will_overwrite": data.get("willOverwrite", {}),
                "from_id": from_id,
                "to_id": to_id,
                "from_name": _resolve_profile_name(from_id),
                "to_name": _resolve_profile_name(to_id),
                "to_is_active": _target_is_active(to_id),
                "types": types,
            },
        )

    @app.post("/profiles/transfer/apply", response_class=HTMLResponse)
    async def profiles_transfer_apply(
        request: Request,
        from_id: str = Form(...),
        to_id: str = Form(...),
        types: list[str] = Form(default=[]),
    ) -> Any:
        try:
            data = await _dispatch(handle, type="beanTransfer", method="post", data={
                "action": "apply",
                "from": from_id, "to": to_id, "types": types,
            })
        except HTTPException as exc:
            return templates.TemplateResponse(
                request, "_bean_transfer_applied.html",
                {"error": exc.detail},
            )
        return templates.TemplateResponse(
            request, "_bean_transfer_applied.html",
            {
                "copied": data.get("copied", {}),
                "target_is_active": data.get("targetIsActive", False),
            },
        )

    # ----- signal systems ----------------------------------------------------

    @app.get("/signalsystems", response_class=HTMLResponse)
    async def signal_systems_home(request: Request) -> Any:
        systems = await _dispatch(
            handle, type="signalSystem", method="list",
        )
        return templates.TemplateResponse(
            request, "signalsystems.html",
            _base_context(handle, "signalsystems") | {"systems": systems},
        )

    # ----- logs widget -------------------------------------------------------

    _LOG_TAIL_MAX = 20

    async def _log_tail_context(_handle: "AppHandle") -> dict[str, Any]:
        """Build the recent-log widget's template context.

        Fetches via the ``logs`` service so the widget and any script
        or mobile client see byte-for-byte the same tail -- no chance
        of divergent formatting between the widget and the JSON API.
        """
        try:
            snapshot = await _dispatch(
                handle, type="logs", method="get",
                data={"tail": _LOG_TAIL_MAX},
            )
        except HTTPException:
            snapshot = {"path": None, "tail": _LOG_TAIL_MAX, "lines": []}
        return {
            "log_path": snapshot.get("path"),
            "log_lines": snapshot.get("lines") or [],
            "log_tail_max": snapshot.get("tail", _LOG_TAIL_MAX),
        }

    @app.get("/logs/refresh", response_class=HTMLResponse)
    async def logs_refresh(request: Request) -> Any:
        ctx = await _log_tail_context(handle)
        return templates.TemplateResponse(
            request, "_log_tail.html", ctx,
        )

    # ----- logixng -----------------------------------------------------------

    async def _logixng_list_fragment(request: Request) -> Any:
        programs = await _dispatch(handle, type="logixng", method="list")
        return templates.TemplateResponse(
            request, "_logixng_list.html", {"programs": programs},
        )

    @app.get("/logixng", response_class=HTMLResponse)
    async def logixng_home(request: Request) -> Any:
        programs = await _dispatch(handle, type="logixng", method="list")
        return templates.TemplateResponse(
            request, "logixng.html",
            _base_context(handle, "logixng") | {"programs": programs},
        )

    @app.get("/logixng/refresh", response_class=HTMLResponse)
    async def logixng_refresh(request: Request) -> Any:
        return await _logixng_list_fragment(request)

    @app.post("/logixng/create", response_class=HTMLResponse)
    async def logixng_create(
        request: Request,
        name: str = Form(...),
        userName: str = Form(""),
    ) -> Any:
        try:
            await _dispatch(handle, type="logixng", method="put", data={
                "name": name, "userName": userName,
            })
        except HTTPException as exc:
            programs = await _dispatch(
                handle, type="logixng", method="list",
            )
            return templates.TemplateResponse(
                request, "_logixng_list.html",
                {"programs": programs, "create_error": exc.detail},
                status_code=200,
            )
        return await _logixng_list_fragment(request)

    @app.post("/logixng/import-xml", response_class=HTMLResponse)
    async def logixng_import_xml(
        request: Request, file: UploadFile,
    ) -> Any:
        """Convert a JMRI Java LogixNG XML file into our tree
        format and load every program into the running manager.

        Bypasses the JSON put path so we can (a) skip re-encoding
        each program to a dict and back, and (b) collect
        importer warnings for surfacing to the operator. Each
        program is persisted so a server restart doesn't lose the
        import."""
        from pmri_logixng import import_pmri_logixng_xml  # noqa: PLC0415
        from pmri_server.services.logixng import _require_manager  # noqa: PLC0415

        raw = await file.read()
        if not raw:
            return templates.TemplateResponse(
                request, "_logixng_import_result.html",
                {"error": "empty file"},
                status_code=400,
            )
        try:
            result = import_pmri_logixng_xml(raw)
        except Exception as e:  # noqa: BLE001
            return templates.TemplateResponse(
                request, "_logixng_import_result.html",
                {"error": f"failed to parse XML: {e}"},
                status_code=400,
            )

        # Install each imported program under its JMRI system name
        # (the ``node_id`` we recorded during walk_programs). Persist
        # to the profile's logixng dir so the import survives a
        # restart -- same behaviour as the JSON put path.
        try:
            mgr = _require_manager(handle.ctx)
        except HTTPException as exc:
            return templates.TemplateResponse(
                request, "_logixng_import_result.html",
                {"error": f"manager unavailable: {exc.detail}"},
                status_code=503,
            )
        logixng_dir = getattr(handle.ctx, "_logixng_dir", None)
        imported_names: list[str] = []
        for program in result.programs:
            # Node stores the constructor's ``node_id`` on ``.id``
            # (with a uuid-suffixed fallback when the caller
            # didn't supply one). Prefer that, fall back to
            # user_name for the rare case the imported program
            # had neither.
            raw_name = program.id or program.user_name or "imported"
            # Colons are legal in JMRI Java system names (``IQ:AUTO:0001``)
            # but not in Windows filenames -- replace with dots so the
            # persisted YAML file is portable. On Linux/Pi the raw
            # name works fine; keeping the sanitization uniform
            # simplifies cross-platform testing.
            name = raw_name.replace(":", ".")
            await mgr.put(name, program)
            imported_names.append(name)
            if logixng_dir is not None:
                mgr.save_one(logixng_dir, name)

        # Load programs list for the out-of-band swap.
        programs = await _dispatch(
            handle, request, type="logixng", method="list",
        )
        return templates.TemplateResponse(
            request, "_logixng_import_result.html",
            {
                "imported": imported_names,
                "warnings": result.warnings,
                "programs": programs,
            },
        )

    @app.post("/logixng/{name}/delete", response_class=HTMLResponse)
    async def logixng_delete(request: Request, name: str) -> Any:
        try:
            await _dispatch(
                handle, type="logixng", method="delete", data={"name": name},
            )
        except HTTPException:
            pass
        return await _logixng_list_fragment(request)

    @app.post("/logixng/{name}/enable", response_class=HTMLResponse)
    async def logixng_enable(request: Request, name: str) -> Any:
        await _dispatch(handle, type="logixng", method="post", data={
            "name": name, "command": "enable",
        })
        return await _logixng_list_fragment(request)

    @app.post("/logixng/{name}/disable", response_class=HTMLResponse)
    async def logixng_disable(request: Request, name: str) -> Any:
        await _dispatch(handle, type="logixng", method="post", data={
            "name": name, "command": "disable",
        })
        return await _logixng_list_fragment(request)

    @app.post("/logixng/{name}/run", response_class=HTMLResponse)
    async def logixng_run(request: Request, name: str) -> Any:
        try:
            await _dispatch(handle, type="logixng", method="post", data={
                "name": name, "command": "run",
            })
        except HTTPException as exc:
            programs = await _dispatch(
                handle, type="logixng", method="list",
            )
            return templates.TemplateResponse(
                request, "_logixng_list.html",
                {"programs": programs, "run_error": exc.detail},
                status_code=200,
            )
        return await _logixng_list_fragment(request)

    @app.post("/logixng/{name}/run-conditional", response_class=HTMLResponse)
    async def logixng_run_conditional(
        request: Request, name: str,
        conditional_id: str = Form(...),
    ) -> Any:
        try:
            await _dispatch(handle, type="logixng", method="post", data={
                "name": name, "command": "run_conditional",
                "conditional_id": conditional_id,
            })
        except HTTPException as exc:
            programs = await _dispatch(
                handle, type="logixng", method="list",
            )
            return templates.TemplateResponse(
                request, "_logixng_list.html",
                {"programs": programs, "run_error": exc.detail},
                status_code=200,
            )
        return await _logixng_list_fragment(request)

    @app.get("/logixng/types", response_class=HTMLResponse)
    async def logixng_types(request: Request) -> Any:
        """Browse every registered LogixNG type_tag with its class name.

        Sorted by kind (container / action / expression / other) so the
        catalog is easy to skim when authoring YAML by hand. Kind is
        derived from the tag prefix -- ``action:...`` etc."""
        from pmri_logixng.registry import snapshot  # noqa: PLC0415

        def _kind(tag: str) -> str:
            if tag.startswith("action:"):
                return "action"
            if tag.startswith("expr:"):
                return "expression"
            if tag.startswith("logixng:"):
                return "container"
            return "other"

        rows = [
            {
                "kind": _kind(tag),
                "tag": tag,
                "cls": f"{cls.__module__}.{cls.__name__}",
            }
            for tag, cls in sorted(snapshot().items())
        ]
        rows.sort(key=lambda r: (r["kind"], r["tag"]))
        return templates.TemplateResponse(
            request, "logixng_types.html",
            _base_context(handle, "logixng") | {"types": rows},
        )

    @app.get("/logixng/{name}", response_class=HTMLResponse)
    async def logixng_detail(request: Request, name: str) -> Any:
        """Program detail page: full tree + editable YAML."""
        from pmri_logixng.serialize import to_yaml  # noqa: PLC0415

        prog = await _logixng_program_or_404(name)
        program = await _dispatch(
            handle, type="logixng", method="get", data={"name": name},
        )
        yaml_text = to_yaml(prog)
        return templates.TemplateResponse(
            request, "logixng_detail.html",
            _base_context(handle, "logixng") | {
                "program": program,
                "yaml_text": yaml_text,
                "name": name,
            } | _logixng_tree_context(prog),
        )

    @app.post("/logixng/{name}/save-yaml", response_class=HTMLResponse)
    async def logixng_save_yaml(
        request: Request, name: str, yaml: str = Form(...),
    ) -> Any:
        try:
            await _dispatch(handle, type="logixng", method="post", data={
                "name": name, "command": "replace_yaml", "yaml": yaml,
            })
        except HTTPException as exc:
            return HTMLResponse(
                f'<span style="color:var(--err,#c33)">Save failed: '
                f'{exc.detail}</span>',
                status_code=200,
            )
        return HTMLResponse(
            '<span style="color:var(--ok,#3a3)">Saved.</span>',
        )

    @app.post(
        "/logixng/{name}/conditional/{conditional_id}/enable",
        response_class=HTMLResponse,
    )
    async def logixng_conditional_enable(
        request: Request, name: str, conditional_id: str,
    ) -> Any:
        try:
            await _dispatch(handle, type="logixng", method="post", data={
                "name": name, "command": "conditional_enable",
                "conditional_id": conditional_id,
            })
        except HTTPException as exc:
            return HTMLResponse(
                f'<span style="color:var(--err,#c33)">{exc.detail}</span>',
                status_code=exc.status_code,
            )
        return HTMLResponse("ok")

    @app.post(
        "/logixng/{name}/conditional/{conditional_id}/disable",
        response_class=HTMLResponse,
    )
    async def logixng_conditional_disable(
        request: Request, name: str, conditional_id: str,
    ) -> Any:
        try:
            await _dispatch(handle, type="logixng", method="post", data={
                "name": name, "command": "conditional_disable",
                "conditional_id": conditional_id,
            })
        except HTTPException as exc:
            return HTMLResponse(
                f'<span style="color:var(--err,#c33)">{exc.detail}</span>',
                status_code=exc.status_code,
            )
        return HTMLResponse("ok")

    # ----- logixng visual tree editor -------------------------------------
    #
    # The YAML editor on the detail page covers power-user editing.
    # These routes supplement it with structural edits keyed by node_id:
    # add a child, delete a node, update a leaf's fields -- all through
    # the service's ``update_node`` / ``add_child`` / ``delete_node``
    # verbs. Pages return small HTML fragments htmx swaps into the tree
    # view so the operator stays on one screen.

    def _logixng_tree_context(prog: Any) -> dict[str, Any]:
        """Expose the tree-walker helpers to the template so a single
        recursive include can render any node."""
        from pmri_logixng.tree_ops import (  # noqa: PLC0415
            is_container, list_slot_names, single_slot_names,
        )
        return {
            "logixng_is_container": is_container,
            "logixng_list_slots": list_slot_names,
            "logixng_single_slots": single_slot_names,
            "root": prog,
        }

    async def _logixng_program_or_404(name: str) -> Any:
        from pmri_server.services.logixng import _require_manager  # noqa: PLC0415
        try:
            mgr = _require_manager(handle.ctx)
        except HTTPException as exc:
            raise exc
        prog = mgr.get(name)
        if prog is None:
            raise HTTPException(404, f"no logixng named {name!r}")
        return prog

    @app.get("/logixng/{name}/tree", response_class=HTMLResponse)
    async def logixng_tree_fragment(request: Request, name: str) -> Any:
        """Return the whole-tree fragment. Used after a structural edit
        to refresh the editor pane without a full page reload."""
        prog = await _logixng_program_or_404(name)
        return templates.TemplateResponse(
            request, "_logixng_tree.html",
            {"name": name} | _logixng_tree_context(prog),
        )

    @app.post("/logixng/{name}/node/{node_id}/delete",
              response_class=HTMLResponse)
    async def logixng_node_delete(
        request: Request, name: str, node_id: str,
    ) -> Any:
        try:
            await _dispatch(handle, type="logixng", method="post", data={
                "name": name, "command": "delete_node",
                "node_id": node_id,
            })
        except HTTPException as exc:
            return HTMLResponse(
                f'<div class="card" style="border-color:var(--err,#c33)">'
                f'<strong>Delete failed:</strong> {exc.detail}</div>',
                status_code=200,
            )
        return await logixng_tree_fragment(request, name)

    @app.get("/logixng/{name}/node/{node_id}/edit",
             response_class=HTMLResponse)
    async def logixng_node_edit_form(
        request: Request, name: str, node_id: str,
    ) -> Any:
        """Return an inline edit form for the node's leaf fields."""
        from pmri_logixng.tree_ops import find_node  # noqa: PLC0415
        prog = await _logixng_program_or_404(name)
        target = find_node(prog, node_id)
        if target is None:
            raise HTTPException(404, f"no node {node_id!r} in {name!r}")
        return templates.TemplateResponse(
            request, "_logixng_node_edit.html",
            {
                "name": name,
                "node": target,
                "node_dict": target.to_dict(),
            },
        )

    @app.post("/logixng/{name}/node/{node_id}/update",
              response_class=HTMLResponse)
    async def logixng_node_update(
        request: Request, name: str, node_id: str,
    ) -> Any:
        """Apply a form-posted field update. The form carries the
        node's current ``type_tag`` so the server rebuilds the node
        from a plain dict and splices it into the tree."""
        form = await request.form()
        node_fields: dict[str, Any] = {}
        # ``type`` is the type_tag the client echoes back; required so
        # the service can validate "same kind" before splicing.
        type_tag = form.get("type")
        if not isinstance(type_tag, str) or not type_tag:
            return HTMLResponse(
                '<div class="card" style="border-color:var(--err,#c33)">'
                '<strong>Update failed:</strong> missing type.</div>',
                status_code=200,
            )
        node_fields["type"] = type_tag
        for key, raw in form.multi_items():
            if key in ("type", "node_id"):
                continue
            node_fields[key] = _logixng_coerce_field(key, raw, type_tag)
        try:
            await _dispatch(handle, type="logixng", method="post", data={
                "name": name, "command": "update_node",
                "node_id": node_id, "fields": node_fields,
            })
        except HTTPException as exc:
            return HTMLResponse(
                f'<div class="card" style="border-color:var(--err,#c33)">'
                f'<strong>Update failed:</strong> {exc.detail}</div>',
                status_code=200,
            )
        return await logixng_tree_fragment(request, name)

    @app.get("/logixng/{name}/node/{node_id}/add",
             response_class=HTMLResponse)
    async def logixng_node_add_form(
        request: Request, name: str, node_id: str,
        slot: str = "",
    ) -> Any:
        """Return the add-child picker for a container node.

        If ``slot`` is empty, show a slot selector first (containers
        with multiple slots). After the operator picks a slot, the
        picker shows a type dropdown + a stub field form for the
        chosen child type.
        """
        from pmri_logixng.registry import snapshot  # noqa: PLC0415
        from pmri_logixng.tree_ops import (  # noqa: PLC0415
            find_node, list_slot_names, single_slot_names,
        )
        prog = await _logixng_program_or_404(name)
        parent = find_node(prog, node_id)
        if parent is None:
            raise HTTPException(404, f"no node {node_id!r} in {name!r}")
        all_slots = (
            list_slot_names(parent) + single_slot_names(parent)
        )
        # Available child types filtered by slot kind -- actions slots
        # take ``action:*``, expression slots take ``expr:*``; the
        # ``conditionals`` slot on logixng:root takes
        # ``logixng:conditional``.
        tags = sorted(snapshot())
        return templates.TemplateResponse(
            request, "_logixng_node_add.html",
            {
                "name": name,
                "parent_id": node_id,
                "parent_type": parent.type_tag,
                "slot": slot,
                "all_slots": all_slots,
                "candidate_tags": tags,
            },
        )

    @app.post("/logixng/{name}/node/{node_id}/add-child",
              response_class=HTMLResponse)
    async def logixng_node_add_child(
        request: Request, name: str, node_id: str,
    ) -> Any:
        """Commit the add-child picker. Form carries ``slot`` + the
        new child's ``type`` + any field inputs."""
        form = await request.form()
        slot = form.get("slot")
        child_type = form.get("child_type")
        if not isinstance(slot, str) or not isinstance(child_type, str):
            return HTMLResponse(
                '<div class="card" style="border-color:var(--err,#c33)">'
                '<strong>Add failed:</strong> missing slot or type.</div>',
                status_code=200,
            )
        # Start with the type's decoder-valid defaults so a bare Add
        # (no field inputs beyond slot + child_type) still constructs
        # a valid node; the operator fills in bean names + values via
        # the per-node Edit button afterwards.
        child = _logixng_default_child(child_type)
        for key, raw in form.multi_items():
            if key in ("slot", "child_type"):
                continue
            child[key] = _logixng_coerce_field(key, raw, child_type)
        try:
            await _dispatch(handle, type="logixng", method="post", data={
                "name": name, "command": "add_child",
                "parent_id": node_id, "slot": slot, "child": child,
            })
        except HTTPException as exc:
            return HTMLResponse(
                f'<div class="card" style="border-color:var(--err,#c33)">'
                f'<strong>Add failed:</strong> {exc.detail}</div>',
                status_code=200,
            )
        return await logixng_tree_fragment(request, name)

    # ----- consists ----------------------------------------------------------

    async def _consist_table_fragment(request: Request) -> Any:
        consists = await _dispatch(handle, type="consist", method="list")
        return templates.TemplateResponse(
            request, "_consists_table.html", {"consists": consists},
        )

    @app.get("/consists", response_class=HTMLResponse)
    async def consists_home(request: Request) -> Any:
        consists = await _dispatch(handle, type="consist", method="list")
        return templates.TemplateResponse(
            request, "consists.html",
            _base_context(handle, "consists") | {"consists": consists},
        )

    @app.get("/consists/refresh", response_class=HTMLResponse)
    async def consists_refresh(request: Request) -> Any:
        return await _consist_table_fragment(request)

    @app.post("/consists/create", response_class=HTMLResponse)
    async def consists_create(
        request: Request,
        address: int = Form(...),
        kind: str = Form("command_station"),
        comment: str = Form(""),
    ) -> Any:
        # Consist addresses are short 1..127 by spec (CV19); the service
        # enforces range + roster-collision. Any error there comes back
        # as an HTTPException -- render it into the notice so htmx sees a
        # 200 with the message rather than swapping in the FastAPI
        # default JSON error page.
        try:
            await _dispatch(handle, type="consist", method="put", data={
                "address": address, "kind": kind, "comment": comment,
            })
        except HTTPException as exc:
            return templates.TemplateResponse(
                request, "_consists_table.html",
                {
                    "consists": await _dispatch(
                        handle, type="consist", method="list"
                    ),
                    "create_error": exc.detail,
                },
                status_code=200,
            )
        return await _consist_table_fragment(request)

    @app.post("/consists/{address}/delete", response_class=HTMLResponse)
    async def consists_delete(request: Request, address: int) -> Any:
        try:
            await _dispatch(
                handle, type="consist", method="delete",
                data={"address": address},
            )
        except HTTPException:
            pass  # 404 -- table refresh below will just show whatever's left.
        return await _consist_table_fragment(request)

    @app.post("/consists/{address}/add-member", response_class=HTMLResponse)
    async def consists_add_member(
        request: Request, address: int,
        member_address: int = Form(...),
        long_address: str | None = Form(None),
        reversed: str | None = Form(None),
    ) -> Any:
        # Form has a "Reversed" checkbox -- unchecked (the default) means
        # the loco is oriented forward in the consist.
        await _dispatch(handle, type="consist", method="post", data={
            "address": address, "action": "add_member",
            "member": {
                "address": member_address,
                "long_address": bool(long_address),
                "direction_normal": not bool(reversed),
            },
        })
        return await _consist_table_fragment(request)

    @app.post("/consists/{address}/remove-member", response_class=HTMLResponse)
    async def consists_remove_member(
        request: Request, address: int,
        member_address: int = Form(...),
    ) -> Any:
        try:
            await _dispatch(handle, type="consist", method="post", data={
                "address": address, "action": "remove_member",
                "member_address": member_address,
            })
        except HTTPException:
            pass
        return await _consist_table_fragment(request)

    @app.post("/consists/{address}/set-speed", response_class=HTMLResponse)
    async def consists_set_speed(
        request: Request, address: int, speed: float = Form(...),
    ) -> Any:
        await _dispatch(handle, type="consist", method="post", data={
            "address": address, "action": "set_speed", "speed": speed,
        })
        return await _consist_table_fragment(request)

    @app.post("/consists/{address}/direction", response_class=HTMLResponse)
    async def consists_set_direction(
        request: Request, address: int, direction: str = Form(...),
    ) -> Any:
        await _dispatch(handle, type="consist", method="post", data={
            "address": address, "action": "set_direction", "direction": direction,
        })
        return await _consist_table_fragment(request)

    @app.post("/consists/{address}/estop", response_class=HTMLResponse)
    async def consists_estop(request: Request, address: int) -> Any:
        await _dispatch(handle, type="consist", method="post", data={
            "address": address, "action": "emergency_stop",
        })
        return await _consist_table_fragment(request)

    @app.post("/consists/refresh-from-cs", response_class=HTMLResponse)
    async def consists_refresh_from_cs(request: Request) -> Any:
        """Trigger a memory-mapped consist read-back on the driver.
        NCE only -- other connections respond 400 and the notice
        surfaces as a friendly banner above the table."""
        notice: str | None = None
        try:
            result = await _dispatch(
                handle, type="consist", method="post",
                data={"action": "refresh_from_cs"},
            )
            changed = (result or {}).get("changed") or []
            if changed:
                notice = (
                    f"Refreshed from CS -- {len(changed)} consist"
                    f"{'s' if len(changed) != 1 else ''} updated "
                    f"({', '.join(str(a) for a in changed)})."
                )
            else:
                notice = "Refreshed from CS -- no changes."
        except HTTPException as exc:
            notice = f"Refresh from CS failed: {exc.detail}"
        consists = await _dispatch(handle, type="consist", method="list")
        return templates.TemplateResponse(
            request, "_consists_table.html",
            {"consists": consists, "refresh_notice": notice},
        )

    # ----- OpenLCB / LCC discovery --------------------------------------

    async def _openlcb_active() -> bool:
        """True iff the active connection is an OpenLCB one -- checked
        by trying a cheap ``list`` call. Cheaper than importing
        OlcbConnection here (which would drag pmri_openlcb into the
        web-app import graph even on non-OpenLCB installs)."""
        try:
            await _dispatch(handle, type="openlcb", method="list")
        except HTTPException as exc:
            return exc.status_code != 400
        return True

    async def _openlcb_nodes_ctx() -> dict[str, Any]:
        try:
            nodes = await _dispatch(handle, type="openlcb", method="list")
        except HTTPException:
            nodes = []
        try:
            hosted = await _dispatch(handle, type="openlcbCS", method="list")
        except HTTPException:
            hosted = []
        try:
            health = await _dispatch(
                handle, type="openlcb", method="post",
                data={"action": "bus_health"},
            )
        except HTTPException:
            health = None
        return {"nodes": nodes, "hosted": hosted, "health": health}

    @app.get("/openlcb", response_class=HTMLResponse)
    async def openlcb_home(request: Request) -> Any:
        active = await _openlcb_active()
        ctx: dict[str, Any] = _base_context(handle, "openlcb") | {
            "olcb_active": active,
            "search_results": None,
            "search_error": None,
        }
        if active:
            ctx |= await _openlcb_nodes_ctx()
            try:
                ctx["snip"] = await _dispatch(
                    handle, type="openlcb", method="post",
                    data={"action": "snip_get"},
                )
            except HTTPException:
                ctx["snip"] = None
        else:
            ctx["nodes"] = []
            ctx["hosted"] = []
            ctx["health"] = None
            ctx["snip"] = None
        return templates.TemplateResponse(request, "openlcb.html", ctx)

    @app.get("/openlcb/health/refresh", response_class=HTMLResponse)
    async def openlcb_health_refresh(request: Request) -> Any:
        """Fragment endpoint for the auto-polling Bus health card."""
        try:
            health = await _dispatch(
                handle, type="openlcb", method="post",
                data={"action": "bus_health"},
            )
        except HTTPException:
            health = None
        return templates.TemplateResponse(
            request, "_openlcb_health.html", {"health": health},
        )

    @app.get("/openlcb/nodes/refresh", response_class=HTMLResponse)
    async def openlcb_nodes_refresh(request: Request) -> Any:
        """Fragment endpoint hit by the discovery page's auto-poll --
        returns just the _openlcb_nodes.html swap target so a newly-
        joined peer surfaces without an operator click."""
        ctx = await _openlcb_nodes_ctx()
        return templates.TemplateResponse(
            request, "_openlcb_nodes.html", ctx,
        )

    @app.post("/openlcb/prune_all", response_class=HTMLResponse)
    async def openlcb_prune_all(request: Request) -> Any:
        """Force-drop every discovered peer (except our own alias).
        Renders the discovery fragment with a notice describing what
        was dropped so the operator sees the reset take effect."""
        notice: str | None = None
        try:
            result = await _dispatch(
                handle, type="openlcb", method="post",
                data={"action": "prune_all"},
            )
            n = int(result.get("count", 0))
            if n == 0:
                notice = "No peers to reset."
            else:
                aliases = ", ".join(result.get("pruned", []))
                notice = f"Reset roster; dropped {n} peer(s): {aliases}"
        except HTTPException as exc:
            notice = f"Reset failed: {exc.detail}"
        ctx = await _openlcb_nodes_ctx()
        return templates.TemplateResponse(
            request, "_openlcb_nodes.html",
            ctx | {"discover_notice": notice},
        )

    @app.post("/openlcb/identity", response_class=HTMLResponse)
    async def openlcb_identity_update(
        request: Request,
        user_name: str = Form(""),
        user_description: str = Form(""),
    ) -> Any:
        """Update our advertised SNIP user_name + user_description
        and re-render the identity card with the new values. Peers
        see the change on their next SNIP probe (no re-advertisement
        is required)."""
        try:
            await _dispatch(
                handle, type="openlcb", method="post",
                data={
                    "action": "snip_update",
                    "user_name": user_name,
                    "user_description": user_description,
                },
            )
        except HTTPException:
            pass
        return await _openlcb_identity_fragment(request)

    async def _openlcb_identity_fragment(request: Request) -> Any:
        try:
            snip = await _dispatch(
                handle, type="openlcb", method="post",
                data={"action": "snip_get"},
            )
        except HTTPException:
            snip = None
        return templates.TemplateResponse(
            request, "_openlcb_identity.html", {"snip": snip},
        )

    @app.get("/openlcb/roster.json")
    async def openlcb_roster_export(request: Request) -> Response:
        """Download the current roster as JSON. Contains our known
        aliases with NID, cached SNIP/PIP, produced/consumed events
        + ranges, and last-seen timestamps -- enough to seed a fresh
        install with prior discovery state or attach to a bug
        report."""
        import json  # noqa: PLC0415
        from datetime import datetime, timezone  # noqa: PLC0415
        try:
            roster = await _dispatch(
                handle, type="openlcb", method="post",
                data={"action": "roster_export"},
            )
        except HTTPException as exc:
            return Response(
                content=json.dumps({"error": str(exc.detail)}),
                status_code=exc.status_code,
                media_type="application/json",
            )
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return Response(
            content=json.dumps(roster, indent=2, sort_keys=True) + "\n",
            media_type="application/json",
            headers={
                "Content-Disposition":
                    f'attachment; filename="openlcb-roster-{stamp}.json"',
            },
        )

    @app.post("/openlcb/roster/import", response_class=HTMLResponse)
    async def openlcb_roster_import(
        request: Request, file: UploadFile,
    ) -> HTMLResponse:
        """Load an exported roster JSON back into the SNIP + PIP
        caches. Our own alias is skipped so an import can't
        overwrite the local identity."""
        import json  # noqa: PLC0415
        try:
            raw = await file.read()
            roster = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            return HTMLResponse(
                f'<span style="color:var(--err,#c33)">'
                f'invalid JSON: {_html_escape(str(exc))}</span>',
            )
        try:
            result = await _dispatch(
                handle, type="openlcb", method="post",
                data={"action": "roster_import", "roster": roster},
            )
            return HTMLResponse(
                f'<span style="color:var(--ok,#3a3)">'
                f'imported: {result["nid_loaded"]} NID, '
                f'{result["snip_loaded"]} SNIP, '
                f'{result["pip_loaded"]} PIP '
                f'(of {result["nodes_seen"]} nodes)</span>',
            )
        except HTTPException as exc:
            return HTMLResponse(
                f'<span style="color:var(--err,#c33)">'
                f'import failed: {_html_escape(str(exc.detail))}</span>',
            )

    @app.post("/openlcb/health_sweep", response_class=HTMLResponse)
    async def openlcb_health_sweep(request: Request) -> Any:
        """Run node_health against every discovered alias in
        parallel and render the results as a small card. Empty
        card + notice when no aliases are known."""
        try:
            rows = await _dispatch(
                handle, type="openlcb", method="post",
                data={"action": "bus_wide_health", "timeout": 1.0},
            )
        except HTTPException as exc:
            return HTMLResponse(
                f'<div id="openlcb-health-sweep" class="card" '
                f'style="border-color:var(--err,#c33)">'
                f'Health sweep failed: {_html_escape(str(exc.detail))}'
                f'</div>',
            )
        return templates.TemplateResponse(
            request, "_openlcb_health_sweep.html", {"rows": rows},
        )

    @app.post("/openlcb/prune", response_class=HTMLResponse)
    async def openlcb_prune(request: Request) -> Any:
        """Force one immediate stale-peer sweep. Any alias silent
        past the sweeper's threshold is dropped; the discovery
        fragment re-renders with the updated roster + a notice
        describing what was pruned."""
        notice: str | None = None
        try:
            result = await _dispatch(
                handle, type="openlcb", method="post",
                data={"action": "prune_stale"},
            )
            n = int(result.get("count", 0))
            if n == 0:
                notice = "No stale peers to prune."
            else:
                aliases = ", ".join(result.get("pruned", []))
                notice = f"Pruned {n} peer(s): {aliases}"
        except HTTPException as exc:
            notice = f"Prune failed: {exc.detail}"
        ctx = await _openlcb_nodes_ctx()
        return templates.TemplateResponse(
            request, "_openlcb_nodes.html",
            ctx | {"discover_notice": notice},
        )

    @app.post("/openlcb/discover", response_class=HTMLResponse)
    async def openlcb_discover(request: Request) -> Any:
        """Two-stage discovery: broadcast Verify NID + Identify
        Events (populates the alias roster), briefly wait for
        replies to settle, then probe SNIP + PIP for every alias
        we heard from so the table lands populated with identity +
        capabilities on a single click."""
        import asyncio  # noqa: PLC0415
        notice: str | None = None
        try:
            await _dispatch(
                handle, type="openlcb", method="post",
                data={"action": "discover"},
            )
            # Give replies to the initial broadcast a moment to land
            # so probe_all sees the fresh aliases.
            await asyncio.sleep(0.15)
            enriched = await _dispatch(
                handle, type="openlcb", method="post",
                data={"action": "probe_all", "timeout": 1.0},
            )
            probed = sum(1 for n in enriched if n.get("snip") or n.get("pip"))
            notice = (
                f"Discovered {len(enriched)} node(s); probed identity + "
                f"capabilities for {probed}."
            )
        except HTTPException as exc:
            notice = f"Discovery failed: {exc.detail}"
        ctx = await _openlcb_nodes_ctx()
        return templates.TemplateResponse(
            request, "_openlcb_nodes.html",
            ctx | {"discover_notice": notice},
        )

    @app.get("/openlcb/nodes/{alias}", response_class=HTMLResponse)
    async def openlcb_node_detail(request: Request, alias: str) -> Any:
        """Detail page for one alias. The cached roster projection is
        rendered inline; SNIP + PIP are fetched only when the
        operator hits the Refresh identity button (they require
        wire round-trips)."""
        try:
            node = await _dispatch(
                handle, type="openlcb", method="get",
                data={"alias": alias},
            )
        except HTTPException as exc:
            # Bad alias / non-OpenLCB connection -> surface as a
            # rendered error page rather than a raw 400.
            return templates.TemplateResponse(
                request, "openlcb_node.html",
                _base_context(handle, "openlcb") | {
                    "node": {
                        "alias": alias, "node_id": None,
                        "produced_events": [], "consumed_events": [],
                    },
                    "snip": None, "pip": None,
                    "probe_error": exc.detail,
                    "alias": alias,
                    "frames": [],
                },
                status_code=exc.status_code,
            )
        # Initial frame log -- auto-polls after this render, but
        # showing something on first paint avoids a jarring flash.
        try:
            initial_frames = await _dispatch(
                handle, type="openlcb", method="post",
                data={
                    "action": "recent_frames",
                    "source_alias": alias,
                    "limit": 20,
                },
            )
        except HTTPException:
            initial_frames = []
        return templates.TemplateResponse(
            request, "openlcb_node.html",
            _base_context(handle, "openlcb") | {
                "node": node,
                "snip": None, "pip": None, "probe_error": None,
                "alias": alias,
                "frames": initial_frames,
            },
        )

    @app.post("/openlcb/nodes/{alias}/probe", response_class=HTMLResponse)
    async def openlcb_node_probe(request: Request, alias: str) -> Any:
        """Send SNIP + PIP probes and re-render just the identity
        fragment with the results. Errors on either probe surface as
        an inline notice -- the operator can then retry without
        losing the page."""
        node = await _dispatch(
            handle, type="openlcb", method="get",
            data={"alias": alias},
        )
        snip = None
        pip = None
        error: str | None = None
        try:
            snip = await _dispatch(
                handle, type="openlcb", method="post",
                data={"action": "probe_snip", "alias": alias, "timeout": 2.0},
            )
        except HTTPException as exc:
            error = f"SNIP: {exc.detail}"
        try:
            probe_pip = await _dispatch(
                handle, type="openlcb", method="post",
                data={"action": "probe_pip", "alias": alias, "timeout": 1.0},
            )
            pip = probe_pip.get("flags") if probe_pip else None
        except HTTPException as exc:
            # Combine both probe errors so the operator sees each.
            msg = f"PIP: {exc.detail}"
            error = f"{error}; {msg}" if error else msg
        return templates.TemplateResponse(
            request, "_openlcb_node_detail.html",
            {"node": node, "snip": snip, "pip": pip, "probe_error": error},
        )

    @app.get(
        "/openlcb/nodes/{alias}/pcer_history", response_class=HTMLResponse,
    )
    async def openlcb_node_pcer_history(
        request: Request, alias: str,
    ) -> Any:
        """Auto-polled fragment: per-event PCER count + last-seen for
        this peer. Newest-first."""
        try:
            entries = await _dispatch(
                handle, type="openlcb", method="post",
                data={"action": "pcer_history", "alias": alias},
            )
        except HTTPException:
            entries = []
        return templates.TemplateResponse(
            request, "_openlcb_node_pcer_history.html",
            {"alias": alias, "entries": entries},
        )

    @app.get("/openlcb/nodes/{alias}/frames", response_class=HTMLResponse)
    async def openlcb_node_frames(request: Request, alias: str) -> Any:
        """Auto-polled fragment: recent frames from this specific
        alias only. Uses the same recent_frames action + source_alias
        filter as the main monitor, so what shows up here is a
        strict subset of the bus log."""
        try:
            frames = await _dispatch(
                handle, type="openlcb", method="post",
                data={
                    "action": "recent_frames",
                    "source_alias": alias,
                    "limit": 20,
                },
            )
        except HTTPException:
            frames = []
        return templates.TemplateResponse(
            request, "_openlcb_node_frames.html",
            {"alias": alias, "frames": frames},
        )

    @app.post(
        "/openlcb/nodes/{alias}/identify_events",
        response_class=HTMLResponse,
    )
    async def openlcb_node_identify_events(
        request: Request, alias: str,
    ) -> HTMLResponse:
        """Fire MTI 0x0968 (Identify Events Addressed) at this peer.
        Reply frames land in the roster asynchronously; the operator
        watches the events section auto-refresh."""
        try:
            await _dispatch(
                handle, type="openlcb", method="post",
                data={
                    "action": "identify_events_addressed",
                    "alias": alias,
                },
            )
            return HTMLResponse(
                f'<span style="color:var(--ok,#3a3)">'
                f'sent Identify Events to {_html_escape(alias)}; '
                f'replies will populate the events list shortly.'
                f'</span>',
            )
        except HTTPException as exc:
            return HTMLResponse(
                f'<span style="color:var(--err,#c33)">'
                f'{_html_escape(str(exc.detail))}</span>',
            )

    @app.post("/openlcb/nodes/{alias}/health", response_class=HTMLResponse)
    async def openlcb_node_health(request: Request, alias: str) -> Any:
        """Time Verify NID + PIP + SNIP against the peer and render a
        small fragment summarising each round-trip + overall status.
        Timeout is fixed at 2s per probe -- enough that a healthy peer
        answers well inside it but not so long that a click stalls
        the UI when the peer is offline."""
        try:
            health = await _dispatch(
                handle, type="openlcb", method="post",
                data={"action": "node_health", "alias": alias, "timeout": 2.0},
            )
        except HTTPException as exc:
            return HTMLResponse(
                f'<div id="openlcb-node-health" style="color:var(--err,#c33)">'
                f'Health check failed: {_html_escape(str(exc.detail))}</div>',
            )
        return templates.TemplateResponse(
            request, "_openlcb_node_health.html",
            {"health": health, "alias": alias},
        )

    @app.get("/openlcb/nodes/{alias}/fdi", response_class=HTMLResponse)
    async def openlcb_fdi_page(request: Request, alias: str) -> Any:
        """FDI viewer -- read-only labels for a loco's functions.

        Unlike CDI, FDI isn't editable through the memory config
        protocol (it's the loco author's static description); the
        page is a viewer only."""
        try:
            fdi = await _dispatch(
                handle, type="openlcb", method="post",
                data={"action": "fdi_fetch", "alias": alias},
            )
            return templates.TemplateResponse(
                request, "openlcb_fdi.html",
                _base_context(handle, "openlcb") | {
                    "alias": alias, "fdi": fdi, "fetch_error": None,
                },
            )
        except HTTPException as exc:
            return templates.TemplateResponse(
                request, "openlcb_fdi.html",
                _base_context(handle, "openlcb") | {
                    "alias": alias, "fdi": None, "fetch_error": exc.detail,
                },
                status_code=exc.status_code,
            )

    @app.get("/openlcb/nodes/{alias}/cdi", response_class=HTMLResponse)
    async def openlcb_cdi_page(request: Request, alias: str) -> Any:
        """CDI editor page. Fetches the node's CDI on load; errors
        (peer offline, malformed XML, non-OpenLCB connection) render
        as an inline banner with an empty tree, so the page shell
        still lets the operator navigate back."""
        try:
            cdi = await _dispatch(
                handle, type="openlcb", method="post",
                data={"action": "cdi_fetch", "alias": alias},
            )
            return templates.TemplateResponse(
                request, "openlcb_cdi.html",
                _base_context(handle, "openlcb") | {
                    "alias": alias,
                    "cdi": cdi,
                    "fetch_error": None,
                },
            )
        except HTTPException as exc:
            return templates.TemplateResponse(
                request, "openlcb_cdi.html",
                _base_context(handle, "openlcb") | {
                    "alias": alias,
                    "cdi": None,
                    "fetch_error": exc.detail,
                },
                status_code=exc.status_code,
            )

    @app.get("/openlcb/nodes/{alias}/cdi/read", response_class=HTMLResponse)
    async def openlcb_cdi_read(
        request: Request, alias: str,
        space: int, offset: int, size: int, type: str,
        bit: int = 0,
    ) -> HTMLResponse:
        """Read one field. Returns raw HTML for the status cell --
        just the current value as text, or a red error message on
        failure. Kept plain (no template) since it's a one-cell
        fragment."""
        try:
            result = await _dispatch(
                handle, type="openlcb", method="post",
                data={
                    "action": "cdi_read", "alias": alias,
                    "space": space, "offset": offset, "size": size,
                    "type": type, "bit": bit,
                },
            )
            value = result.get("value", "")
            return HTMLResponse(
                f'<span title="last read">{_html_escape(str(value))}</span>',
            )
        except HTTPException as exc:
            return HTMLResponse(
                f'<span style="color:var(--err,#c33)">{_html_escape(str(exc.detail))}</span>',
            )

    @app.post("/openlcb/nodes/{alias}/cdi/write", response_class=HTMLResponse)
    async def openlcb_cdi_write(
        request: Request, alias: str,
        space: int = Form(...), offset: int = Form(...),
        size: int = Form(...), type: str = Form(...),
        value: str = Form(""), bit: int = Form(0),
    ) -> HTMLResponse:
        """Write one field. Form value comes in as a string always;
        the JSON layer coerces to the right Python type based on the
        ``type`` param. Empty string is treated as "clear the field"
        for strings, and as 0 for numeric types."""
        try:
            payload: Any = value
            if type == "int":
                payload = int(value) if value else 0
            elif type == "float":
                payload = float(value) if value else 0.0
            elif type == "bit":
                # Unchecked checkbox posts nothing (or empty); "1"
                # is the checked marker; anything else falsy -> off.
                payload = value == "1"
            await _dispatch(
                handle, type="openlcb", method="post",
                data={
                    "action": "cdi_write", "alias": alias,
                    "space": space, "offset": offset, "size": size,
                    "type": type, "bit": bit,
                    "value": payload,
                },
            )
            return HTMLResponse(
                '<span style="color:var(--ok,#3a3)">saved</span>',
            )
        except HTTPException as exc:
            return HTMLResponse(
                f'<span style="color:var(--err,#c33)">{_html_escape(str(exc.detail))}</span>',
            )
        except (TypeError, ValueError) as exc:
            return HTMLResponse(
                f'<span style="color:var(--err,#c33)">'
                f'{_html_escape(str(exc))}</span>',
            )

    @app.post("/openlcb/nodes/{alias}/reboot", response_class=HTMLResponse)
    async def openlcb_node_reboot(request: Request, alias: str) -> HTMLResponse:
        """Send a Memory Config Reboot (0xA9) to the target. The peer
        typically drops off the bus for a moment as it restarts. The
        response is a small in-place notice; the discovery page's
        auto-refresh picks up the reconnection."""
        try:
            await _dispatch(
                handle, type="openlcb", method="post",
                data={
                    "action": "memory_reboot",
                    "alias": alias, "timeout": 3.0,
                },
            )
            return HTMLResponse(
                f'<span style="color:var(--ok,#3a3)">'
                f'reboot sent to {_html_escape(alias)}</span>',
            )
        except HTTPException as exc:
            return HTMLResponse(
                f'<span style="color:var(--err,#c33)">'
                f'reboot failed: {_html_escape(str(exc.detail))}</span>',
            )

    @app.post(
        "/openlcb/nodes/{alias}/firmware/upgrade",
        response_class=HTMLResponse,
    )
    async def openlcb_firmware_upgrade(
        request: Request, alias: str, file: UploadFile,
    ) -> HTMLResponse:
        """Upload a firmware binary and run the S-9.7.4.3 upgrade
        against the target. Blocks until done -- the request response
        carries the outcome. Large firmware files may take tens of
        seconds; the client should show a spinner during the upload."""
        try:
            data = await file.read()
        except Exception as exc:  # noqa: BLE001
            return HTMLResponse(
                f'<span style="color:var(--err,#c33)">'
                f'failed to read upload: {_html_escape(str(exc))}</span>',
            )
        if not data:
            return HTMLResponse(
                '<span style="color:var(--err,#c33)">'
                'firmware file is empty</span>',
            )
        try:
            result = await _dispatch(
                handle, type="openlcb", method="post",
                data={
                    "action": "firmware_upgrade",
                    "alias": alias,
                    "firmware_hex": data.hex(),
                },
            )
            return HTMLResponse(
                f'<span style="color:var(--ok,#3a3)">'
                f'firmware uploaded: {result["bytes_written"]} bytes '
                f'sent to {_html_escape(alias)}</span>',
            )
        except HTTPException as exc:
            return HTMLResponse(
                f'<span style="color:var(--err,#c33)">'
                f'firmware upgrade failed: {_html_escape(str(exc.detail))}</span>',
            )

    @app.post(
        "/openlcb/nodes/{alias}/factory_reset", response_class=HTMLResponse,
    )
    async def openlcb_node_factory_reset(
        request: Request, alias: str,
    ) -> HTMLResponse:
        """Send a Memory Config Factory Reset (0xAA). Payload carries
        the target's NodeID as a safety guard -- we fetch the cached
        NodeID from the discovery projection so a stray click still
        can't reset an arbitrary peer."""
        # Look up the target's NodeID via the get action so the
        # safety payload is always in sync with what the operator
        # sees on the detail page.
        try:
            node = await _dispatch(
                handle, type="openlcb", method="get",
                data={"alias": alias},
            )
        except HTTPException as exc:
            return HTMLResponse(
                f'<span style="color:var(--err,#c33)">'
                f'factory reset failed: {_html_escape(str(exc.detail))}</span>',
            )
        nid = node.get("node_id") if node else None
        if not nid:
            return HTMLResponse(
                '<span style="color:var(--err,#c33)">factory reset '
                'refused: this node\'s NodeID isn\'t cached. Run '
                '<em>Discover</em> so we can send the safety '
                'payload the spec requires.</span>',
            )
        try:
            await _dispatch(
                handle, type="openlcb", method="post",
                data={
                    "action": "memory_factory_reset",
                    "alias": alias, "target_node_id": nid,
                    "timeout": 3.0,
                },
            )
            return HTMLResponse(
                f'<span style="color:var(--ok,#3a3)">'
                f'factory reset sent to {_html_escape(alias)}</span>',
            )
        except HTTPException as exc:
            return HTMLResponse(
                f'<span style="color:var(--err,#c33)">'
                f'factory reset failed: {_html_escape(str(exc.detail))}</span>',
            )

    async def _openlcb_hosted_fragment(
        request: Request, hosted_error: str | None = None,
    ) -> Any:
        try:
            hosted = await _dispatch(
                handle, type="openlcbCS", method="list",
            )
        except HTTPException:
            hosted = []
        return templates.TemplateResponse(
            request, "_openlcb_hosted.html",
            {"hosted": hosted, "hosted_error": hosted_error},
        )

    @app.post("/openlcb/cs/add", response_class=HTMLResponse)
    async def openlcb_cs_add(
        request: Request,
        address: int = Form(...),
        long_address: str | None = Form(None),
    ) -> Any:
        long_flag = bool(long_address)
        try:
            await _dispatch(
                handle, type="openlcbCS", method="post",
                data={
                    "action": "add",
                    "address": address,
                    "long_address": long_flag,
                },
            )
        except HTTPException as exc:
            return await _openlcb_hosted_fragment(request, exc.detail)
        return await _openlcb_hosted_fragment(request)

    @app.post("/openlcb/cs/remove", response_class=HTMLResponse)
    async def openlcb_cs_remove(
        request: Request,
        address: int = Form(...),
        long_address: int = Form(0),
    ) -> Any:
        try:
            await _dispatch(
                handle, type="openlcbCS", method="post",
                data={
                    "action": "remove",
                    "address": address,
                    "long_address": bool(long_address),
                },
            )
        except HTTPException as exc:
            return await _openlcb_hosted_fragment(request, exc.detail)
        return await _openlcb_hosted_fragment(request)

    # ----- OpenLCB / LCC bus monitor ------------------------------------

    async def _openlcb_monitor_fragment(
        request: Request, limit: int = 100,
    ) -> Any:
        # Filter values arrive in query params on GETs (Refresh +
        # auto-poll) and in the form body on POSTs (Clear). Read
        # from whichever the request carries. Missing/empty strings
        # mean "no filter" -- the service treats them the same way.
        params = request.query_params
        if not params and request.method == "POST":
            try:
                form = await request.form()
                params = form  # type: ignore[assignment]
            except Exception:
                params = request.query_params
        filter_src = params.get("src", "")
        filter_mti = params.get("mti", "")
        filter_text = params.get("text", "")
        # Invert flags -- presence of the checkbox with any truthy
        # value counts. htmx sends "on" for a checked box; also
        # accept "1" / "true" for programmatic callers.
        filter_src_invert = params.get("src_invert") in ("on", "1", "true")
        filter_mti_invert = params.get("mti_invert") in ("on", "1", "true")
        filter_text_invert = params.get("text_invert") in ("on", "1", "true")
        # Row limit: clamp to a sane range so a rogue query string
        # can't request 10^9 rows. 500 is the visible ceiling; the
        # default matches the initial page render.
        try:
            requested = int(params.get("limit", limit))
            limit = max(1, min(requested, 500))
        except (TypeError, ValueError):
            pass
        try:
            frames = await _dispatch(
                handle, type="openlcb", method="post",
                data={
                    "action": "recent_frames", "limit": limit,
                    "source_alias": filter_src,
                    "source_alias_invert": filter_src_invert,
                    "frame_type": filter_mti,
                    "frame_type_invert": filter_mti_invert,
                    "text": filter_text,
                    "text_invert": filter_text_invert,
                },
            )
        except HTTPException:
            frames = []
        return templates.TemplateResponse(
            request, "_openlcb_monitor.html",
            {
                "frames": frames,
                "filter_src": filter_src,
                "filter_src_invert": filter_src_invert,
                "filter_mti": filter_mti,
                "filter_mti_invert": filter_mti_invert,
                "filter_text": filter_text,
                "filter_text_invert": filter_text_invert,
                "filter_limit": limit,
            },
        )

    async def _openlcb_stats_fragment(request: Request) -> Any:
        try:
            stats = await _dispatch(
                handle, type="openlcb", method="post",
                data={"action": "bus_stats", "top_n": 5},
            )
        except HTTPException:
            stats = None
        return templates.TemplateResponse(
            request, "_openlcb_stats.html", {"stats": stats},
        )

    @app.get("/openlcb/monitor", response_class=HTMLResponse)
    async def openlcb_monitor_home(request: Request) -> Any:
        active = await _openlcb_active()
        ctx: dict[str, Any] = _base_context(handle, "openlcb") | {
            "olcb_active": active,
        }
        # Filter values echoed into the form; initial page load has
        # nothing pre-selected (the operator hasn't typed yet), so
        # empty strings are the correct default.
        ctx["filter_src"] = ""
        ctx["filter_src_invert"] = False
        ctx["filter_mti"] = ""
        ctx["filter_mti_invert"] = False
        ctx["filter_text"] = ""
        ctx["filter_text_invert"] = False
        ctx["filter_limit"] = 100
        if active:
            try:
                ctx["frames"] = await _dispatch(
                    handle, type="openlcb", method="post",
                    data={"action": "recent_frames", "limit": 100},
                )
            except HTTPException:
                ctx["frames"] = []
            try:
                ctx["stats"] = await _dispatch(
                    handle, type="openlcb", method="post",
                    data={"action": "bus_stats", "top_n": 5},
                )
            except HTTPException:
                ctx["stats"] = None
            try:
                ctx["presets"] = await _dispatch(
                    handle, type="openlcb", method="post",
                    data={"action": "send_frame_presets"},
                )
            except HTTPException:
                ctx["presets"] = []
            try:
                ctx["send_history"] = await _dispatch(
                    handle, type="openlcb", method="post",
                    data={"action": "send_frame_history"},
                )
            except HTTPException:
                ctx["send_history"] = []
            try:
                ctx["well_known_events"] = await _dispatch(
                    handle, type="openlcb", method="post",
                    data={"action": "well_known_events"},
                )
            except HTTPException:
                ctx["well_known_events"] = []
            # Initial pause-button label derives from live monitor
            # state so a page reload after pausing shows "Resume".
            ctx["paused"] = bool(ctx["stats"].get("paused")) if ctx["stats"] else False
            ctx["our_alias"] = (
                ctx["stats"].get("our_alias") if ctx["stats"] else None
            )
        else:
            ctx["frames"] = []
            ctx["stats"] = None
            ctx["presets"] = []
            ctx["send_history"] = []
            ctx["well_known_events"] = []
            ctx["paused"] = False
            ctx["our_alias"] = None
        return templates.TemplateResponse(
            request, "openlcb_monitor.html", ctx,
        )

    @app.get("/openlcb/monitor/refresh", response_class=HTMLResponse)
    async def openlcb_monitor_refresh(request: Request) -> Any:
        return await _openlcb_monitor_fragment(request)

    @app.get("/openlcb/monitor/sse")
    async def openlcb_monitor_sse(request: Request) -> StreamingResponse:
        """Server-Sent Events push -- nudges the client on every new
        frame so the monitor updates in near real-time instead of
        waiting for the 2s poll tick.

        Each nudge is a plain ``data: refresh`` message; the client
        listens with a native EventSource + fires a custom event
        that the existing hx-get container picks up. Because the
        client re-fetches via the standard refresh route, filters
        and inverts are automatically honoured.

        A ``maxsize=1`` coalescing queue keeps a burst of frames
        from flooding the client -- many arrivals collapse to one
        pending nudge. Heartbeats every 15s keep intermediary
        proxies from closing an idle connection."""
        import asyncio  # noqa: PLC0415

        async def event_gen():
            conn = getattr(handle.ctx, "connection", None)
            if conn is None or not hasattr(conn, "traffic"):
                # No OpenLCB connection -- emit a single terminator so
                # the client's EventSource sees a clean close instead
                # of a hanging request.
                yield "event: no_connection\ndata: none\n\n"
                return
            tc = conn.traffic
            queue: asyncio.Queue[None] = asyncio.Queue(maxsize=1)

            def _push(_frame) -> None:  # noqa: ANN001
                # Sync callback: try to enqueue; drop silently when a
                # nudge is already pending -- the client fetches the
                # freshest state on its next tick anyway.
                try:
                    queue.put_nowait(None)
                except asyncio.QueueFull:
                    pass

            unsub_in = tc.frames.connect(_push)
            unsub_out = tc.sent_frames.connect(_push)
            try:
                # Prime the client with an immediate nudge so it
                # aligns with the current buffer without waiting
                # for the first inbound frame.
                yield "data: refresh\n\n"
                while True:
                    try:
                        await asyncio.wait_for(queue.get(), timeout=15.0)
                        yield "data: refresh\n\n"
                    except asyncio.TimeoutError:
                        # SSE comment line keeps the connection open
                        # through timing-out proxies.
                        yield ": heartbeat\n\n"
                    if await request.is_disconnected():
                        break
            finally:
                unsub_in()
                unsub_out()

        return StreamingResponse(
            event_gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                # Long-poll style -- some browsers close on default.
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/openlcb/monitor/stats", response_class=HTMLResponse)
    async def openlcb_monitor_stats(request: Request) -> Any:
        return await _openlcb_stats_fragment(request)

    @app.post("/openlcb/monitor/send", response_class=HTMLResponse)
    async def openlcb_monitor_send(
        request: Request,
        header: str = Form(...),
        data: str = Form(""),
    ) -> HTMLResponse:
        """Small in-place fragment: green ok / red error span. On
        success the response triggers an ``openlcb-send-ok`` custom
        event via HX-Trigger; the Recent sends panel listens for it
        and refreshes itself so the resend button list stays current
        without a full page reload."""
        try:
            result = await _dispatch(
                handle, type="openlcb", method="post",
                data={
                    "action": "send_frame",
                    "header": header, "data": data,
                },
            )
            resp = HTMLResponse(
                f'<span style="color:var(--ok,#3a3)">'
                f'sent: {_html_escape(result["header"])} '
                f'{_html_escape(result["data"]) or "(no data)"}</span>',
            )
            resp.headers["HX-Trigger"] = "openlcb-send-ok"
            return resp
        except HTTPException as exc:
            return HTMLResponse(
                f'<span style="color:var(--err,#c33)">'
                f'{_html_escape(str(exc.detail))}</span>',
            )

    async def _openlcb_send_history_fragment(request: Request) -> Any:
        try:
            history = await _dispatch(
                handle, type="openlcb", method="post",
                data={"action": "send_frame_history"},
            )
        except HTTPException:
            history = []
        return templates.TemplateResponse(
            request, "_openlcb_send_history.html",
            {"send_history": history},
        )

    @app.get(
        "/openlcb/monitor/send_history", response_class=HTMLResponse,
    )
    async def openlcb_monitor_send_history(request: Request) -> Any:
        """Fragment endpoint the send form polls after each send to
        refresh the Recent sends panel."""
        return await _openlcb_send_history_fragment(request)

    @app.post(
        "/openlcb/monitor/send_datagram", response_class=HTMLResponse,
    )
    async def openlcb_monitor_send_datagram(
        request: Request,
        alias: str = Form(...),
        data: str = Form(""),
        timeout: float = Form(3.0),
    ) -> HTMLResponse:
        """Send an arbitrary datagram to a peer. Shows Received OK
        with the byte count on success, Rejected(code+description)
        on peer reject, timeout otherwise."""
        try:
            result = await _dispatch(
                handle, type="openlcb", method="post",
                data={
                    "action": "send_datagram",
                    "alias": alias, "data": data,
                    "timeout": timeout,
                },
            )
            if result.get("rejected"):
                return HTMLResponse(
                    f'<span style="color:var(--warn,#c93)">'
                    f'peer rejected: {_html_escape(result["description"])} '
                    f'({_html_escape(result["error_code"])})</span>',
                )
            return HTMLResponse(
                f'<span style="color:var(--ok,#3a3)">'
                f'sent {result["bytes"]} byte(s) to '
                f'{_html_escape(result["alias"])} (Received OK)</span>',
            )
        except HTTPException as exc:
            return HTMLResponse(
                f'<span style="color:var(--err,#c33)">'
                f'{_html_escape(str(exc.detail))}</span>',
            )

    @app.post("/openlcb/monitor/toggle_pause", response_class=HTMLResponse)
    async def openlcb_monitor_toggle_pause(request: Request) -> HTMLResponse:
        """Flip the monitor between paused + running, then re-render
        the pause button so its label reflects the new state. Reads
        the current state from bus_stats to decide which direction
        to flip in."""
        try:
            stats = await _dispatch(
                handle, type="openlcb", method="post",
                data={"action": "bus_stats", "top_n": 1},
            )
        except HTTPException:
            stats = {}
        currently_paused = bool(stats.get("paused", False))
        action = "resume_monitor" if currently_paused else "pause_monitor"
        try:
            await _dispatch(
                handle, type="openlcb", method="post",
                data={"action": action},
            )
        except HTTPException:
            pass
        return templates.TemplateResponse(
            request, "_openlcb_pause_button.html",
            {"paused": not currently_paused},
        )

    @app.get("/openlcb/monitor/export_session.zip")
    async def openlcb_monitor_export_session(request: Request) -> Response:
        """Download the current filtered frame log plus metadata as a
        ZIP archive. Contains ``frames.csv`` (same content as the
        plain CSV export) and ``metadata.json`` with connection
        info, filter state, and a bus stats snapshot -- enough for
        offline analysis or bug-report attachments without having
        to re-derive context by hand."""
        import csv  # noqa: PLC0415
        import io  # noqa: PLC0415
        import json  # noqa: PLC0415
        import zipfile  # noqa: PLC0415
        from datetime import datetime, timezone  # noqa: PLC0415

        params = request.query_params
        filter_state = {
            "source_alias": params.get("src", ""),
            "source_alias_invert":
                params.get("src_invert") in ("on", "1", "true"),
            "frame_type": params.get("mti", ""),
            "frame_type_invert":
                params.get("mti_invert") in ("on", "1", "true"),
            "text": params.get("text", ""),
            "text_invert":
                params.get("text_invert") in ("on", "1", "true"),
        }
        try:
            frames = await _dispatch(
                handle, type="openlcb", method="post",
                data={"action": "recent_frames", **filter_state},
            )
        except HTTPException:
            frames = []
        try:
            stats = await _dispatch(
                handle, type="openlcb", method="post",
                data={"action": "bus_stats", "top_n": 10},
            )
        except HTTPException:
            stats = None

        # Best-effort: our alias + NodeID from the connection.
        our_alias = stats.get("our_alias") if stats else None
        our_node_id = None
        try:
            conn = getattr(handle.ctx, "connection", None)
            if conn is not None and hasattr(conn, "node_id"):
                our_node_id = conn.node_id.dotted()
        except Exception:
            pass

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        metadata = {
            "exported_at": (
                datetime.now(timezone.utc).isoformat(timespec="seconds")
            ),
            "our_alias": our_alias,
            "our_node_id": our_node_id,
            "filter": filter_state,
            "frame_count": len(frames),
            "bus_stats": stats,
        }

        # Build frames.csv in memory using the same layout as the
        # plain CSV export.
        csv_buf = io.StringIO()
        writer = csv.writer(csv_buf, lineterminator="\n")
        writer.writerow(
            ["timestamp", "source_alias", "frame_type",
             "header", "summary", "data_hex"],
        )
        for f in frames:
            writer.writerow([
                f"{f['timestamp']:.3f}",
                f["source_alias"],
                f"{f['frame_type']:#x}",
                f["header"],
                f["summary"],
                f["data_hex"],
            ])

        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("frames.csv", csv_buf.getvalue())
            zf.writestr(
                "metadata.json",
                json.dumps(metadata, indent=2, sort_keys=True) + "\n",
            )
        return Response(
            content=zip_buf.getvalue(),
            media_type="application/zip",
            headers={
                "Content-Disposition":
                    f'attachment; filename="openlcb-session-{stamp}.zip"',
            },
        )

    @app.get("/openlcb/monitor/export.csv")
    async def openlcb_monitor_export_csv(request: Request) -> Response:
        """Download the current filtered frame log as CSV. Same
        filters as the on-page table so what an operator sees is
        exactly what they download -- filter first, then click."""
        import csv  # noqa: PLC0415
        import io  # noqa: PLC0415
        from datetime import datetime, timezone  # noqa: PLC0415
        params = request.query_params
        try:
            frames = await _dispatch(
                handle, type="openlcb", method="post",
                data={
                    "action": "recent_frames",
                    "source_alias": params.get("src", ""),
                    "source_alias_invert":
                        params.get("src_invert") in ("on", "1", "true"),
                    "frame_type": params.get("mti", ""),
                    "frame_type_invert":
                        params.get("mti_invert") in ("on", "1", "true"),
                    "text": params.get("text", ""),
                    "text_invert":
                        params.get("text_invert") in ("on", "1", "true"),
                },
            )
        except HTTPException:
            frames = []
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow(
            ["timestamp", "source_alias", "frame_type",
             "header", "summary", "data_hex"],
        )
        for f in frames:
            writer.writerow([
                f"{f['timestamp']:.3f}",
                f["source_alias"],
                f"{f['frame_type']:#x}",
                f["header"],
                f["summary"],
                f["data_hex"],
            ])
        # Filename with UTC date so an operator downloading several
        # captures in one session can tell them apart later.
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return Response(
            content=buf.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition":
                    f'attachment; filename="openlcb-frames-{stamp}.csv"',
            },
        )

    @app.post("/openlcb/monitor/clear", response_class=HTMLResponse)
    async def openlcb_monitor_clear(request: Request) -> Any:
        try:
            await _dispatch(
                handle, type="openlcb", method="post",
                data={"action": "clear_frames"},
            )
        except HTTPException:
            pass
        return await _openlcb_monitor_fragment(request)

    @app.post("/openlcb/search", response_class=HTMLResponse)
    async def openlcb_search(
        request: Request,
        address: int = Form(...),
        long_address: str = Form(""),
    ) -> Any:
        long_flag: bool | None
        if long_address == "1":
            long_flag = True
        elif long_address == "0":
            long_flag = False
        else:
            long_flag = None
        try:
            results = await _dispatch(
                handle, type="openlcb", method="post",
                data={
                    "action": "search",
                    "address": address,
                    "long_address": long_flag,
                    "timeout": 0.5,
                },
            )
            return templates.TemplateResponse(
                request, "_openlcb_search.html",
                {"search_results": results, "search_error": None},
            )
        except HTTPException as exc:
            return templates.TemplateResponse(
                request, "_openlcb_search.html",
                {"search_results": None, "search_error": exc.detail},
            )

    @app.post("/panel/upload", response_class=HTMLResponse)
    async def panel_upload(request: Request, file: UploadFile) -> Any:
        from pmri_core.persistence import load_panel_bytes  # noqa: PLC0415
        from pmri_web.panel_svg import parse_panel_xml, render_svg  # noqa: PLC0415

        data = await file.read()
        try:
            panel_file = load_panel_bytes(data)
        except Exception as e:  # noqa: BLE001
            return templates.TemplateResponse(
                request, "_panel_summary.html",
                {"error": f"failed to parse: {e}"},
                status_code=400,
            )
        model = parse_panel_xml(data)
        svg = render_svg(model, _panel_bean_state_for(handle.ctx))
        summary = {
            "filename": file.filename,
            "root_tag": panel_file.root_tag,
            "pmri_version": panel_file.pmri_version,
            "panels": [p.get("name") or "(unnamed)" for p in panel_file.panels()],
            "layout_editors": [
                le.get("name") or "(unnamed)" for le in panel_file.layout_editors()
            ],
            "top_level_child_count": len(list(panel_file.root)),
            "model": model,
            "svg": svg,
        }
        return templates.TemplateResponse(
            request, "_panel_summary.html", {"summary": summary},
        )

    @app.get("/panels/{name}", response_class=HTMLResponse)
    async def panels_detail(request: Request, name: str) -> Any:
        """Render an SVG view of ``<profile>/panels/<name>.xml``."""
        xml_bytes = _panel_xml_bytes(handle.ctx, name)
        if xml_bytes is None:
            raise HTTPException(404, f"no panel named {name!r}")
        from pmri_web.panel_svg import parse_panel_xml, render_svg  # noqa: PLC0415
        model = parse_panel_xml(xml_bytes)
        svg = render_svg(model, _panel_bean_state_for(handle.ctx))
        return templates.TemplateResponse(
            request, "panel_detail.html",
            _base_context(handle, "panel") | {
                "name": name, "model": model, "svg": svg,
            },
        )

    @app.get("/panels/{name}/svg")
    async def panels_svg(name: str) -> Any:
        """Raw inline SVG for the panel. Served as ``image/svg+xml`` so
        a browser can open it standalone or an ``<img>`` can embed it."""
        xml_bytes = _panel_xml_bytes(handle.ctx, name)
        if xml_bytes is None:
            raise HTTPException(404, f"no panel named {name!r}")
        from pmri_web.panel_svg import parse_panel_xml, render_svg  # noqa: PLC0415
        model = parse_panel_xml(xml_bytes)
        svg = render_svg(model, _panel_bean_state_for(handle.ctx))
        return Response(content=svg, media_type="image/svg+xml")
