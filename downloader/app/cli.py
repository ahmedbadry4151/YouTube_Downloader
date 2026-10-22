from __future__ import annotations

import argparse
import io
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.markup import escape
from rich.progress import (
    BarColumn,
    Progress,
    TaskID,
    TaskProgressColumn,
    TextColumn,
    TimeRemainingColumn,
)
from rich.table import Table

from . import __version__
from .config import PROJECT_ROOT, Settings, get_settings
from .models import DownloadOptions
from .security import UnsafeURLError, resolve_cookie_file, validate_media_url, validate_proxy
from .utils import (
    SelectionError,
    human_bytes,
    human_duration,
)
from .youtube import (
    aria2_path,
    extract_output_path,
    expand_playlist,
    ffmpeg_path,
    friendly_error,
    js_runtime,
    probe_media,
    run_download,
    ytdlp_version,
)

console = Console(highlight=False)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
class ProgressTracker:
    """Aggregates per-stream progress into one bar per video."""

    def __init__(self, progress: Progress, task: TaskID) -> None:
        self.progress = progress
        self.task = task
        self.streams: dict[str, tuple[int, int | None]] = {}
        self.last = 0.0

    def hook(self, data: dict[str, Any]) -> None:
        status = data.get("status")
        if status not in {"downloading", "finished"}:
            return
        now = time.monotonic()
        info = data.get("info_dict") or {}
        key = str(info.get("format_id") or data.get("filename") or "stream")
        total = data.get("total_bytes") or data.get("total_bytes_estimate")
        downloaded = data.get("downloaded_bytes")
        if status == "finished":
            downloaded = total or downloaded or self.streams.get(key, (0, None))[0]
            total = total or downloaded
        self.streams[key] = (
            int(downloaded or 0),
            int(total) if total else None,
        )

        totals = [item[1] for item in self.streams.values()]
        downloaded_total = sum(item[0] for item in self.streams.values())
        if all(totals):
            percent = downloaded_total / sum(totals) * 100  # type: ignore[arg-type]
        else:
            percents = [
                (item[0] / item[1] * 100) if item[1] else 0.0 for item in self.streams.values()
            ]
            percent = sum(percents) / len(percents) if percents else 0.0

        speed = float(data.get("speed") or 0)
        eta = data.get("eta")
        status_text = ""
        if speed:
            status_text = f"{human_bytes(speed)}/s"
        if eta:
            status_text += f" - ETA {human_duration(eta)}"

        if now - self.last >= 0.2 or status == "finished":
            self.last = now
            self.progress.update(
                self.task,
                completed=min(99.9, percent),
                status=status_text,
            )


def _build_settings(args: argparse.Namespace) -> Settings:
    overrides: dict[str, Any] = {}
    if getattr(args, "output_dir", None):
        overrides["download_dir"] = Path(args.output_dir).expanduser()
    if getattr(args, "workers", None):
        overrides["max_concurrent_downloads"] = max(1, min(32, args.workers))
    if getattr(args, "fragments", None):
        overrides["fragments_per_download"] = max(1, min(64, args.fragments))
    if getattr(args, "no_aria2", False):
        overrides["use_aria2"] = False
    settings = Settings(**overrides)
    settings.prepare_dirs()
    return settings


def _options_from_args(args: argparse.Namespace) -> DownloadOptions:
    audio_format = args.audio if args.audio else "mp3"
    return DownloadOptions(
        mode="video" if args.video else ("playlist" if args.playlist else "auto"),
        quality=args.quality,
        container=args.container,
        audio_only=bool(args.audio),
        audio_format=audio_format,
        playlist_items=args.items or "",
        playlist_reverse=args.reverse,
        subtitles=args.subtitles,
        auto_subtitles=args.auto_subs,
        subtitle_langs=args.sub_langs,
        embed_subtitles=not args.no_embed_subs,
        embed_thumbnail=not args.no_thumbnail,
        embed_metadata=not args.no_metadata,
        chapters=args.chapters,
        sponsorblock=args.sponsorblock,
        cookies_file=args.cookies or "",
        cookies_from_browser=args.cookies_from_browser or "",
        proxy=args.proxy or "",
        rate_limit=args.limit_rate or "",
        section=args.section or "",
        archive=args.archive,
        use_aria2=not args.no_aria2,
        fragments=args.fragments,
    )


def _resolve_cookie(args: argparse.Namespace, settings: Settings) -> str | None:
    if not getattr(args, "cookies", None):
        return None
    return resolve_cookie_file(args.cookies, settings.cookies_path, PROJECT_ROOT)


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    settings = get_settings()
    host = args.host or settings.host
    port = args.port or settings.port
    console.print(f"[bold cyan]TurboTube[/] {__version__} -> http://{host}:{port}")
    if args.reload:
        uvicorn.run(
            "app.main:app",
            host=host,
            port=port,
            reload=True,
            log_level=settings.log_level,
        )
    else:
        uvicorn.run(
            "app.main:app",
            host=host,
            port=port,
            log_level=settings.log_level,
        )
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    settings = _build_settings(args)
    try:
        url = validate_media_url(args.url, settings.allowed_host_list, settings.allow_private_hosts)
        proxy = validate_proxy(args.proxy) if args.proxy else ""
        cookies = _resolve_cookie(args, settings)
        probe = probe_media(
            url,
            settings,
            mode="video" if args.video else ("playlist" if args.playlist else "auto"),
            cookies_file=cookies,
            cookies_from_browser=args.cookies_from_browser or "",
            proxy=proxy,
        )
    except (UnsafeURLError, ValueError) as exc:
        console.print(f"[red]Error:[/] {escape(str(exc))}")
        return 2

    if args.json:
        console.print_json(json.dumps(probe, ensure_ascii=False))
        return 0

    console.print(f"[bold]{escape(probe['title'])}[/]")
    console.print(f"  type      : {probe['kind']}")
    console.print(f"  uploader  : {escape(probe['uploader'] or '-')}")
    if probe.get("duration"):
        console.print(f"  duration  : {human_duration(probe['duration'])}")
    if probe["kind"] == "playlist":
        console.print(f"  videos    : {probe['entry_count']} ({len(probe['entries'])} listed)")
        table = Table(show_header=True, header_style="bold")
        table.add_column("#", justify="right", width=5)
        table.add_column("Title", overflow="fold")
        table.add_column("Duration", justify="right", width=10)
        for entry in probe["entries"][:25]:
            table.add_row(
                str(entry["index"]),
                escape(entry["title"]),
                human_duration(entry.get("duration")) or "-",
            )
        console.print(table)
        if len(probe["entries"]) > 25:
            console.print(f"  ... and {len(probe['entries']) - 25} more")
    else:
        qualities = ", ".join(q["label"] for q in probe["qualities"][:12]) or "unknown"
        console.print(f"  qualities : {qualities}")
        if probe.get("live_status"):
            console.print(f"  live      : {probe['live_status']}")
    return 0


def cmd_download(args: argparse.Namespace) -> int:
    settings = _build_settings(args)
    options = _options_from_args(args)

    try:
        url = validate_media_url(args.url, settings.allowed_host_list, settings.allow_private_hosts)
        if options.proxy:
            options = options.model_copy(update={"proxy": validate_proxy(options.proxy)})
        cookies = _resolve_cookie(args, settings)
        entries = expand_playlist(url, options, settings, cookies_file=cookies)
        if not entries:
            console.print("[yellow]Nothing matched the selection.[/]")
            return 1
    except (UnsafeURLError, SelectionError, ValueError) as exc:
        console.print(f"[red]Error:[/] {escape(str(exc))}")
        return 2
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Analysis failed:[/] {escape(friendly_error(str(exc)))}")
        return 2

    quiet = bool(args.json)

    if args.dry_run:
        if quiet:
            console.print_json(json.dumps(entries, ensure_ascii=False))
            return 0
        table = Table(title="Selection", header_style="bold")
        table.add_column("#", justify="right", width=5)
        table.add_column("Title", overflow="fold")
        table.add_column("Duration", justify="right", width=10)
        for entry in entries:
            table.add_row(
                str(entry.get("index") or ""),
                escape(entry.get("title") or ""),
                human_duration(entry.get("duration")) or "-",
            )
        console.print(table)
        return 0

    workers = max(1, min(len(entries), settings.max_concurrent_downloads))
    if not quiet:
        console.print(
            f"[bold cyan]Downloading {len(entries)} video(s)[/] - {workers} parallel worker(s) - "
            f"{'aria2c' if aria2_path() and settings.use_aria2 else 'native'} transport"
        )
        console.print(f"  -> {settings.download_path}")

    cancel = threading.Event()
    results: list[dict[str, Any]] = []
    started = time.monotonic()

    with Progress(
        TextColumn("[progress.description]{task.description}", table_column=None),
        BarColumn(bar_width=None),
        TaskProgressColumn(),
        TextColumn("{task.fields[status]}", style="cyan"),
        TimeRemainingColumn(),
        # Discard live bars in JSON mode so stdout stays valid JSON.
        console=Console(file=io.StringIO()) if quiet else console,
        transient=False,
    ) as progress:
        tasks: dict[str, TaskID] = {}
        for index, entry in enumerate(entries):
            label = (entry.get("title") or f"Video {index + 1}")[:52]
            tasks[entry["url"]] = progress.add_task(
                escape(label), total=100, status="queued"
            )

        def worker(entry: dict[str, Any]) -> dict[str, Any]:
            task = tasks[entry["url"]]
            tracker = ProgressTracker(progress, task)
            progress.update(task, status="starting")
            try:
                info = run_download(
                    entry["url"],
                    options,
                    settings,
                    progress_hook=tracker.hook,
                    cancel_event=cancel,
                    cookies_file=cookies,
                )
            except Exception as exc:  # noqa: BLE001
                message = friendly_error(str(exc))
                if cancel.is_set():
                    progress.update(task, status="[yellow]cancelled[/]")
                    return {
                        "title": entry.get("title"),
                        "status": "cancelled",
                        "error": "Cancelled",
                        "path": "",
                    }
                progress.update(task, status="[red]failed[/]")
                return {
                    "title": entry.get("title"),
                    "status": "failed",
                    "error": message,
                    "path": "",
                }
            path = extract_output_path(info, settings.download_path)
            size = path.stat().st_size if path else 0
            progress.update(
                task, completed=100, status=f"[green]done[/] {human_bytes(size)}"
            )
            return {
                "title": entry.get("title"),
                "status": "completed",
                "error": "",
                "path": str(path) if path else "",
                "size": size,
            }

        try:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="tt-cli") as pool:
                futures = [pool.submit(worker, entry) for entry in entries]
                for future in futures:
                    results.append(future.result())
        except KeyboardInterrupt:
            cancel.set()
            console.print("\n[yellow]Cancelling...[/]")
            raise

    elapsed = time.monotonic() - started
    ok = sum(1 for result in results if result["status"] == "completed")
    failed = sum(1 for result in results if result["status"] == "failed")

    if quiet:
        console.print_json(json.dumps(results, ensure_ascii=False))
        return 0 if failed == 0 else 1

    table = Table(title=f"Finished {ok}/{len(results)} in {elapsed:.1f}s", header_style="bold")
    table.add_column("Status", width=10)
    table.add_column("Title", overflow="fold")
    table.add_column("Size", justify="right", width=9)
    table.add_column("File", overflow="fold")
    for result in results:
        colour = {"completed": "green", "failed": "red", "cancelled": "yellow"}.get(
            result["status"], "white"
        )
        table.add_row(
            f"[{colour}]{result['status']}[/]",
            escape(result.get("title") or ""),
            human_bytes(result.get("size")) if result.get("size") else "-",
            escape(result.get("path") or result.get("error") or ""),
        )
    console.print(table)
    return 0 if failed == 0 else 1


def cmd_doctor(args: argparse.Namespace) -> int:
    settings = get_settings()
    table = Table(title="TurboTube doctor", header_style="bold")
    table.add_column("Check")
    table.add_column("Result")
    table.add_row("TurboTube", __version__)
    table.add_row("Python", sys.version.split()[0])
    table.add_row("yt-dlp", ytdlp_version())
    table.add_row("ffmpeg", escape(ffmpeg_path() or "[red]missing[/]"))
    table.add_row("aria2c", escape(aria2_path() or "[yellow]not found (native transport)[/]"))
    runtime = next(iter(js_runtime()), None)
    table.add_row(
        "js runtime",
        escape(runtime) if runtime else "[yellow]none (some videos may return HTTP 403)[/]",
    )
    try:
        from importlib.metadata import version as _dist_version

        ejs = _dist_version("yt-dlp-ejs")
    except Exception:  # noqa: BLE001
        ejs = ""
    table.add_row("ejs scripts", escape(ejs) if ejs else "[yellow]missing (pip install yt-dlp-ejs)[/]")
    table.add_row("download dir", escape(str(settings.download_path)))
    try:
        probe_file = settings.download_path / ".write-test"
        probe_file.write_text("ok", encoding="utf-8")
        probe_file.unlink()
        table.add_row("writable", "[green]yes[/]")
    except OSError as exc:
        table.add_row("writable", f"[red]no - {escape(str(exc))}[/]")
    table.add_row("workers", str(settings.max_concurrent_downloads))
    table.add_row("fragments", str(settings.fragments_per_download))
    table.add_row("api key", "enabled" if settings.api_key else "disabled")
    console.print(table)
    return 0


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="turbotube",
        description="High-performance YouTube downloader (CLI + Web UI).",
    )
    parser.add_argument("--version", action="version", version=f"TurboTube {__version__}")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="start the web UI")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--reload", action="store_true", help="auto-reload (development)")
    serve.set_defaults(func=cmd_serve)

    probe = sub.add_parser("probe", help="analyse a video or playlist")
    probe.add_argument("url")
    probe.add_argument("--json", action="store_true")
    probe.add_argument("--video", action="store_true", help="force single-video mode")
    probe.add_argument("--playlist", action="store_true", help="force playlist mode")
    probe.add_argument("--cookies", help="Netscape cookie file")
    probe.add_argument("--cookies-from-browser", dest="cookies_from_browser", default="")
    probe.add_argument("--proxy", default="")
    probe.set_defaults(func=cmd_probe)

    download = sub.add_parser("download", help="download a video or playlist")
    download.add_argument("url")
    download.add_argument("-q", "--quality", default="best",
                          choices=["best", "2160", "1440", "1080", "720", "480", "360", "worst"])
    download.add_argument("--container", default="mp4", choices=["mp4", "mkv", "webm", "original"])
    download.add_argument("--audio", nargs="?", const="mp3", default=None,
                          choices=["mp3", "m4a", "opus", "aac", "flac", "wav", "original"])
    download.add_argument("--items", help="playlist selection, e.g. 1-5,8,10-12")
    download.add_argument("--reverse", action="store_true")
    download.add_argument("--video", action="store_true")
    download.add_argument("--playlist", action="store_true")
    download.add_argument("-o", "--output-dir", dest="output_dir")
    download.add_argument("-w", "--workers", type=int, default=None)
    download.add_argument("--fragments", type=int, default=None)
    download.add_argument("--no-aria2", action="store_true")
    download.add_argument("--cookies")
    download.add_argument("--cookies-from-browser", dest="cookies_from_browser", default="")
    download.add_argument("--proxy", default="")
    download.add_argument("--limit-rate", dest="limit_rate", default="")
    download.add_argument("--section", default="", help="e.g. 00:30-02:10")
    download.add_argument("--subtitles", action="store_true")
    download.add_argument("--auto-subs", dest="auto_subs", action="store_true")
    download.add_argument("--sub-langs", dest="sub_langs", default="en")
    download.add_argument("--no-embed-subs", dest="no_embed_subs", action="store_true")
    download.add_argument("--no-thumbnail", dest="no_thumbnail", action="store_true")
    download.add_argument("--no-metadata", dest="no_metadata", action="store_true")
    download.add_argument("--chapters", action="store_true")
    download.add_argument("--sponsorblock", default="off", choices=["off", "mark", "remove"])
    download.add_argument("--archive", action="store_true")
    download.add_argument("--dry-run", action="store_true")
    download.add_argument("--json", action="store_true")
    download.set_defaults(func=cmd_download)

    doctor = sub.add_parser("doctor", help="check the local toolchain")
    doctor.set_defaults(func=cmd_doctor)

    return parser


def _make_streams_safe() -> None:
    """Never crash on characters the console encoding cannot represent.

    Windows consoles often use a legacy codepage (cp1256, cp437, ...), while
    video titles can contain any Unicode. Replacing unencodable characters is
    much friendlier than a UnicodeEncodeError in the middle of a download.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def main(argv: list[str] | None = None) -> int:
    _make_streams_safe()
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        argv = ["serve"]
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        console.print("[yellow]Interrupted.[/]")
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
