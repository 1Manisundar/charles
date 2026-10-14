"""Short-code generation and validation."""

from __future__ import annotations

import re
import secrets
import string

ALPHABET = string.digits + string.ascii_letters  # base62
MIN_CODE_LENGTH = 4
_CODE = re.compile(r"[0-9A-Za-z]{4,32}")


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
