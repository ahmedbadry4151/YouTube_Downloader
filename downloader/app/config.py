from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Runtime configuration. Every field can be overridden with ``TT_<NAME>``."""

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_prefix="TT_",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- server ---------------------------------------------------------
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "info"
    api_key: str = ""
    cors_origins: str = ""

    # --- storage --------------------------------------------------------
    download_dir: Path = PROJECT_ROOT / "downloads"
    data_dir: Path = PROJECT_ROOT / "data"

    # --- performance ----------------------------------------------------
    max_concurrent_downloads: int = Field(default=4, ge=1, le=32)
    fragments_per_download: int = Field(default=16, ge=1, le=64)
    http_chunk_size_mb: int = Field(default=10, ge=1, le=64)
    use_aria2: bool = True
    aria2_connections: int = Field(default=16, ge=1, le=32)
    retries: int = Field(default=3, ge=0, le=20)
    socket_timeout: int = Field(default=20, ge=5, le=120)

    # --- behaviour ------------------------------------------------------
    probe_cache_ttl: int = Field(default=180, ge=0, le=3600)
    probe_max_entries: int = Field(default=1000, ge=1, le=10000)
    max_history: int = Field(default=200, ge=1, le=5000)
    enable_open_folder: bool = True
    auto_resume: bool = False
    allowed_hosts: str = ""
    allow_private_hosts: bool = False

    @field_validator("log_level")
    @classmethod
    def _valid_log_level(cls, value: str) -> str:
        value = value.lower().strip()
        return value if value in {"debug", "info", "warning", "error", "critical"} else "info"

    @property
    def download_path(self) -> Path:
        return self._resolve_path(self.download_dir)

    @property
    def data_path(self) -> Path:
        return self._resolve_path(self.data_dir)

    @property
    def cookies_path(self) -> Path:
        return self.data_path / "cookies"

    @property
    def allowed_host_list(self) -> list[str]:
        return [h.strip().lower() for h in self.allowed_hosts.split(",") if h.strip()]

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @staticmethod
    def _resolve_path(path: Path) -> Path:
        path = path.expanduser()
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        return path.resolve()

    def prepare_dirs(self) -> None:
        for directory in (self.download_path, self.data_path, self.cookies_path, self.data_path / "logs"):
            directory.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.prepare_dirs()
    return settings
