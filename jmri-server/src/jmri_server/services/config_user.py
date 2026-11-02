"""``ConfigUserService`` -- user CRUD + self-service password/token.

Wire shape (rewrite-specific extension -- JMRI Java doesn't have
this topic since its Swing UI edits users through a native dialog)::

    method=list                            -> array of {username, role, has_token, self}
    method=get, data={"username": "..."}    -> one user projection
    method=put,  data={"username": "...",
                       "password": "...",
                       "role": "viewer|operator|admin"}
        -> creates or updates. On existing user, updates only the
          fields you passed. Admin-only.

    method=delete, data={"username": "..."} -> remove user. Admin only.
                                             Refuses self-delete + last-admin.

    method=post, data={"action": "change_password",
                       "username": "...",
                       "new_password": "..."}
        -> change password. Self only for non-admins; admins can
          change anyone's.

    method=post, data={"action": "regenerate_token",
                       "username": "..."}
        -> mint new API token, returns {"token": "..."}. Self only
          for non-admins; admins can regenerate anyone's.

**Never returns the password_hash** in list/get/put -- that would
leak the bcrypt salt across the wire even if hashed, and there's
no legitimate client-side use for it. Callers change passwords
via the ``change_password`` action, not by reading the current
hash and writing a modified one.
"""
from __future__ import annotations

import logging
from typing import Any

from jmri_server.auth.models import Role, User
from jmri_server.service import JsonService, JsonServiceError

_log = logging.getLogger(__name__)


def _project(user: User, *, self_flag: bool = False) -> dict[str, Any]:
    """Wire projection of a User. Excludes password_hash and the
    token itself; only reports whether one exists. The token is
    returned only on regenerate."""
    return {
        "username": user.username,
        "role": user.role.value,
        "has_token": bool(user.token),
        "self": self_flag,
    }


class ConfigUserService(JsonService):
    topic = "configUser"

    # User CRUD is admin-only for read + mutate. The self-service
    # POST actions do their own finer-grained checks (a viewer can
    # change their own password, not anyone else's) so ``post``
    # is gated at operator -- the action handler enforces the rest.
    REQUIRED_ROLE = {
        "list": Role.ADMIN,
        "get": Role.ADMIN,
        "put": Role.ADMIN,
        "delete": Role.ADMIN,
        "post": Role.VIEWER,   # self-service always available
    }

    # ---- helpers -------------------------------------------------------

    def _auth(self):
        auth = getattr(self.ctx, "auth", None)
        if auth is None:
            raise JsonServiceError(
                503,
                "auth is disabled on this server (JMRI_AUTH_ENABLED not set)",
            )
        return auth

    def _current_username(self) -> str | None:
        return getattr(self.ctx, "_current_username", None)

    def _current_role(self) -> Role:
        """The current role is passed through dispatch to the JsonService
        signature, not to instances -- but we can infer from the
        current user. When auth is off the middleware sets role=admin;
        we mirror that here by treating "no user" as admin."""
        current = self._current_username()
        if current is None:
            return Role.ADMIN
        user = self._auth().by_username(current)
        return user.role if user else Role.VIEWER

    # ---- CRUD ----------------------------------------------------------

    async def on_list(
        self, data: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        auth = self._auth()
        current = self._current_username()
        return [
            _project(u, self_flag=(u.username == current))
            for u in auth.users
        ]

    async def on_get(
        self, data: dict[str, Any] | None,
    ) -> dict[str, Any]:
        data = data or {}
        username = data.get("username")
        if not isinstance(username, str) or not username:
            raise JsonServiceError(400, "configUser.get requires 'username'")
        auth = self._auth()
        user = auth.by_username(username)
        if user is None:
            raise JsonServiceError(404, f"no user {username!r}")
        return _project(
            user, self_flag=(user.username == self._current_username()),
        )

    async def on_put(
        self, data: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Create or update.

        For a new user: ``password`` is required, ``role`` defaults
        to viewer. For an existing user: only the fields supplied
        are updated (role and/or password). Changing your own role
        would trigger the self-demotion guard -- see ``set_role``.
        """
        data = data or {}
        username = data.get("username")
        password = data.get("password")
        role_raw = data.get("role")
        if not isinstance(username, str) or not username:
            raise JsonServiceError(400, "configUser.put requires 'username'")
        role: Role | None = None
        if role_raw is not None:
            try:
                role = Role(role_raw)
            except ValueError:
                raise JsonServiceError(
                    400,
                    f"invalid role {role_raw!r}; must be one of "
                    f"{[r.value for r in Role]}",
                ) from None

        auth = self._auth()
        current = self._current_username() or ""
        existing = auth.by_username(username)
        if existing is None:
            if not isinstance(password, str) or not password:
                raise JsonServiceError(
                    400, "creating a user requires 'password'",
                )
            user = auth.add_user(
                username, password,
                role=role if role is not None else Role.VIEWER,
            )
            return _project(user, self_flag=(username == current))

        # Existing user -- update only what's supplied.
        if isinstance(password, str) and password:
            auth.set_password(username, password)
        if role is not None and role is not existing.role:
            try:
                auth.set_role(
                    username, role, current_username=current,
                )
            except ValueError as e:
                raise JsonServiceError(409, str(e)) from None
        return _project(
            auth.by_username(username),
            self_flag=(username == current),
        )

    async def on_delete(
        self, data: dict[str, Any] | None,
    ) -> dict[str, Any]:
        data = data or {}
        username = data.get("username")
        if not isinstance(username, str) or not username:
            raise JsonServiceError(400, "configUser.delete requires 'username'")
        auth = self._auth()
        current = self._current_username() or ""
        try:
            auth.remove_user(username, current_username=current)
        except LookupError as e:
            raise JsonServiceError(404, str(e)) from None
        except ValueError as e:
            raise JsonServiceError(409, str(e)) from None
        return {"username": username, "deleted": True}

    # ---- actions -------------------------------------------------------

    async def on_post(
        self, data: dict[str, Any] | None,
    ) -> dict[str, Any]:
        data = data or {}
        action = data.get("action")
        if action == "change_password":
            return await self._change_password(data)
        if action == "regenerate_token":
            return await self._regenerate_token(data)
        raise JsonServiceError(
            400, f"unknown configUser action: {action!r}",
        )

    async def _change_password(
        self, data: dict[str, Any],
    ) -> dict[str, Any]:
        username = data.get("username")
        new_password = data.get("new_password")
        if not isinstance(username, str) or not username:
            raise JsonServiceError(400, "change_password requires 'username'")
        if not isinstance(new_password, str) or not new_password:
            raise JsonServiceError(400, "change_password requires 'new_password'")
        current = self._current_username() or ""
        if username != current and self._current_role() is not Role.ADMIN:
            raise JsonServiceError(
                403,
                "only admins can change other users' passwords",
            )
        auth = self._auth()
        try:
            auth.set_password(username, new_password)
        except LookupError as e:
            raise JsonServiceError(404, str(e)) from None
        return {"username": username, "changed": True}

    async def _regenerate_token(
        self, data: dict[str, Any],
    ) -> dict[str, Any]:
        username = data.get("username")
        if not isinstance(username, str) or not username:
            raise JsonServiceError(
                400, "regenerate_token requires 'username'",
            )
        current = self._current_username() or ""
        if username != current and self._current_role() is not Role.ADMIN:
            raise JsonServiceError(
                403,
                "only admins can regenerate other users' tokens",
            )
        auth = self._auth()
        try:
            token = auth.regenerate_token(username)
        except LookupError as e:
            raise JsonServiceError(404, str(e)) from None
        return {"username": username, "token": token}
