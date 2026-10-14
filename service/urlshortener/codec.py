"""Short-code generation and validation."""

from __future__ import annotations

import re
import secrets
import string

ALPHABET = string.digits + string.ascii_letters  # base62
MIN_CODE_LENGTH = 4
_CODE = re.compile(r"[0-9A-Za-z_-]{3,32}")
_ALIAS = re.compile(r"[0-9A-Za-z][0-9A-Za-z_-]{2,31}")
# Path segments the application serves itself; an alias must never shadow them.
RESERVED_ALIASES = frozenset({"api", "docs", "redoc", "healthz", "readyz", "metrics", "openapi", "static", "admin"})


def generate_code(length: int = 7) -> str:
    """Return a random base62 code.

    Codes come from the OS CSPRNG, so they cannot be enumerated the way
    sequential ids can. Uniqueness is enforced by the database, not here.
    """
    if length < MIN_CODE_LENGTH:
        raise ValueError(f"code length must be at least {MIN_CODE_LENGTH}")
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def is_valid_code(code: str) -> bool:
    """Cheap syntactic check that lets us reject garbage without touching the database."""
    return _CODE.fullmatch(code) is not None


class InvalidAlias(ValueError):
    """The requested alias may not be used as a short code."""


def validate_alias(alias: str) -> str:
    """Return the alias unchanged if it is allowed, otherwise raise InvalidAlias."""
    if _ALIAS.fullmatch(alias) is None:
        raise InvalidAlias(
            "alias must be 3-32 characters (letters, digits, '-' or '_') and start with a letter or digit"
        )
    if alias.lower() in RESERVED_ALIASES:
        raise InvalidAlias(f"'{alias}' is a reserved name")
    return alias
