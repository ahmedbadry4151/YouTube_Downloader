from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app import manager as manager_module
from app.main import create_app

VIDEO = "https://www.youtube.com/watch?v=abc123"


def _fake_run(settings):
    def fake(url, options, settings_arg, **kwargs):
        hook = kwargs.get("progress_hook")
        if hook:
            hook({"status": "downloading", "downloaded_bytes": 10, "total_bytes": 20, "speed": 100,
                  "info_dict": {"format_id": "v"}})
            hook({"status": "finished", "downloaded_bytes": 20, "total_bytes": 20,
                  "info_dict": {"format_id": "v"}})
        path = settings.download_path / "abc123.mp4"
        path.write_bytes(b"bytes")
        return {"id": "abc123", "filepath": str(path)}

    return fake


def test_health_system_and_static(settings):
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/health").json() == {"status": "ok"}
        info = client.get("/api/system").json()
        assert info["app"] == "TurboTube"
        assert info["download_dir"] == str(settings.download_path)
        assert info["auth_required"] is False

        index = client.get("/")
        assert index.status_code == 200
        assert "TurboTube" in index.text
        assert client.get("/app.js").status_code == 200
        assert client.get("/styles.css").status_code == 200
        assert client.get("/favicon.svg").status_code == 200
        assert "Content-Security-Policy" in index.headers


def test_api_key_protection(settings):
    settings.api_key = "s3cret"
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/system").status_code == 401
        assert client.get("/api/system", headers={"x-api-key": "wrong"}).status_code == 401
        assert client.get("/api/system", headers={"x-api-key": "s3cret"}).status_code == 200
        assert client.get("/api/system?key=s3cret").status_code == 200
        assert client.get("/").status_code == 200  # UI shell is public


def test_probe_rejects_unsafe_url(settings):
    with TestClient(create_app(settings)) as client:
        for url in ["file:///etc/passwd", "http://127.0.0.1/secret", "ftp://example.com/x"]:
            response = client.post("/api/probe", json={"url": url})
            assert response.status_code == 400, response.text


def test_probe_success(settings, monkeypatch):
    payload = {
        "kind": "video",
        "url": VIDEO,
        "title": "Demo",
        "id": "abc123",
        "uploader": "Tester",
        "duration": 60,
        "thumbnail": "",
        "description": "",
        "live_status": "",
        "entry_count": 1,
        "entries": [],
        "qualities": [{"height": 1080, "label": "1080p", "fps": 60}],
        "audio_qualities": [],
        "truncated": False,
    }
    monkeypatch.setattr("app.api.probe_media", lambda *args, **kwargs: payload)
    with TestClient(create_app(settings)) as client:
        response = client.post("/api/probe", json={"url": VIDEO})
        assert response.status_code == 200
        assert response.json()["title"] == "Demo"


def test_create_job_end_to_end(settings, monkeypatch):
    monkeypatch.setattr(
        manager_module,
        "expand_playlist",
        lambda *args, **kwargs: [
            {"url": VIDEO, "title": "Demo", "index": 1, "duration": 60}
        ],
    )
    monkeypatch.setattr(manager_module, "run_download", _fake_run(settings))

    with TestClient(create_app(settings)) as client:
        response = client.post("/api/jobs", json={"url": VIDEO, "options": {"quality": "720"}})
        assert response.status_code == 201, response.text
        job = response.json()
        assert job["total_items"] == 1
        assert job["kind"] == "video"

        state = job
        for _ in range(200):
            state = client.get(f"/api/jobs/{job['id']}").json()
            if state["status"] in {"completed", "failed"}:
                break
            time.sleep(0.03)
        assert state["status"] == "completed", state
        assert state["items"][0]["filepath"].endswith("abc123.mp4")

        listing = client.get("/api/jobs").json()
        assert listing["jobs"]
        assert listing["counts"]["total"] == 1


def test_job_validation_errors(settings):
    with TestClient(create_app(settings)) as client:
        assert client.post("/api/jobs", json={"url": "file:///etc/passwd"}).status_code == 400
        assert client.get("/api/jobs/nope").status_code == 404
        assert client.post("/api/jobs/nope/cancel").status_code == 404
        assert client.delete("/api/jobs/nope").status_code == 404


def test_cookie_upload_and_listing(settings):
    payload = b"# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tabc\n"
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/cookies",
            files={"file": ("cookies.txt", payload, "text/plain")},
        )
        assert response.status_code == 201, response.text
        cookie_id = response.json()["id"]
        assert cookie_id.endswith(".txt")
        assert (settings.cookies_path / cookie_id).is_file()

        listing = client.get("/api/cookies").json()
        assert any(item["id"] == cookie_id for item in listing)

        bad = client.post("/api/cookies", files={"file": ("x.txt", b"hello", "text/plain")})
        assert bad.status_code == 400


def test_missing_cookie_file_rejected(settings, monkeypatch):
    monkeypatch.setattr(
        manager_module,
        "expand_playlist",
        lambda *args, **kwargs: [{"url": VIDEO, "title": "X", "index": 1, "duration": 1}],
    )
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/jobs",
            json={"url": VIDEO, "options": {"cookies_file": "does-not-exist.txt"}},
        )
        assert response.status_code == 400
