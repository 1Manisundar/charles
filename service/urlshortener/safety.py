"""Destination vetting: refuse URLs that are unsafe to send visitors to.

These are syntactic checks on the URL as submitted. The service never fetches
a destination, so this is abuse hygiene rather than SSRF protection, and it
does not resolve DNS: a public name that points at a private address passes.
"""

from __future__ import annotations

import ipaddress
from typing import Iterable
from urllib.parse import urlsplit

_INTERNAL_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa")


class UnsafeUrl(ValueError):
    """The URL is well-formed but must not be shortened."""


def check_destination(url: str, own_host: str | None = None, blocked_domains: Iterable[str] = ()) -> None:
    """Raise UnsafeUrl if ``url`` points somewhere a short link must not lead."""
    parts = urlsplit(url)
    host = (parts.hostname or "").rstrip(".").lower()

    # https://bank.example@evil.example/ shows a trusted name but goes elsewhere.
    if parts.username is not None or parts.password is not None:
        raise UnsafeUrl("URLs with embedded credentials are not allowed")
    if own_host and host == own_host.lower():
        raise UnsafeUrl("a short link may not point back at this service")
    if host == "localhost" or host.endswith(_INTERNAL_SUFFIXES):
        raise UnsafeUrl("the destination is an internal host name")

    # Browsers accept many spellings of an IP address (2130706433, 0177.0.0.1,
    # 0x7f.1). Anything that looks numeric must parse as a plain public address.
    if all(label[:1].isdigit() for label in host.split(".")) or ":" in host:
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            raise UnsafeUrl("the destination is an ambiguous numeric host") from None
        if not address.is_global:
            raise UnsafeUrl("the destination is not a public address")

    for domain in blocked_domains:
        if host == domain or host.endswith("." + domain):
            raise UnsafeUrl("the destination domain is blocked")
