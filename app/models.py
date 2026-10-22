from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

Quality = Literal["best", "2160", "1440", "1080", "720", "480", "360", "worst"]
Container = Literal["mp4", "mkv", "webm", "original"]
AudioFormat = Literal["mp3", "m4a", "opus", "aac", "flac", "wav", "original"]
SponsorMode = Literal["off", "mark", "remove"]
PlaylistMode = Literal["auto", "video", "playlist"]
JobStatus = Literal[
    "queued", "running", "processing", "completed", "failed", "cancelled", "partial", "interrupted"
]


class DownloadOptions(BaseModel):
    """Everything the user can tune, shared by the Web UI and the CLI."""

    mode: PlaylistMode = "auto"
    quality: Quality = "best"
    container: Container = "mp4"

    audio_only: bool = False
    audio_format: AudioFormat = "mp3"
    audio_quality: str = "0"

    playlist_items: str = ""
    playlist_reverse: bool = False

    subtitles: bool = False
    auto_subtitles: bool = False
    subtitle_langs: str = "en"
    embed_subtitles: bool = True

    embed_thumbnail: bool = True
    embed_metadata: bool = True
    chapters: bool = False
    sponsorblock: SponsorMode = "off"

    cookies_file: str = ""
    cookies_from_browser: str = ""
    proxy: str = ""
    rate_limit: str = ""
    section: str = ""

    archive: bool = False
    use_aria2: bool | None = None
    fragments: int | None = Field(default=None, ge=1, le=64)
    retries: int | None = Field(default=None, ge=0, le=20)
    output_template: str = ""

    @field_validator("playlist_items", "subtitle_langs", "cookies_file", "proxy", "rate_limit", "section", "output_template")
    @classmethod
    def _strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("cookies_from_browser")
    @classmethod
    def _valid_browser(cls, value: str) -> str:
        value = value.strip().lower()
        allowed = {"", "chrome", "chromium", "edge", "firefox", "brave", "opera", "vivaldi", "safari", "whale"}
        if value and value not in allowed:
            raise ValueError(f"Unsupported browser '{value}'")
        return value

    @field_validator("output_template")
    @classmethod
    def _safe_template(cls, value: str) -> str:
        value = value.strip()
        if not value:
            return ""
        if len(value) > 180:
            raise ValueError("Output template is too long (max 180 chars)")
        if any(ch in value for ch in ("/", "\\", "..", "\x00")):
            raise ValueError("Output template must not contain path separators or '..'")
        return value


class SelectedEntry(BaseModel):
    """A single video chosen by the user after analysing a playlist."""

    url: str
    title: str = ""
    index: int | None = Field(default=None, ge=1)
    duration: float | None = None


class ProbeRequest(BaseModel):
    url: str
    mode: PlaylistMode = "auto"
    cookies_file: str = ""
    cookies_from_browser: str = ""
    proxy: str = ""


class JobCreateRequest(BaseModel):
    url: str
    options: DownloadOptions = Field(default_factory=DownloadOptions)
    entries: list[SelectedEntry] | None = None
    title: str = ""

    @field_validator("entries")
    @classmethod
    def _limit_entries(cls, value: list[SelectedEntry] | None) -> list[SelectedEntry] | None:
        if value is not None and len(value) > 5000:
            raise ValueError("Too many entries selected (max 5000)")
        return value


class FormatQuality(BaseModel):
    height: int
    fps: float | None = None
    label: str
    estimated_bytes: int | None = None


class AudioQuality(BaseModel):
    abr: float | None = None
    ext: str = "m4a"
    label: str = ""


class MediaEntry(BaseModel):
    index: int
    id: str = ""
    title: str = ""
    url: str
    duration: float | None = None
    thumbnail: str = ""


class ProbeResult(BaseModel):
    kind: Literal["video", "playlist"]
    url: str
    title: str = ""
    id: str = ""
    uploader: str = ""
    duration: float | None = None
    thumbnail: str = ""
    description: str = ""
    live_status: str = ""
    entry_count: int = 0
    entries: list[MediaEntry] = Field(default_factory=list)
    qualities: list[FormatQuality] = Field(default_factory=list)
    audio_qualities: list[AudioQuality] = Field(default_factory=list)
    truncated: bool = False
