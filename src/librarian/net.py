"""Network guards against server-side request forgery (SSRF).

Onboarding fetches URLs supplied by other agents, and provisional entries are
created from those specs, so by default neither may reach private, loopback,
link-local (cloud metadata) or other non-public addresses. Human-verified
entries may point at internal services.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit


class BlockedAddress(Exception):
    pass


def _is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


async def resolve_public(host: str) -> list[str]:
    """Resolve `host` and require every address to be public."""
    if not host:
        raise BlockedAddress("missing host")
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise BlockedAddress(f"cannot resolve {host!r}: {exc}") from exc
    addresses = sorted({str(info[4][0]) for info in infos})
    blocked = [a for a in addresses if not _is_public(a)]
    if blocked:
        raise BlockedAddress(f"{host!r} resolves to non-public address(es) {', '.join(blocked)}")
    return addresses


async def check_url(url: str, allow_private: bool) -> None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise BlockedAddress(f"unsupported URL scheme {parts.scheme!r}")
    if not allow_private:
        await resolve_public(parts.hostname or "")


@dataclass(frozen=True)
class PinnedTarget:
    """A request target bound to an address that already passed the public-IP check.

    Connecting to the vetted IP (instead of resolving the name again) closes the
    DNS-rebinding gap between check and connect. TLS still verifies the
    certificate against the real hostname via the `sni_hostname` extension.
    """

    url: str
    host_header: str
    extensions: dict[str, Any]


async def pin(url: str) -> PinnedTarget:
    """Resolve `url`'s host, require a public address, and target that address."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    address = (await resolve_public(host))[0]
    literal = f"[{address}]" if ":" in address else address
    netloc = literal + (f":{parts.port}" if parts.port else "")
    pinned = urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    host_header = host + (f":{parts.port}" if parts.port else "")
    extensions: dict[str, Any] = {"sni_hostname": host} if parts.scheme == "https" else {}
    return PinnedTarget(pinned, host_header, extensions)
