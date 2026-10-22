from __future__ import annotations

import copy
import os
import re
import shutil
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadCancelled, download_range_func

from .config import Settings
from .models import (
    AudioQuality,
    DownloadOptions,
    FormatQuality,
    MediaEntry,
    ProbeResult,
)
from .security import UnsafeURLError, validate_media_url
from .utils import (
    normalize_outtmpl,
    parse_langs,
    parse_rate_limit,
    parse_section,
)

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_MEDIA_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".mp3", ".m4a", ".opus", ".aac", ".flac", ".wav", ".ogg"}
_EMBEDDABLE_AUDIO = {"mp3", "m4a", "opus", "aac"}
_ARIA2_HINT_CACHE: dict[str, Any] = {"path": None, "checked": 0.0}
_ARIA2_LOCK = threading.Lock()
_JS_RUNTIME_CACHE: dict[str, Any] = {"value": None, "checked": 0.0}
_JS_RUNTIME_LOCK = threading.Lock()

_PROBE_CACHE: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
_PROBE_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# capabilities
# ---------------------------------------------------------------------------
def aria2_path() -> str | None:
    with _ARIA2_LOCK:
        now = time.monotonic()
        if now - _ARIA2_HINT_CACHE["checked"] < 60 and _ARIA2_HINT_CACHE["checked"]:
            return _ARIA2_HINT_CACHE["path"]
        path = shutil.which("aria2c")
        _ARIA2_HINT_CACHE.update(path=path, checked=now)
        return path


def js_runtime() -> dict[str, dict[str, str]]:
    """Return the best available yt-dlp JavaScript runtime.

    YouTube increasingly requires solving a JS challenge before releasing
    media URLs. yt-dlp only enables Deno by default, so Node.js and Bun users
    would otherwise get HTTP 403 on many videos. The result is cached briefly
    because this is consulted for every probe and download.
    """
    with _JS_RUNTIME_LOCK:
        now = time.monotonic()
        if _JS_RUNTIME_CACHE["checked"] and now - _JS_RUNTIME_CACHE["checked"] < 60:
            return _JS_RUNTIME_CACHE["value"]
        value: dict[str, dict[str, str]] = {}
        for name in ("deno", "node", "bun", "quickjs"):
            path = shutil.which(name)
            if path:
                value = {name: {"path": path}}
                break
        # An explicit empty mapping means "no runtime available"; omitting the
        # key would silently fall back to Deno and spam warnings.
        _JS_RUNTIME_CACHE.update(value=value, checked=now)
        return value


def ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg")


def ytdlp_version() -> str:
    from yt_dlp.version import __version__

    return __version__


# ---------------------------------------------------------------------------
# format selection
# ---------------------------------------------------------------------------
def build_format_selector(options: DownloadOptions) -> str:
    if options.audio_only:
        return "bestaudio/best"

    height = None if options.quality in {"best", "worst"} else int(options.quality)
    cap = f"[height<={height}]" if height else ""

    if options.quality == "worst":
        return f"wv*{cap}+wa/w{cap}/w"

    # Prefer separate streams over progressive formats such as itag 18:
    # YouTube increasingly rejects direct progressive downloads with HTTP 403,
    # while the DASH video/audio pairs remain available.
    if options.container == "mp4":
        return f"bv*{cap}[ext=mp4]+ba[ext=m4a]/bv*{cap}+ba/b{cap}/b"
    if options.container == "webm":
        return f"bv*{cap}[ext=webm]+ba[ext=webm]/bv*{cap}+ba/b{cap}/b"
    return f"bv*{cap}+ba/b{cap}/b"


def _format_sort(options: DownloadOptions) -> list[str] | None:
    if options.audio_only or options.quality == "worst":
        return None
    # Only resolution and frame rate. Codec preferences such as
    # ``vcodec:h264`` can make yt-dlp fall back to a progressive format like
    # itag 18 instead of a higher-quality DASH pair, and those progressive
    # URLs are the ones YouTube most often rejects with HTTP 403.
    return ["res", "fps"]


def _postprocessors(options: DownloadOptions) -> list[dict[str, Any]]:
    pps: list[dict[str, Any]] = []

    if options.sponsorblock != "off":
        categories = ["sponsor", "selfpromo", "interaction", "intro", "outro", "preview", "music_offtopic"]
        pps.append(
            {
                "key": "SponsorBlock",
                "categories": categories,
                "when": "after_filter",
            }
        )
        pps.append(
            {
                "key": "ModifyChapters",
                "remove_sponsor_segments": categories if options.sponsorblock == "remove" else [],
                "sponsorblock_chapter_title": "[SponsorBlock] %(category_names)l",
                "force_keyframes": options.sponsorblock == "remove",
            }
        )

    if options.audio_only and options.audio_format != "original":
        pps.append(
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": options.audio_format,
                "preferredquality": options.audio_quality or "0",
            }
        )

    if not options.audio_only and options.container != "original":
        pps.append({"key": "FFmpegVideoRemuxer", "preferedformat": options.container})

    embed_thumb = options.embed_thumbnail and (
        not options.audio_only or options.audio_format in _EMBEDDABLE_AUDIO
    )
    if embed_thumb:
        pps.append({"key": "EmbedThumbnail", "already_have_thumbnail": True})

    if options.embed_subtitles and not options.audio_only and (
        options.subtitles or options.auto_subtitles
    ):
        pps.append({"key": "FFmpegEmbedSubtitle", "already_have_subtitle": True})

    if options.embed_metadata or options.chapters:
        pps.append(
            {
                "key": "FFmpegMetadata",
                "add_metadata": options.embed_metadata,
                "add_chapters": options.chapters and not options.audio_only,
                "add_infojson": False,
            }
        )

    return pps


# ---------------------------------------------------------------------------
# yt-dlp option builder
# ---------------------------------------------------------------------------
def _base_params(settings: Settings) -> dict[str, Any]:
    params: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "no_color": True,
        "js_runtimes": js_runtime(),
        # YouTube's default android_vr client increasingly hands out media
        # URLs that return HTTP 403 for some videos/regions. The embedded web
        # client is more reliable and exposes the same quality ladder; keep
        # the Android clients as fallbacks for videos that block embedding.
        "extractor_args": {
            "youtube": {"player_client": ["web_embedded", "android_vr", "android"]}
        },
        "cachedir": str(settings.data_path / "yt-dlp-cache"),
        "socket_timeout": settings.socket_timeout,
        "retries": settings.retries,
        "fragment_retries": settings.retries,
        "extractor_retries": 2,
        "windowsfilenames": os.name == "nt",
        "trim_file_name": 160,
        "paths": {"home": str(settings.download_path)},
        "nopart": False,
        "continuedl": True,
        "overwrites": False,
        "noplaylist": True,
    }
    return params


def build_download_params(
    options: DownloadOptions,
    settings: Settings,
    *,
    cookies_file: str | None = None,
) -> dict[str, Any]:
    params = _base_params(settings)
    retries = settings.retries if options.retries is None else options.retries
    fragments = options.fragments or settings.fragments_per_download
    use_aria2 = settings.use_aria2 if options.use_aria2 is None else options.use_aria2
    aria2 = aria2_path() if use_aria2 else None

    # Section (time-range) downloads use yt-dlp's own ffmpeg range downloader
    # to cut precisely; aria2c cannot improve byte-range cuts and conflicts
    # with it, so fall back to native transport for section downloads.
    if options.section:
        aria2 = None

    params.update(
        {
            "format": build_format_selector(options),
            "outtmpl": {"default": normalize_outtmpl(options.output_template)},
            "retries": retries,
            "fragment_retries": retries,
            "concurrent_fragment_downloads": fragments,
            "http_chunk_size": settings.http_chunk_size_mb * 1024 * 1024,
            "writethumbnail": options.embed_thumbnail
            and (not options.audio_only or options.audio_format in _EMBEDDABLE_AUDIO),
            "postprocessors": _postprocessors(options),
            "prefer_ffmpeg": True,
            "keepvideo": False,
        }
    )

    sort = _format_sort(options)
    if sort:
        params["format_sort"] = sort

    if not options.audio_only and options.container != "original":
        params["merge_output_format"] = options.container

    if options.subtitles or options.auto_subtitles:
        params.update(
            {
                "writesubtitles": True,
                "writeautomaticsub": options.auto_subtitles,
                "subtitleslangs": parse_langs(options.subtitle_langs),
                "subtitlesformat": "srt/best",
            }
        )

    if aria2:
        params["external_downloader"] = {"default": aria2}
        params["external_downloader_args"] = {
            "default": [
                "--no-conf",
                f"-x{settings.aria2_connections}",
                f"-s{settings.aria2_connections}",
                "-k1M",
                "-j1",
                "--min-split-size=1M",
                "--file-allocation=none",
                "--console-log-level=warn",
                "--summary-interval=0",
                "--download-result=hide",
                "--connect-timeout=15",
                "--timeout=45",
                "--max-tries=5",
                "--retry-wait=1",
            ]
        }

    if options.rate_limit:
        limit = parse_rate_limit(options.rate_limit)
        if limit:
            params["ratelimit"] = limit

    if options.proxy:
        params["proxy"] = options.proxy

    if options.cookies_file:
        params["cookiefile"] = options.cookies_file
    elif options.cookies_from_browser:
        params["cookiesfrombrowser"] = (options.cookies_from_browser, None, None, None)

    if options.archive:
        params["download_archive"] = str(settings.data_path / "archive.txt")

    section = parse_section(options.section)
    if section:
        start, end = section
        params["download_ranges"] = download_range_func(None, [(start, end)])
        params["force_keyframes_at_cuts"] = True

    return params


# ---------------------------------------------------------------------------
# logging bridge
# ---------------------------------------------------------------------------
class YdlLogger:
    """Routes yt-dlp messages to a user callback (kept intentionally quiet)."""

    def __init__(self, sink: Callable[[str], None] | None = None) -> None:
        self._sink = sink

    def _send(self, message: str) -> None:
        if self._sink:
            clean = _ANSI.sub("", str(message)).strip()
            if clean:
                self._sink(clean)

    def debug(self, msg: str) -> None:
        if not msg.startswith("[debug]"):
            return
        self._send(msg)

    def info(self, msg: str) -> None:
        self._send(msg)

    def warning(self, msg: str) -> None:
        self._send(f"warning: {msg}")

    def error(self, msg: str) -> None:
        self._send(f"error: {msg}")


def friendly_error(message: str) -> str:
    text = _ANSI.sub("", str(message)).strip()
    lowered = text.lower()
    hints = {
        "sign in to confirm": "YouTube requires a signed-in session — add a cookies file (Advanced → Cookies) and retry.",
        "not a bot": "YouTube flagged this IP — export browser cookies in Advanced options and retry.",
        "private video": "This video is private and cannot be downloaded.",
        "members-only": "This video is members-only; cookies from a member account are required.",
        "video unavailable": "The video is unavailable in your region or was removed.",
        "aria2c exited with code 22": (
            "aria2c could not fetch the media (usually HTTP 403/404). TurboTube retried; "
            "if it persists, refresh yt-dlp, add browser cookies, or disable aria2c."
        ),
        "http error 403": "The media URL expired or was rejected — retry; if it persists, add cookies.",
        "http error 429": "YouTube rate-limited this IP — lower concurrency or retry later.",
        "requested format is not available": "That quality is not available for this video — pick another quality.",
        "unsupported url": "This URL is not supported by the extractor.",
    }
    for marker, hint in hints.items():
        if marker in lowered:
            return f"{text[:400]} — {hint}"
    return text[:600] or "Unknown download error"


# ---------------------------------------------------------------------------
# probing
# ---------------------------------------------------------------------------
def _entry_url(entry: dict[str, Any]) -> str:
    for key in ("webpage_url", "url", "original_url"):
        value = entry.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return value
    video_id = entry.get("id")
    if video_id:
        return f"https://www.youtube.com/watch?v={video_id}"
    return ""


def _thumbnail(info: dict[str, Any]) -> str:
    thumb = info.get("thumbnail")
    if isinstance(thumb, str) and thumb:
        return thumb
    thumbs = info.get("thumbnails") or []
    valid = [t for t in thumbs if isinstance(t, dict) and t.get("url")]
    if valid:
        valid.sort(key=lambda t: (t.get("preference") or 0, t.get("width") or 0))
        return valid[-1]["url"]
    return ""


def _summarize_formats(formats: list[dict[str, Any]]) -> tuple[list[FormatQuality], list[AudioQuality]]:
    videos = [
        f
        for f in formats
        if f.get("vcodec") not in (None, "none") and isinstance(f.get("height"), int)
    ]
    audios = [f for f in formats if f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none")]
    best_audio = max(
        ((f.get("filesize") or f.get("filesize_approx") or 0) for f in audios),
        default=0,
    )

    by_height: dict[int, dict[str, float]] = {}
    for fmt in videos:
        height = int(fmt["height"])
        slot = by_height.setdefault(height, {"fps": 0.0, "size": 0.0})
        slot["fps"] = max(slot["fps"], float(fmt.get("fps") or 0))
        slot["size"] = max(slot["size"], float(fmt.get("filesize") or fmt.get("filesize_approx") or 0))

    qualities: list[FormatQuality] = []
    for height in sorted(by_height, reverse=True):
        slot = by_height[height]
        fps = slot["fps"]
        label = f"{height}p{int(fps)}" if fps >= 50 else f"{height}p"
        estimate = int(slot["size"] + best_audio) if slot["size"] else None
        qualities.append(FormatQuality(height=height, fps=fps or None, label=label, estimated_bytes=estimate))

    audio_qualities: list[AudioQuality] = []
    seen: set[tuple[float, str]] = set()
    for fmt in sorted(audios, key=lambda f: f.get("abr") or 0, reverse=True):
        abr = fmt.get("abr")
        ext = fmt.get("ext") or "m4a"
        key = (round(float(abr or 0), 1), ext)
        if key in seen or len(audio_qualities) >= 8:
            continue
        seen.add(key)
        label = f"{int(abr)} kbps · {ext}" if abr else f"audio · {ext}"
        audio_qualities.append(AudioQuality(abr=float(abr) if abr else None, ext=ext, label=label))

    return qualities, audio_qualities


def _probe_params(
    settings: Settings,
    *,
    mode: str,
    cookies_file: str | None,
    cookies_from_browser: str,
    proxy: str,
) -> dict[str, Any]:
    params = _base_params(settings)
    flat = mode != "video"
    params.update(
        {
            "skip_download": True,
            "extract_flat": "in_playlist" if flat else False,
            "playlistend": settings.probe_max_entries + 1,
            "noplaylist": mode == "video",
            "ignoreerrors": mode != "video",
            "writethumbnail": False,
            "writesubtitles": False,
        }
    )
    if cookies_file:
        params["cookiefile"] = cookies_file
    elif cookies_from_browser:
        params["cookiesfrombrowser"] = (cookies_from_browser, None, None, None)
    if proxy:
        params["proxy"] = proxy
    return params


def probe_media(
    url: str,
    settings: Settings,
    *,
    mode: str = "auto",
    cookies_file: str | None = None,
    cookies_from_browser: str = "",
    proxy: str = "",
) -> dict[str, Any]:
    cache_key = f"{url}|{mode}|{cookies_file}|{cookies_from_browser}|{proxy}"
    now = time.monotonic()
    if settings.probe_cache_ttl:
        with _PROBE_LOCK:
            cached = _PROBE_CACHE.get(cache_key)
            if cached and cached[0] > now:
                _PROBE_CACHE.move_to_end(cache_key)
                return copy.deepcopy(cached[1])

    params = _probe_params(
        settings,
        mode=mode,
        cookies_file=cookies_file,
        cookies_from_browser=cookies_from_browser,
        proxy=proxy,
    )
    with YoutubeDL(params) as ydl:
        info = ydl.extract_info(url, download=False)

    if not info:
        raise ValueError("No media information returned for this URL")

    if info.get("_type") == "playlist":
        if mode == "video":
            raise ValueError("This URL is a playlist, not a single video")
        raw_entries = [entry for entry in (info.get("entries") or []) if entry]
        truncated = len(raw_entries) > settings.probe_max_entries
        raw_entries = raw_entries[: settings.probe_max_entries]
        entries: list[MediaEntry] = []
        for position, entry in enumerate(raw_entries, start=1):
            entry_url = _entry_url(entry)
            if not entry_url:
                continue
            entries.append(
                MediaEntry(
                    index=position,
                    id=str(entry.get("id") or ""),
                    title=str(entry.get("title") or f"Video {position}"),
                    url=entry_url,
                    duration=float(entry["duration"]) if entry.get("duration") else None,
                    thumbnail=_thumbnail(entry),
                )
            )
        if mode == "playlist" and not entries:
            raise ValueError("No playable entries found in this playlist")
        if not entries:
            raise ValueError("This playlist is empty or unavailable")
        result = ProbeResult(
            kind="playlist",
            url=info.get("webpage_url") or url,
            title=str(info.get("title") or "Playlist"),
            id=str(info.get("id") or ""),
            uploader=str(info.get("uploader") or info.get("channel") or ""),
            duration=None,
            thumbnail=_thumbnail(info) or (entries[0].thumbnail if entries else ""),
            description=(info.get("description") or "")[:600],
            entry_count=int(info.get("playlist_count") or len(raw_entries)),
            entries=entries,
            truncated=truncated,
        )
    else:
        if mode == "playlist":
            raise ValueError("This URL is a single video, not a playlist")
        formats = [f for f in (info.get("formats") or []) if isinstance(f, dict)]
        qualities, audio_qualities = _summarize_formats(formats)
        result = ProbeResult(
            kind="video",
            url=info.get("webpage_url") or url,
            title=str(info.get("title") or "Video"),
            id=str(info.get("id") or ""),
            uploader=str(info.get("uploader") or info.get("channel") or ""),
            duration=float(info["duration"]) if info.get("duration") else None,
            thumbnail=_thumbnail(info),
            description=(info.get("description") or "")[:600],
            live_status=str(info.get("live_status") or ""),
            entry_count=1,
            qualities=qualities,
            audio_qualities=audio_qualities,
        )

    payload = result.model_dump()
    if settings.probe_cache_ttl:
        with _PROBE_LOCK:
            _PROBE_CACHE[cache_key] = (now + settings.probe_cache_ttl, payload)
            if len(_PROBE_CACHE) > 64:
                _PROBE_CACHE.popitem(last=False)
    return copy.deepcopy(payload)


def expand_playlist(
    url: str,
    options: DownloadOptions,
    settings: Settings,
    *,
    cookies_file: str | None = None,
) -> list[dict[str, Any]]:
    """Return the selected videos of a playlist as ``{url,title,index,duration}``."""
    probe = probe_media(
        url,
        settings,
        mode=options.mode,
        cookies_file=cookies_file,
        cookies_from_browser=options.cookies_from_browser,
        proxy=options.proxy,
    )
    if probe["kind"] != "playlist":
        return [
            {
                "url": validate_media_url(
                    probe["url"], settings.allowed_host_list, settings.allow_private_hosts
                ),
                "title": probe["title"],
                "index": 1,
                "duration": probe.get("duration"),
            }
        ]

    entries = probe["entries"]
    if options.playlist_reverse:
        entries = list(reversed(entries))
    selected = parse_item_spec(options.playlist_items, total=len(entries))
    if selected is not None:
        wanted = set(selected)
        entries = [entry for entry in entries if entry["index"] in wanted]
    if not entries:
        raise ValueError("The selection did not match any playlist item")
    cleaned: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in entries:
        entry_url = validate_media_url(
            entry["url"], settings.allowed_host_list, settings.allow_private_hosts
        )
        if entry_url in seen:
            continue
        seen.add(entry_url)
        cleaned.append(
            {
                "url": entry_url,
                "title": entry["title"],
                "index": entry.get("index"),
                "duration": entry.get("duration"),
            }
        )
    return cleaned


# ---------------------------------------------------------------------------
# downloading
# ---------------------------------------------------------------------------
def _hook_progress(
    data: dict[str, Any],
    cancel_event: threading.Event | None,
    callback: Callable[[dict[str, Any]], None] | None,
) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise DownloadCancelled("Cancelled by user")
    if callback:
        callback(data)


def _pp_hook(
    data: dict[str, Any],
    cancel_event: threading.Event | None,
    callback: Callable[[dict[str, Any]], None] | None,
) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise DownloadCancelled("Cancelled by user")
    if callback:
        callback(data)


def run_download(
    url: str,
    options: DownloadOptions,
    settings: Settings,
    *,
    progress_hook: Callable[[dict[str, Any]], None] | None = None,
    pp_hook: Callable[[dict[str, Any]], None] | None = None,
    log_sink: Callable[[str], None] | None = None,
    cancel_event: threading.Event | None = None,
    cookies_file: str | None = None,
) -> dict[str, Any]:
    params = build_download_params(options, settings, cookies_file=cookies_file)
    params["progress_hooks"] = [
        lambda data: _hook_progress(data, cancel_event, progress_hook)
    ]
    params["postprocessor_hooks"] = [lambda data: _pp_hook(data, cancel_event, pp_hook)]
    params["logger"] = YdlLogger(log_sink)

    with YoutubeDL(params) as ydl:
        info = ydl.extract_info(url, download=True)
    if not isinstance(info, dict):
        raise RuntimeError("yt-dlp did not return media information")
    return info


def extract_output_path(info: dict[str, Any], download_dir: Path) -> Path | None:
    candidates: list[str] = []
    for value in (info.get("filepath"), info.get("_filename")):
        if isinstance(value, str) and value:
            candidates.append(value)
    for requested in info.get("requested_downloads") or []:
        if isinstance(requested, dict) and requested.get("filepath"):
            candidates.append(requested["filepath"])

    for candidate in candidates:
        path = Path(candidate)
        if not path.is_absolute():
            path = download_dir / path
        if path.is_file() and path.suffix.lower() in _MEDIA_EXTENSIONS:
            try:
                return safety_check(download_dir, path)
            except UnsafeURLError:
                continue

    video_id = str(info.get("id") or "")
    if video_id:
        matches = [
            p
            for p in download_dir.iterdir()
            if p.is_file() and f"[{video_id}]" in p.name and p.suffix.lower() in _MEDIA_EXTENSIONS
        ]
        if matches:
            matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            return matches[0]
    return None


def safety_check(download_dir: Path, path: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(download_dir.resolve()):
        raise UnsafeURLError("Output file escaped the download directory")
    return resolved
