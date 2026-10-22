from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    config = Settings(
        download_dir=tmp_path / "downloads",
        data_dir=tmp_path / "data",
        max_concurrent_downloads=2,
        fragments_per_download=4,
        retries=0,
        probe_cache_ttl=0,
        use_aria2=False,
    )
    config.prepare_dirs()
    return config


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    """Keep URL validation deterministic and offline."""
    from app import security

    monkeypatch.setattr(security, "_resolve", lambda host: ["93.184.216.34"])


@pytest.fixture()
def manager(settings):
    from app.events import EventBus
    from app.manager import JobManager
    from app.store import Store

    store = Store(settings.data_path / "turbotube.db")
    instance = JobManager(settings, store, EventBus())
    try:
        yield instance
    finally:
        instance.shutdown()
        store.close()
