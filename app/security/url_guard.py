"""SSRF guardrails for scrape target and proxy URLs."""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlparse

_NAT64_WELL_KNOWN_PREFIX = ipaddress.ip_network("64:ff9b::/96")


@dataclass(frozen=True, slots=True)
class ValidationResult:
    is_allowed: bool
    status_code: int = 200
    error_message: str | None = None


class UrlGuard:
    """Deep module enforcing network security and SSRF prevention guardrails."""

    @classmethod
    def is_blocked_ip(cls, ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
        if isinstance(ip, ipaddress.IPv6Address) and ip in _NAT64_WELL_KNOWN_PREFIX:
            try:
                embedded_ipv4 = ipaddress.IPv4Address(ip.packed[-4:])
                return cls.is_blocked_ip(embedded_ipv4)
            except ValueError:
                return True

        return (
            ip.is_loopback
            or ip.is_private
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        )

    @classmethod
    def validate(cls, raw_url: str) -> ValidationResult:
        parsed = urlparse(raw_url)
        if parsed.scheme not in {"http", "https"}:
            return ValidationResult(
                is_allowed=False,
                status_code=400,
                error_message="Only http/https URLs are allowed",
            )

        host = parsed.hostname
        if not host:
            return ValidationResult(
                is_allowed=False,
                status_code=400,
                error_message="URL must include a hostname",
            )

        normalized_host = host.strip().lower().rstrip(".")
        if normalized_host == "localhost" or normalized_host.endswith(".localhost"):
            return ValidationResult(
                is_allowed=False,
                status_code=403,
                error_message="Target hostname is blocked",
            )

        try:
            addr_infos = socket.getaddrinfo(host, parsed.port, type=socket.SOCK_STREAM)
        except socket.gaierror:
            return ValidationResult(
                is_allowed=False,
                status_code=400,
                error_message="Hostname could not be resolved",
            )

        resolved_ips: set[str] = set()
        for info in addr_infos:
            sockaddr = info[4]
            if sockaddr:
                resolved_ips.add(sockaddr[0])

        for ip_text in resolved_ips:
            try:
                ip = ipaddress.ip_address(ip_text)
            except ValueError:
                continue
            if cls.is_blocked_ip(ip):
                return ValidationResult(
                    is_allowed=False,
                    status_code=403,
                    error_message=f"Target resolved to blocked IP address: {ip_text}",
                )

        return ValidationResult(is_allowed=True, status_code=200, error_message=None)

    @classmethod
    def validate_proxy(cls, proxy_url: str) -> ValidationResult:
        result = cls.validate(proxy_url)
        if not result.is_allowed:
            return ValidationResult(
                is_allowed=False,
                status_code=result.status_code,
                error_message=f"Proxy URL is invalid or blocked: {result.error_message}",
            )
        return result
