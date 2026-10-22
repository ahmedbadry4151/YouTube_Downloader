from __future__ import annotations

import ipaddress
import socket
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

_PRIVATE_NETWORKS = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.0.0.0/24"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("240.0.0.0/4"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]

_DNS_CACHE: dict[str, tuple[float, list[str]]] = {}
_DNS_LOCK = threading.Lock()
_DNS_TTL = 300.0


class UnsafeURLError(ValueError):
    """The URL is malformed or points somewhere the server must not reach."""


def _is_blocked_ip(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return True
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    return any(ip in network for network in _PRIVATE_NETWORKS)


def _resolve(host: str) -> list[str]:
    now = time.monotonic()
    with _DNS_LOCK:
        cached = _DNS_CACHE.get(host)
        if cached and cached[0] > now:
            return cached[1]
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafeURLError(f"Could not resolve host '{host}'") from exc
    addresses = sorted({info[4][0] for info in infos})
    with _DNS_LOCK:
        if len(_DNS_CACHE) > 512:
            _DNS_CACHE.clear()
        _DNS_CACHE[host] = (now + _DNS_TTL, addresses)
    return addresses


def validate_media_url(
    url: str,
    allowed_hosts: list[str] | None = None,
    allow_private: bool = False,
) -> str:
    """Validate an http(s) media URL and block SSRF targets.

    Returns the cleaned URL. Raises :class:`UnsafeURLError` otherwise.
    """
    if not url or not isinstance(url, str):
        raise UnsafeURLError("URL is required")
    cleaned = url.strip()
    if len(cleaned) > 4096:
        raise UnsafeURLError("URL is too long")
    parts = urlsplit(cleaned)
    if parts.scheme.lower() not in {"http", "https"}:
        raise UnsafeURLError("Only http:// and https:// URLs are supported")
    if not parts.hostname:
        raise UnsafeURLError("URL has no host")
    if parts.username or parts.password:
        raise UnsafeURLError("URLs with embedded credentials are not allowed")

    host = parts.hostname.lower().rstrip(".")
    if not host:
        raise UnsafeURLError("URL has no host")
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        if not allow_private:
            raise UnsafeURLError("Local addresses are blocked")

    if allowed_hosts:
        if not any(host == allowed or host.endswith(f".{allowed}") for allowed in allowed_hosts):
            raise UnsafeURLError(f"Host '{host}' is not in TT_ALLOWED_HOSTS")

    if not allow_private:
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None:
            if _is_blocked_ip(str(literal)):
                raise UnsafeURLError("Private or reserved IP addresses are blocked")
        else:
            for address in _resolve(host):
                if _is_blocked_ip(address):
                    raise UnsafeURLError("Host resolves to a private or reserved address")

    return cleaned


def validate_proxy(proxy: str | None) -> str:
    if not proxy:
        return ""
    text = proxy.strip()
    if not text:
        return ""
    parts = urlsplit(text)
    if parts.scheme.lower() not in {"http", "https", "socks4", "socks5", "socks5h"}:
        raise UnsafeURLError("Proxy must use http(s) or socks4/5")
    if not parts.hostname:
        raise UnsafeURLError("Proxy URL has no host")
    return text


def resolve_cookie_file(value: str | None, cookies_dir: Path, base_dir: Path) -> str | None:
    """Resolve a cookie setting to an absolute path.

    Accepts an uploaded cookie id/filename (looked up in ``data/cookies``) or a
    local absolute/relative path provided by the operator.
    """
    if not value:
        return None
    text = value.strip()
    if not text:
        return None

    candidates: list[Path] = []
    raw = Path(text).expanduser()
    if raw.is_absolute():
        candidates.append(raw)
    else:
        candidates.append(cookies_dir / text)
        if not text.endswith(".txt"):
            candidates.append(cookies_dir / f"{text}.txt")
        candidates.append(base_dir / text)

    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved.is_file():
            return str(resolved)
    raise UnsafeURLError("Cookie file not found")


def ensure_within(base: Path, target: Path) -> Path:
    resolved = target.resolve()
    if not resolved.is_relative_to(base.resolve()):
        raise UnsafeURLError("Path escapes the download directory")
    return resolved
