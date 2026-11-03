"""System-name parsing.

A JMRI system name has three parts:

  ``<connection-prefix><type-letter><system-address>``

For example, ``"LT12"`` = LocoNet (prefix ``L``) Turnout (type ``T``) at
address ``12``. Connection prefixes are user-configurable and can be multiple
characters: ``"L1T12"`` means a second LocoNet connection (prefix ``L1``).

The convention is that the *first* character or characters up to and
including the last digit-or-letter run is the connection prefix; the next
single letter is the type; everything after is the address (which may
itself contain colons, dots, slashes for hierarchical addressing -- e.g.
``"MS1:5:3"`` for an OpenLCB sensor).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Connection prefix: one letter, optionally followed by digits.
# Type letter: a single uppercase letter from the JMRI bean-type alphabet.
# Address: anything that follows. Empty addresses are not allowed.
_NAME_RE = re.compile(r"^(?P<prefix>[A-Z][A-Z0-9]*?)(?P<type>[A-Z])(?P<address>.+)$")

# Minimum length of the post-prefix substring: type letter + at least 1 address char.
_MIN_TYPE_PLUS_ADDR_LEN = 2


@dataclass(frozen=True, slots=True)
class SystemName:
    """Parsed system name."""

    prefix: str           # connection prefix, e.g. "L" or "L2"
    type_letter: str      # one-letter bean type, e.g. "T", "S", "H"
    address: str          # whatever follows; may be numeric or hierarchical

    def __str__(self) -> str:
        return f"{self.prefix}{self.type_letter}{self.address}"

    @classmethod
    def parse(cls, system_name: str, expected_prefix: str | None = None) -> SystemName:
        """Parse *system_name*. Optionally enforce *expected_prefix*.

        Raises :class:`ValueError` if the name doesn't match the JMRI shape or
        if a prefix is specified and doesn't match.
        """
        if expected_prefix is not None:
            if not system_name.startswith(expected_prefix):
                raise ValueError(
                    f"system name {system_name!r} does not start with expected "
                    f"prefix {expected_prefix!r}"
                )
            # Split: prefix + first-char-after = type letter; remainder = address.
            rest = system_name[len(expected_prefix):]
            if (
                len(rest) < _MIN_TYPE_PLUS_ADDR_LEN
                or not rest[0].isupper()
                or not rest[0].isalpha()
            ):
                raise ValueError(
                    f"system name {system_name!r} has no valid type letter after "
                    f"prefix {expected_prefix!r}"
                )
            return cls(prefix=expected_prefix, type_letter=rest[0], address=rest[1:])

        m = _NAME_RE.match(system_name)
        if not m:
            raise ValueError(f"system name {system_name!r} is not in <prefix><T><address> form")
        return cls(prefix=m["prefix"], type_letter=m["type"], address=m["address"])

    @classmethod
    def make(cls, prefix: str, type_letter: str, address: str | int) -> str:
        """Convenience: format a system name from parts.

        Returns the string form, not a ``SystemName`` -- most callers want the
        flat name for storage and pass it through :meth:`parse` again later.
        """
        if len(type_letter) != 1 or not type_letter.isupper() or not type_letter.isalpha():
            raise ValueError(f"type_letter must be a single uppercase letter, got {type_letter!r}")
        return f"{prefix}{type_letter}{address}"
