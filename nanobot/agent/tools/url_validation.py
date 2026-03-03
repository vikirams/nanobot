"""Shared URL validation for SSRF protection: reject loopback, private, and link-local IPs."""

import ipaddress
import socket
from urllib.parse import urlparse


def _is_private_or_reserved(ip_str: str) -> bool:
    """Return True if the IP is loopback, private, link-local, or cloud metadata range."""
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # Treat invalid as blocked
    if addr.is_loopback:
        return True
    if addr.is_private:
        return True
    if addr.is_link_local:
        return True
    # Cloud metadata (e.g. AWS 169.254.169.254, GCP/Azure similar)
    if ip_str == "169.254.169.254":
        return True
    # Reserved / "this" network
    if addr.is_reserved:
        return True
    return False


def validate_not_private(url: str) -> tuple[bool, str]:
    """
    Validate that the URL's host does not resolve to loopback, private, link-local,
    or cloud metadata IPs. Returns (True, "") if allowed, (False, "reason") if blocked.
    """
    try:
        parsed = urlparse(url)
        host = (parsed.netloc or "").split(":")[0].strip()
        if not host:
            return False, "Missing host"
        # Resolve hostname to IPs
        try:
            infos = socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        except socket.gaierror as e:
            return False, f"Cannot resolve host: {e}"
        for (_family, _type, _proto, _canonname, sockaddr) in infos:
            ip_str = sockaddr[0]
            if _is_private_or_reserved(ip_str):
                return False, f"URL must not target private or internal IP (resolved to {ip_str})"
        return True, ""
    except Exception as e:
        return False, str(e)
