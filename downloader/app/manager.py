from __future__ import annotations

import logging
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from yt_dlp.utils import DownloadCancelled

from .config import PROJECT_ROOT, Settings
from .events import EventBus
from .models import DownloadOptions, SelectedEntry
from .security import UnsafeURLError, ensure_within, resolve_cookie_file, validate_media_url, validate_proxy
from .store import Store
from .utils import parse_item_spec
from .youtube import (
    extract_output_path,
    expand_playlist,
    friendly_error,
    run_download,
)

log = logging.getLogger("turbotube.manager")

ACTIVE = {"queued", "running", "downloading", "processing"}
TERMINAL = {"completed", "failed", "cancelled", "interrupted"}
_ARTIFACT_SUFFIXES = {
    ".srt",
    ".vtt",
    ".ass",
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".json",
    ".description",
    ".part",
    ".ytdl",
}

_FATAL_MARKERS = (
    "private video",
    "members-only",
    "members only",
    "video unavailable",
    "this video is not available",
    "unsupported url",
    "requested format is not available",
    "sign in to confirm",
    "not a bot",
    "removed by the uploader",
    "does not exist",
    "age-restricted",
    "copyright",
    "account has been terminated",
)


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _retryable(message: str) -> bool:
    lowered = message.lower()
    return not any(marker in lowered for marker in _FATAL_MARKERS)


@dataclass
class ItemState:
    id: str
    job_id: str
    index: int
    url: str
    title: str
    created_at: float
    duration: float | None = None
    status: str = "queued"
    percent: float = 0.0
    speed: float = 0.0
    eta: float | None = None
    downloaded: int = 0
    total: int = 0
    filepath: str = ""
    error: str = ""
    attempts: int = 0
    updated_at: float = field(default_factory=time.time)
    cancel: threading.Event = field(default_factory=threading.Event, repr=False)
    streams: dict[str, tuple[int, int | None]] = field(default_factory=dict, repr=False)
    last_emit: float = 0.0
    last_persist: float = 0.0
    future: Future[Any] | None = field(default=None, repr=False)

    def to_store(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "job_id": self.job_id,
            "idx": self.index,
            "url": self.url,
            "title": self.title,
            "duration": self.duration,
            "status": self.status,
            "percent": self.percent,
            "speed": self.speed,
            "eta": self.eta,
            "downloaded": self.downloaded,
            "total": self.total,
            "filepath": self.filepath,
            "error": self.error,
            "attempts": self.attempts,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "job_id": self.job_id,
            "index": self.index,
            "url": self.url,
            "title": self.title,
            "duration": self.duration,
            "status": self.status,
            "percent": round(self.percent, 1),
            "speed": round(self.speed, 1),
            "eta": self.eta,
            "downloaded": self.downloaded,
            "total": self.total,
            "filepath": self.filepath,
            "filename": Path(self.filepath).name if self.filepath else "",
            "error": self.error,
            "attempts": self.attempts,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class JobState:
    id: str
    url: str
    title: str
    kind: str
    options: DownloadOptions
    created_at: float
    items: list[ItemState]
    status: str = "queued"
    error: str = ""
    updated_at: float = field(default_factory=time.time)
    cookies_file: str | None = None

    def summary(self, *, include_items: bool = True) -> dict[str, Any]:
        completed = sum(1 for item in self.items if item.status == "completed")
        failed = sum(1 for item in self.items if item.status == "failed")
        cancelled = sum(1 for item in self.items if item.status == "cancelled")
        active = sum(1 for item in self.items if item.status in ACTIVE)

        total_bytes = sum(item.total for item in self.items)
        downloaded = sum(item.downloaded for item in self.items)
        if total_bytes > 0:
            percent = min(100.0, downloaded / total_bytes * 100)
        elif self.items:
            percent = sum(item.percent for item in self.items) / len(self.items)
        else:
            percent = 0.0

        speed = sum(item.speed for item in self.items if item.status in ACTIVE)
        remaining = max(0, total_bytes - downloaded)
        eta = None
        if speed > 0 and remaining:
            eta = remaining / speed

        payload: dict[str, Any] = {
            "id": self.id,
            "url": self.url,
            "title": self.title,
            "kind": self.kind,
            "status": self.status,
            "error": self.error,
            "options": self.options.model_dump(),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "total_items": len(self.items),
            "completed_items": completed,
            "failed_items": failed,
            "cancelled_items": cancelled,
            "active_items": active,
            "percent": round(min(100.0, percent), 1),
            "downloaded": downloaded,
            "total_bytes": total_bytes,
            "speed": round(speed, 1),
            "eta": eta,
        }
        if include_items:
            payload["items"] = [item.to_dict() for item in self.items]
        return payload


class JobManager:
    """Owns the job queue, worker pool and live state."""

    def __init__(self, settings: Settings, store: Store, bus: EventBus) -> None:
        self.settings = settings
        self.store = store
        self.bus = bus
        self._lock = threading.RLock()
        self._jobs: dict[str, JobState] = {}
        self._executor = ThreadPoolExecutor(
            max_workers=settings.max_concurrent_downloads,
            thread_name_prefix="tt-download",
        )
        self._closed = False
        self._log_lock = threading.Lock()
        self._logs_dir = settings.data_path / "logs"
        self._load_history()

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def _load_history(self) -> None:
        jobs, items_by_job = self.store.load(limit=self.settings.max_history)
        now = time.time()
        for row in jobs:
            try:
                options = DownloadOptions.model_validate(row.get("options") or {})
            except Exception:
                options = DownloadOptions()
            items: list[ItemState] = []
            for position, item_row in enumerate(items_by_job.get(row["id"], []), start=1):
                status = item_row["status"]
                error = item_row["error"]
                if status in ACTIVE:
                    status = "interrupted"
                    error = "Server restarted while this item was active"
                items.append(
                    ItemState(
                        id=item_row["id"],
                        job_id=row["id"],
                        index=int(item_row["idx"] or position),
                        url=item_row["url"],
                        title=item_row["title"] or "",
                        duration=item_row["duration"],
                        created_at=item_row["created_at"] or now,
                        status=status,
                        percent=float(item_row["percent"] or 0),
                        speed=0.0,
                        eta=item_row["eta"],
                        downloaded=int(item_row["downloaded"] or 0),
                        total=int(item_row["total"] or 0),
                        filepath=item_row["filepath"] or "",
                        error=error,
                        attempts=int(item_row["attempts"] or 0),
                        updated_at=item_row["updated_at"] or now,
                    )
                )
            job = JobState(
                id=row["id"],
                url=row["url"],
                title=row["title"] or row["url"],
                kind=row["kind"] or "video",
                options=options,
                created_at=row["created_at"] or now,
                items=items,
                status=row["status"] if row["status"] not in ACTIVE else "interrupted",
                error=row["error"] or "",
                updated_at=row["updated_at"] or now,
                cookies_file=self._safe_cookie(options),
            )
            self._recompute_status(job, persist=False)
            self._jobs[job.id] = job
        if jobs:
            log.info("Restored %s job(s) from history", len(jobs))

    def _safe_cookie(self, options: DownloadOptions) -> str | None:
        try:
            return resolve_cookie_file(options.cookies_file, self.settings.cookies_path, PROJECT_ROOT)
        except UnsafeURLError:
            return None

    def shutdown(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for job in self._jobs.values():
                for item in job.items:
                    if item.status in ACTIVE:
                        item.cancel.set()
        self._executor.shutdown(wait=False, cancel_futures=True)
        log.info("Download manager stopped")

    def resume_interrupted(self) -> int:
        count = 0
        for job in list(self._jobs.values()):
            pending = [item for item in job.items if item.status == "interrupted"]
            if pending:
                count += self.retry_job(job.id)
        return count

    # ------------------------------------------------------------------
    # creation
    # ------------------------------------------------------------------
    def create_job(
        self,
        url: str,
        options: DownloadOptions,
        *,
        entries: list[SelectedEntry] | None = None,
        title: str = "",
    ) -> JobState:
        if self._closed:
            raise RuntimeError("Server is shutting down")

        clean_url = validate_media_url(
            url, self.settings.allowed_host_list, self.settings.allow_private_hosts
        )
        if options.proxy:
            options = options.model_copy(update={"proxy": validate_proxy(options.proxy)})

        cookies_file = self._safe_cookie(options)
        if options.cookies_file and not cookies_file:
            raise UnsafeURLError("Cookie file not found")

        selected: list[dict[str, Any]]
        kind = "video"
        if entries:
            selected = self._clean_entries(entries)
            if options.playlist_items:
                highest = max(int(entry.get("index") or position) for position, entry in enumerate(selected, 1))
                wanted = parse_item_spec(options.playlist_items, total=highest)
                if wanted:
                    selected = [entry for entry in selected if int(entry.get("index") or 0) in wanted]
            if not selected:
                raise ValueError("No playlist entries were selected")
            kind = "playlist" if len(selected) > 1 else "video"
        else:
            selected = expand_playlist(clean_url, options, self.settings, cookies_file=cookies_file)
            kind = "playlist" if len(selected) > 1 else "video"
            options = options.model_copy(
                update={
                    "mode": "playlist" if kind == "playlist" else "video",
                }
            )

        if not selected:
            raise ValueError("Nothing to download")

        now = time.time()
        job_id = _new_id()
        job_title = (title or "").strip()
        if not job_title:
            if kind == "playlist":
                job_title = f"Playlist · {len(selected)} videos"
            else:
                job_title = selected[0].get("title") or clean_url

        job = JobState(
            id=job_id,
            url=clean_url,
            title=job_title[:250],
            kind=kind,
            options=options,
            created_at=now,
            items=[],
            status="queued",
            cookies_file=cookies_file,
        )
        for position, entry in enumerate(selected, start=1):
            job.items.append(
                ItemState(
                    id=_new_id(),
                    job_id=job_id,
                    index=int(entry.get("index") or position),
                    url=entry["url"],
                    title=str(entry.get("title") or f"Video {position}")[:300],
                    duration=entry.get("duration"),
                    created_at=now,
                )
            )

        with self._lock:
            self._jobs[job_id] = job
            self.store.save_job(self._job_row(job))
            self.store.save_items([item.to_store() for item in job.items])
            self.store.prune(self.settings.max_history)
            self._trim_memory()
        self._emit_job(job)
        for item in job.items:
            self._submit(item)
        log.info("Created job %s (%s item(s)): %s", job.id, len(job.items), job.title)
        return job

    def _trim_memory(self) -> None:
        with self._lock:
            if len(self._jobs) <= self.settings.max_history:
                return
            overflow = len(self._jobs) - self.settings.max_history
            stale = sorted(self._jobs.values(), key=lambda job: job.created_at)
            for job in stale:
                if overflow <= 0:
                    break
                if any(item.status in ACTIVE for item in job.items):
                    continue
                self._jobs.pop(job.id, None)
                overflow -= 1

    def _clean_entries(self, entries: list[SelectedEntry]) -> list[dict[str, Any]]:
        cleaned: list[dict[str, Any]] = []
        seen: set[str] = set()
        for entry in entries:
            url = validate_media_url(
                entry.url, self.settings.allowed_host_list, self.settings.allow_private_hosts
            )
            if url in seen:
                continue
            seen.add(url)
            cleaned.append(
                {
                    "url": url,
                    "title": entry.title or "Video",
                    "index": entry.index or len(cleaned) + 1,
                    "duration": entry.duration,
                }
            )
        return cleaned

    # ------------------------------------------------------------------
    # worker
    # ------------------------------------------------------------------
    def _submit(self, item: ItemState) -> None:
        if self._closed:
            return
        try:
            item.future = self._executor.submit(self._run_item, item)
        except RuntimeError:
            item.future = None

    def _run_item(self, item: ItemState) -> None:
        job = self._jobs.get(item.job_id)
        if job is None:
            return
        if item.cancel.is_set():
            self._set_status(item, "cancelled", error="Cancelled")
            self._recompute_status(job)
            return

        with self._lock:
            item.status = "running"
            item.attempts += 1
            item.streams.clear()
            item.speed = 0.0
            item.eta = None
            item.updated_at = time.time()
        self._emit_item(item)
        self._persist_items([item])
        # Promote the job to "running" as soon as its first worker starts so
        # the UI and REST clients never see a queued job with active items.
        self._recompute_status(job, persist=False)

        attempts = 0
        max_attempts = 2
        while attempts < max_attempts and not self._closed:
            attempts += 1
            info: dict[str, Any] | None = None
            try:
                info = run_download(
                    item.url,
                    job.options,
                    self.settings,
                    progress_hook=lambda data: self._on_progress(item, data),
                    pp_hook=lambda data: self._on_postprocess(item, data),
                    log_sink=lambda message: self._log_line(item, message),
                    cancel_event=item.cancel,
                    cookies_file=job.cookies_file,
                )
            except DownloadCancelled:
                self._set_status(item, "cancelled", error="Cancelled by user")
                break
            except Exception as exc:  # noqa: BLE001 - surfaced to the UI
                message = friendly_error(str(exc))
                self._log_line(item, message)
                if item.cancel.is_set() or self._closed:
                    self._set_status(item, "cancelled", error="Cancelled")
                    break
                if attempts < max_attempts and _retryable(message):
                    self._log_line(item, f"Transient failure, retrying ({attempts}/{max_attempts - 1})")
                    self._set_status(item, "running", error=f"Retrying: {message[:180]}")
                    if item.cancel.wait(2.0):
                        self._set_status(item, "cancelled", error="Cancelled")
                        break
                    continue
                self._set_status(item, "failed", error=message)
                break
            else:
                path = extract_output_path(info, self.settings.download_path)
                size = path.stat().st_size if path else 0
                with self._lock:
                    item.status = "completed"
                    item.error = ""
                    item.filepath = str(path) if path else ""
                    item.percent = 100.0
                    item.speed = 0.0
                    item.eta = 0.0
                    if size:
                        item.total = size
                        item.downloaded = size
                    item.updated_at = time.time()
                self._log_line(item, f"Completed: {item.filepath or item.title}")
                break

        self._persist_items([item])
        self._recompute_status(job)

    def _set_status(self, item: ItemState, status: str, *, error: str = "") -> None:
        with self._lock:
            item.status = status
            item.error = error
            item.speed = 0.0
            item.eta = None if status != "completed" else 0
            item.updated_at = time.time()
        self._emit_item(item)
        self._persist_items([item])

    def _on_progress(self, item: ItemState, data: dict[str, Any]) -> None:
        status = data.get("status")
        job = self._jobs.get(item.job_id)
        if job is None:
            return

        if status in {"downloading", "finished"}:
            info = data.get("info_dict") or {}
            key = str(info.get("format_id") or data.get("filename") or "stream")
            total = data.get("total_bytes") or data.get("total_bytes_estimate")
            downloaded = data.get("downloaded_bytes")
            now = time.time()
            with self._lock:
                if status == "finished":
                    previous = item.streams.get(key)
                    downloaded = int(total or downloaded or (previous[0] if previous else 0))
                    total = total or downloaded
                elif downloaded is None:
                    downloaded = 0
                item.streams[key] = (int(downloaded or 0), int(total) if total else None)
                self._recalculate(item)
                item.status = "downloading"
                if status == "finished":
                    item.speed = 0.0
                    item.eta = None
                else:
                    item.speed = float(data.get("speed") or 0)
                    eta = data.get("eta")
                    item.eta = float(eta) if eta is not None else None
                item.updated_at = now
                due = now - item.last_emit >= 0.25
                if due:
                    item.last_emit = now
                persist = now - item.last_persist >= 3.0
                if persist:
                    item.last_persist = now
            if due:
                self._emit_item(item)
                self._emit_job(job)
            if persist:
                self._persist_items([item])

        elif status == "error":
            self._log_line(item, f"Downloader reported an error: {data.get('error', 'unknown')}")

    @staticmethod
    def _recalculate(item: ItemState) -> None:
        if not item.streams:
            return
        downloaded = sum(stream[0] for stream in item.streams.values())
        totals = [stream[1] for stream in item.streams.values()]
        if all(totals):
            total = int(sum(totals))
            item.total = total
            item.downloaded = downloaded
            item.percent = min(100.0, downloaded / total * 100) if total else 0.0
        else:
            percents = [
                (stream[0] / stream[1] * 100) if stream[1] else 0.0 for stream in item.streams.values()
            ]
            item.downloaded = downloaded
            item.total = max(item.total, sum(t for t in totals if t))
            item.percent = min(100.0, sum(percents) / len(percents))
        if item.percent >= 99.9:
            item.percent = 99.9  # 100% only once post-processing is done

    def _on_postprocess(self, item: ItemState, data: dict[str, Any]) -> None:
        status = data.get("status")
        if status in {"started", "processing"}:
            with self._lock:
                item.status = "processing"
                item.speed = 0.0
                item.eta = None
                item.updated_at = time.time()
            self._emit_item(item)
            job = self._jobs.get(item.job_id)
            if job:
                self._emit_job(job)

    # ------------------------------------------------------------------
    # status / events
    # ------------------------------------------------------------------
    def _recompute_status(self, job: JobState, *, persist: bool = True) -> None:
        statuses = [item.status for item in job.items]
        if any(status in {"running", "downloading", "processing"} for status in statuses):
            status = "running"
        elif any(status == "queued" for status in statuses):
            status = "queued" if all(s == "queued" for s in statuses) else "running"
        elif statuses and all(status == "completed" for status in statuses):
            status = "completed"
        elif statuses and all(status == "failed" for status in statuses):
            status = "failed"
        elif statuses and all(status == "cancelled" for status in statuses):
            status = "cancelled"
        else:
            status = "partial"
        with self._lock:
            job.status = status
            job.updated_at = time.time()
        if persist:
            self.store.save_job(self._job_row(job))
        self._emit_job(job)

    def _job_row(self, job: JobState) -> dict[str, Any]:
        return {
            "id": job.id,
            "url": job.url,
            "title": job.title,
            "kind": job.kind,
            "status": job.status,
            "options": job.options.model_dump_json(),
            "created_at": job.created_at,
            "updated_at": job.updated_at,
            "error": job.error,
            "total_items": len(job.items),
        }

    def _persist_items(self, items: list[ItemState]) -> None:
        try:
            self.store.save_items([item.to_store() for item in items])
        except Exception:  # noqa: BLE001
            log.exception("Could not persist item progress")

    def _emit_item(self, item: ItemState) -> None:
        self.bus.publish({"type": "item", "job_id": item.job_id, "item": item.to_dict()})

    def _emit_job(self, job: JobState) -> None:
        self.bus.publish({"type": "job", "job": job.summary(include_items=False)})

    def _log_line(self, item: ItemState, message: str) -> None:
        text = str(message).replace("\r", " ").strip()
        if not text:
            return
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        line = f"{stamp} [{item.id}] {text[:2000]}\n"
        try:
            with self._log_lock:
                path = self._logs_dir / f"{item.job_id}.log"
                if path.exists() and path.stat().st_size > 5 * 1024 * 1024:
                    return
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(line)
        except OSError:
            pass

    # ------------------------------------------------------------------
    # queries
    # ------------------------------------------------------------------
    def snapshot(self, *, limit: int = 60, status: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda job: job.created_at, reverse=True)
        if status:
            jobs = [job for job in jobs if job.status == status]
        return [job.summary() for job in jobs[:limit]]

    def get_job(self, job_id: str) -> JobState | None:
        return self._jobs.get(job_id)

    def counts(self) -> dict[str, int]:
        with self._lock:
            return {
                "total": len(self._jobs),
                "active": sum(1 for job in self._jobs.values() if job.status in ACTIVE),
            }

    # ------------------------------------------------------------------
    # control
    # ------------------------------------------------------------------
    def cancel_job(self, job_id: str) -> int:
        job = self._jobs.get(job_id)
        if not job:
            raise KeyError(job_id)
        changed = 0
        for item in job.items:
            if item.status in ACTIVE:
                item.cancel.set()
                changed += 1
                if item.status == "queued" and item.future and item.future.cancel():
                    self._set_status(item, "cancelled", error="Cancelled before start")
        self._recompute_status(job)
        return changed

    def cancel_item(self, job_id: str, item_id: str) -> None:
        job = self._jobs.get(job_id)
        if not job:
            raise KeyError(job_id)
        for item in job.items:
            if item.id != item_id:
                continue
            if item.status not in ACTIVE:
                return
            item.cancel.set()
            if item.status == "queued" and item.future and item.future.cancel():
                self._set_status(item, "cancelled", error="Cancelled before start")
            self._recompute_status(job)
            return
        raise KeyError(item_id)

    def retry_job(self, job_id: str) -> int:
        job = self._jobs.get(job_id)
        if not job:
            raise KeyError(job_id)
        resubmitted = 0
        for item in job.items:
            if item.status in {"failed", "cancelled", "interrupted"}:
                self._reset_item(item)
                self._submit(item)
                resubmitted += 1
        if resubmitted:
            job.error = ""
            self._recompute_status(job)
        return resubmitted

    def retry_item(self, job_id: str, item_id: str) -> bool:
        job = self._jobs.get(job_id)
        if not job:
            raise KeyError(job_id)
        for item in job.items:
            if item.id == item_id:
                if item.status not in {"failed", "cancelled", "interrupted"}:
                    return False
                self._reset_item(item)
                self._submit(item)
                self._recompute_status(job)
                return True
        raise KeyError(item_id)

    def _reset_item(self, item: ItemState) -> None:
        with self._lock:
            item.cancel.clear()
            item.status = "queued"
            item.percent = 0.0
            item.speed = 0.0
            item.eta = None
            item.downloaded = 0
            item.total = 0
            item.error = ""
            item.attempts = 0
            item.streams.clear()
            item.updated_at = time.time()
        self._emit_item(item)
        self._persist_items([item])

    def delete_job(self, job_id: str, *, delete_files: bool = False) -> list[str]:
        job = self._jobs.get(job_id)
        if not job:
            raise KeyError(job_id)
        if any(item.status in ACTIVE for item in job.items):
            raise ValueError("Cancel the job before deleting it")
        deleted: list[str] = []
        if delete_files:
            for item in job.items:
                if not item.filepath:
                    continue
                try:
                    path = ensure_within(self.settings.download_path, Path(item.filepath))
                except UnsafeURLError:
                    continue
                if path.is_file():
                    try:
                        path.unlink()
                        deleted.append(str(path))
                    except OSError:
                        continue
                for suffix in _ARTIFACT_SUFFIXES:
                    artifact = path.with_suffix(suffix)
                    if artifact.is_file():
                        try:
                            artifact.unlink()
                            deleted.append(str(artifact))
                        except OSError:
                            pass
        with self._lock:
            self._jobs.pop(job_id, None)
        self.store.delete_job(job_id)
        return deleted
