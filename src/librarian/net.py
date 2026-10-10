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
from urllib.parse import urlsplit


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
