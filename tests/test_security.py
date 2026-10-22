from __future__ import annotations

import pytest

from app import security
from app.security import (
    UnsafeURLError,
    ensure_within,
    resolve_cookie_file,
    validate_media_url,
    validate_proxy,
)


def test_rejects_bad_schemes_and_hosts():
    for url in ["ftp://example.com/a", "file:///etc/passwd", "javascript:alert(1)", "not-a-url", ""]:
        with pytest.raises(UnsafeURLError):
            validate_media_url(url)
    with pytest.raises(UnsafeURLError):
        validate_media_url("https://user:pass@example.com/video")
    with pytest.raises(UnsafeURLError):
        validate_media_url("http://localhost:8000/secret")
    with pytest.raises(UnsafeURLError):
        validate_media_url("http://127.0.0.1/secret")
    with pytest.raises(UnsafeURLError):
        validate_media_url("http://[::1]/secret")


def test_private_dns_is_blocked(monkeypatch):
    monkeypatch.setattr(security, "_resolve", lambda host: ["10.0.0.5"])
    with pytest.raises(UnsafeURLError):
        validate_media_url("https://internal.example.com/video")
    assert validate_media_url("https://internal.example.com/video", allow_private=True)


def test_allowlist():
    allowed = ["youtube.com", "youtu.be"]
    assert validate_media_url("https://www.youtube.com/watch?v=x", allowed) == "https://www.youtube.com/watch?v=x"
    assert validate_media_url("https://youtu.be/x", allowed)
    with pytest.raises(UnsafeURLError):
        validate_media_url("https://vimeo.com/123", allowed)
    # suffix must be a real subdomain boundary
    with pytest.raises(UnsafeURLError):
        validate_media_url("https://notyoutube.com/watch?v=x", allowed)


def test_proxy_validation():
    assert validate_proxy("") == ""
    assert validate_proxy("socks5://127.0.0.1:1080") == "socks5://127.0.0.1:1080"
    with pytest.raises(UnsafeURLError):
        validate_proxy("ftp://127.0.0.1")


def test_cookie_resolution(settings, tmp_path):
    path = settings.cookies_path / "abc.txt"
    path.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    assert resolve_cookie_file("abc", settings.cookies_path, tmp_path) == str(path.resolve())
    assert resolve_cookie_file("abc.txt", settings.cookies_path, tmp_path) == str(path.resolve())
    assert resolve_cookie_file(str(path), settings.cookies_path, tmp_path) == str(path.resolve())
    assert resolve_cookie_file("", settings.cookies_path, tmp_path) is None
    with pytest.raises(UnsafeURLError):
        resolve_cookie_file("missing.txt", settings.cookies_path, tmp_path)


def test_ensure_within(tmp_path):
    base = tmp_path / "downloads"
    base.mkdir()
    inner = base / "a" / "b.mp4"
    inner.parent.mkdir()
    inner.write_bytes(b"x")
    assert ensure_within(base, inner) == inner.resolve()
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"x")
    with pytest.raises(UnsafeURLError):
        ensure_within(base, outside)
