"""Network guards against server-side request forgery (SSRF).

Onboarding fetches URLs supplied by other agents, and provisional entries are
created from those specs, so by default neither may reach private, loopback,
link-local (cloud metadata) or other non-public addresses. Human-verified
entries may point at internal services.

`NetworkPolicy` says what else may be reached: either everything
(`allow_private`, for a laptop) or the private addresses behind hosts that match
`internal_hosts` patterns such as `*.internal.example.com`. An internal host may
still never resolve to loopback or link-local (cloud metadata) addresses.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Any
from urllib.parse import urlsplit, urlunsplit


class BlockedAddress(Exception):
    pass


def _normalize(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> Any:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def _is_public(address: str) -> bool:
    ip = _normalize(ipaddress.ip_address(address))
    return bool(ip.is_global and not ip.is_multicast)


def _is_internal_ok(address: str) -> bool:
    """Public or private, but never loopback, link-local (169.254.169.254), multicast,
    unspecified or reserved, even for a host the operator listed."""
    ip = _normalize(ipaddress.ip_address(address))
    return not (
        ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved
    )


def host_pattern_ok(pattern: str) -> bool:
    """A hostname glob: letters, digits, dots, dashes and `*`; not a bare wildcard."""
    p = pattern.strip().lower().rstrip(".")
    return bool(p) and p.strip("*.") != "" and all(c.isalnum() or c in "*.-" for c in p)


@dataclass(frozen=True)
class NetworkPolicy:
    """Which addresses onboarding and calls to provisional entries may reach."""

    allow_private: bool = False  # everything, with no address checks (laptop use)
    internal_hosts: tuple[str, ...] = ()  # hosts whose private addresses are allowed

    def internal_pattern(self, host: str) -> str | None:
        """The `internal_hosts` pattern that matches `host`, if any."""
        name = host.lower().rstrip(".")
        for pattern in self.internal_hosts:
            if fnmatchcase(name, pattern.lower().rstrip(".")):
                return pattern
        return None

    def allows(self, host: str, address: str) -> bool:
        if self.allow_private:
            return True
        if self.internal_pattern(host) is not None:
            return _is_internal_ok(address)
        return _is_public(address)


PUBLIC = NetworkPolicy()


async def resolve(host: str, net: NetworkPolicy = PUBLIC) -> list[str]:
    """Resolve `host` and require every address to be allowed for it."""
    if not host:
        raise BlockedAddress("missing host")
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise BlockedAddress(f"cannot resolve {host!r}: {exc}") from exc
    addresses = sorted({str(info[4][0]) for info in infos})
    blocked = [a for a in addresses if not net.allows(host, a)]
    if blocked:
        kind = "disallowed" if net.internal_pattern(host) else "non-public"
        raise BlockedAddress(f"{host!r} resolves to {kind} address(es) {', '.join(blocked)}")
    return addresses


async def check_url(url: str, net: NetworkPolicy = PUBLIC) -> None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise BlockedAddress(f"unsupported URL scheme {parts.scheme!r}")
    if not net.allow_private:
        await resolve(parts.hostname or "", net)


@dataclass(frozen=True)
class PinnedTarget:
    """A request target bound to an address that already passed the address check.

    Connecting to the vetted IP (instead of resolving the name again) closes the
    DNS-rebinding gap between check and connect. TLS still verifies the
    certificate against the real hostname via the `sni_hostname` extension.
    """

    url: str
    host_header: str
    extensions: dict[str, Any]


async def pin(url: str, net: NetworkPolicy = PUBLIC) -> PinnedTarget:
    """Resolve `url`'s host, require an allowed address, and target that address."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    address = (await resolve(host, net))[0]
    literal = f"[{address}]" if ":" in address else address
    netloc = literal + (f":{parts.port}" if parts.port else "")
    pinned = urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    host_header = host + (f":{parts.port}" if parts.port else "")
    extensions: dict[str, Any] = {"sni_hostname": host} if parts.scheme == "https" else {}
    return PinnedTarget(pinned, host_header, extensions)
