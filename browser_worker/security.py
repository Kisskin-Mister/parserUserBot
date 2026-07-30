from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from typing import Protocol, Sequence
from urllib.parse import SplitResult, urlsplit, urlunsplit


class URLValidationError(ValueError):
    pass


class HostResolver(Protocol):
    async def resolve(self, hostname: str, port: int) -> Sequence[str]: ...


class SocketHostResolver:
    async def resolve(self, hostname: str, port: int) -> Sequence[str]:
        def lookup() -> list[str]:
            records = socket.getaddrinfo(
                hostname, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
            )
            return sorted({record[4][0] for record in records})

        try:
            return await asyncio.to_thread(lookup)
        except socket.gaierror as exc:
            raise URLValidationError(f"hostname cannot be resolved: {hostname}") from exc


@dataclass(frozen=True)
class ValidatedURL:
    url: str
    hostname: str
    addresses: tuple[str, ...]


class URLGuard:
    """Reject unsafe navigation targets before a browser sees them.

    The real Playwright runtime also applies this guard to every request, which
    protects redirects and subresource requests from DNS-to-private targets.
    """

    def __init__(
        self,
        *,
        allowed_domains: Sequence[str] = (),
        resolver: HostResolver | None = None,
        allowed_ports: Sequence[int] = (80, 443),
    ):
        self.allowed_domains = tuple(
            self._normalise_domain(item) for item in allowed_domains if item.strip()
        )
        self.resolver = resolver or SocketHostResolver()
        self.allowed_ports = frozenset(int(port) for port in allowed_ports)

    @staticmethod
    def _normalise_domain(value: str) -> str:
        domain = value.strip().lower().rstrip(".")
        if domain.startswith("*."):
            domain = domain[2:]
        if not domain or "/" in domain or ":" in domain:
            raise ValueError(f"invalid allowlisted domain: {value!r}")
        return domain.encode("idna").decode("ascii")

    def _domain_allowed(self, hostname: str) -> bool:
        if not self.allowed_domains:
            return True
        return any(
            hostname == domain or hostname.endswith(f".{domain}")
            for domain in self.allowed_domains
        )

    async def validate(
        self, url: str, *, enforce_domain_allowlist: bool = True
    ) -> ValidatedURL:
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError as exc:
            raise URLValidationError(f"invalid URL: {exc}") from exc
        if parsed.scheme.lower() not in {"http", "https"}:
            raise URLValidationError("only http/https URLs are permitted")
        if parsed.username is not None or parsed.password is not None:
            raise URLValidationError("credentials in URL are forbidden")
        if not parsed.hostname:
            raise URLValidationError("URL hostname is required")
        try:
            hostname = parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise URLValidationError("invalid internationalized hostname") from exc
        if hostname == "localhost" or hostname.endswith(
            (".localhost", ".local", ".internal")
        ):
            raise URLValidationError("local hostnames are forbidden")
        if enforce_domain_allowlist and not self._domain_allowed(hostname):
            raise URLValidationError("hostname is not in BROWSER_ALLOWED_DOMAINS")

        effective_port = port or (443 if parsed.scheme.lower() == "https" else 80)
        if effective_port not in self.allowed_ports:
            raise URLValidationError("URL port is not permitted")

        literal: ipaddress.IPv4Address | ipaddress.IPv6Address | None
        try:
            literal = ipaddress.ip_address(hostname)
        except ValueError:
            literal = None
        addresses = [str(literal)] if literal is not None else list(
            await self.resolver.resolve(hostname, effective_port)
        )
        if not addresses:
            raise URLValidationError("hostname resolved to no addresses")
        for address in addresses:
            try:
                ip = ipaddress.ip_address(address)
            except ValueError as exc:
                raise URLValidationError("resolver returned an invalid address") from exc
            if not ip.is_global:
                raise URLValidationError(
                    f"non-public address is forbidden for {hostname}"
                )

        safe_netloc = hostname
        if ":" in hostname:
            safe_netloc = f"[{hostname}]"
        if port is not None:
            safe_netloc = f"{safe_netloc}:{port}"
        safe = SplitResult(
            parsed.scheme.lower(), safe_netloc, parsed.path or "/", parsed.query, ""
        )
        return ValidatedURL(url=urlunsplit(safe), hostname=hostname, addresses=tuple(addresses))
