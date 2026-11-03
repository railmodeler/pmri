"""Password hashing via bcrypt.

Isolated into its own module so callers that only need models or
YAML I/O don't pull the ``bcrypt`` dependency transitively (matters
for lean CLI / test builds).

Uses bcrypt with the library default work factor (12 rounds today).
That takes ~200ms per verify on a Raspberry Pi 4, which is fine for
the throughput a hobby-scale server sees and provides adequate
resistance to offline attacks.

The wrapper functions accept ``str`` on the public API and handle
UTF-8 encoding internally -- every real-world password store deals
with strings, and putting the bytes conversion at the boundary
keeps the callers clean.
"""
from __future__ import annotations

import bcrypt


def hash_password(password: str) -> str:
    """Return a bcrypt hash of *password* suitable for storage.

    The returned string embeds the salt + work factor + hash, so
    :func:`verify_password` doesn't need any external state to
    validate. Idempotent-safe: repeated calls with the same
    password produce different hashes (different salts) -- verify
    handles both.
    """
    if not password:
        raise ValueError("password must be non-empty")
    salted = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt())
    return salted.decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """Constant-time check of *password* against *password_hash*.

    Returns ``False`` for an empty ``password_hash`` -- a user with
    no hash on file has no valid password, and short-circuiting to
    ``False`` avoids leaking that state via bcrypt error messages.
    """
    if not password_hash:
        return False
    try:
        return bcrypt.checkpw(
            password.encode("utf-8"),
            password_hash.encode("utf-8"),
        )
    except (ValueError, TypeError):
        # Malformed hash -- treat as no match rather than crashing.
        # Real users see this after a hand-edit of users.yaml gone
        # wrong; the auth log will show why they can't log in.
        return False
