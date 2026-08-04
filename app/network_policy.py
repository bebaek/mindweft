from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit


def validate_public_https_url(raw_url: str) -> None:
    """Reject non-HTTPS URLs and hosts resolving to non-public address space."""
    parsed = urlsplit(raw_url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("must use an absolute HTTPS URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("must not contain embedded credentials")
    host = parsed.hostname.strip("[]").lower()
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        raise ValueError("cannot access local or private network hosts")
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise ValueError(f"host '{host}' could not be resolved") from exc
        addresses = []
        for info in infos:
            sockaddr = info[4]
            if not sockaddr:
                continue
            try:
                addresses.append(ipaddress.ip_address(sockaddr[0]))
            except ValueError:
                continue
    if not addresses or any(not _is_public_address(address) for address in addresses):
        raise ValueError("cannot access local or private network hosts")


def _is_public_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
        or address.is_reserved
    )
