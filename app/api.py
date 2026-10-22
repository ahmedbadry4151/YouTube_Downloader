from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from . import __version__
from .config import PROJECT_ROOT, Settings
from .models import JobCreateRequest, ProbeRequest
from .security import UnsafeURLError, ensure_within, resolve_cookie_file, validate_media_url, validate_proxy
from .utils import SelectionError, human_bytes
from .youtube import (
    aria2_path,
    ffmpeg_path,
    friendly_error,
    js_runtime,
    probe_media,
    ytdlp_version,
)

log = logging.getLogger("turbotube.api")
router = APIRouter(prefix="/api")

_SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _manager(request: Request):
    return request.app.state.manager


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n"


# ---------------------------------------------------------------------------
# system
# ---------------------------------------------------------------------------
@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/system")
async def system_info(request: Request) -> dict[str, Any]:
    settings = _settings(request)
    manager = _manager(request)
    return {
        "app": "TurboTube",
        "version": __version__,
        "ytdlp": ytdlp_version(),
        "ffmpeg": bool(ffmpeg_path()),
        "aria2": bool(aria2_path()),
        "js_runtime": next(iter(js_runtime()), None),
        "download_dir": str(settings.download_path),
        "max_concurrent_downloads": settings.max_concurrent_downloads,
        "fragments_per_download": settings.fragments_per_download,
        "use_aria2": settings.use_aria2,
        "auth_required": bool(settings.api_key),
        "open_folder_enabled": settings.enable_open_folder,
        "jobs": manager.counts(),
        "defaults": {
            "quality": "best",
            "container": "mp4",
            "audio_format": "mp3",
            "sponsorblock": "off",
        },
    }


@router.post("/probe")
async def probe(payload: ProbeRequest, request: Request) -> dict[str, Any]:
    settings = _settings(request)

    def work() -> dict[str, Any]:
        url = validate_media_url(payload.url, settings.allowed_host_list, settings.allow_private_hosts)
        proxy = validate_proxy(payload.proxy)
        cookies = resolve_cookie_file(
            payload.cookies_file, settings.cookies_path, PROJECT_ROOT
        )
        return probe_media(
            url,
            settings,
            mode=payload.mode,
            cookies_file=cookies,
            cookies_from_browser=payload.cookies_from_browser,
            proxy=proxy,
        )

    try:
        return await run_in_threadpool(work)
    except UnsafeURLError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        log.warning("Probe failed: %s", exc)
        raise HTTPException(status_code=502, detail=friendly_error(str(exc))) from exc


# ---------------------------------------------------------------------------
# jobs
# ---------------------------------------------------------------------------
@router.post("/jobs", status_code=201)
async def create_job(payload: JobCreateRequest, request: Request) -> dict[str, Any]:
    manager = _manager(request)

    def work():
        return manager.create_job(
            payload.url,
            payload.options,
            entries=payload.entries,
            title=payload.title,
        )

    try:
        job = await run_in_threadpool(work)
    except UnsafeURLError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SelectionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        log.exception("Could not create job")
        raise HTTPException(status_code=502, detail=friendly_error(str(exc))) from exc
    return job.summary()


@router.get("/jobs")
async def list_jobs(
    request: Request,
    limit: int = Query(default=60, ge=1, le=500),
    status: str | None = Query(default=None),
) -> dict[str, Any]:
    manager = _manager(request)
    if status and status not in {
        "queued",
        "running",
        "completed",
        "failed",
        "cancelled",
        "partial",
        "interrupted",
    }:
        raise HTTPException(status_code=400, detail="Unknown status filter")
    jobs = await run_in_threadpool(lambda: manager.snapshot(limit=limit, status=status))
    return {"jobs": jobs, "counts": manager.counts()}


@router.get("/jobs/{job_id}")
async def get_job(job_id: str, request: Request) -> dict[str, Any]:
    job = _manager(request).get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job.summary()


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str, request: Request) -> dict[str, Any]:
    try:
        count = _manager(request).cancel_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc
    return {"cancelled": count}


@router.post("/jobs/{job_id}/retry")
async def retry_job(job_id: str, request: Request) -> dict[str, Any]:
    try:
        count = _manager(request).retry_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc
    return {"retried": count}


@router.delete("/jobs/{job_id}")
async def delete_job(
    job_id: str,
    request: Request,
    delete_files: bool = Query(default=False),
) -> dict[str, Any]:
    try:
        deleted = _manager(request).delete_job(job_id, delete_files=delete_files)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"deleted": True, "files": deleted}


@router.post("/jobs/{job_id}/items/{item_id}/cancel")
async def cancel_item(job_id: str, item_id: str, request: Request) -> dict[str, Any]:
    try:
        _manager(request).cancel_item(job_id, item_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"cancelled": True}


@router.post("/jobs/{job_id}/items/{item_id}/retry")
async def retry_item(job_id: str, item_id: str, request: Request) -> dict[str, Any]:
    try:
        retried = _manager(request).retry_item(job_id, item_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"retried": retried}


@router.post("/jobs/{job_id}/open")
async def open_job_folder(
    job_id: str,
    request: Request,
    item_id: str | None = Query(default=None),
) -> dict[str, Any]:
    settings = _settings(request)
    if not settings.enable_open_folder:
        raise HTTPException(status_code=403, detail="Opening folders is disabled")
    job = _manager(request).get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    target: Path | None = None
    if item_id:
        for item in job.items:
            if item.id == item_id and item.filepath:
                target = Path(item.filepath)
                break
    else:
        for item in job.items:
            if item.status == "completed" and item.filepath:
                target = Path(item.filepath)
                break
    if target is None or not target.exists():
        target = settings.download_path
    try:
        ensure_within(settings.download_path, target)
    except UnsafeURLError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        if sys.platform == "win32":
            if target.is_file():
                subprocess.Popen(["explorer", f"/select,{target}"])
            else:
                os.startfile(str(target))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            command = ["open", "-R", str(target)] if target.is_file() else ["open", str(target)]
            subprocess.Popen(command)
        else:
            subprocess.Popen(["xdg-open", str(target.parent if target.is_file() else target)])
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Could not open folder: {exc}") from exc
    return {"opened": str(target)}


# ---------------------------------------------------------------------------
# events
# ---------------------------------------------------------------------------
@router.get("/events")
async def events(request: Request) -> StreamingResponse:
    bus = request.app.state.bus
    manager = _manager(request)

    async def stream():
        queue = bus.subscribe()
        try:
            snapshot = await run_in_threadpool(lambda: manager.snapshot(limit=100))
            yield _sse({"type": "snapshot", "jobs": snapshot})
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                yield _sse(event)
        except asyncio.CancelledError:
            raise
        finally:
            bus.unsubscribe(queue)

    return StreamingResponse(stream(), media_type="text/event-stream", headers=_SSE_HEADERS)


# ---------------------------------------------------------------------------
# cookies
# ---------------------------------------------------------------------------
@router.get("/cookies")
async def list_cookies(request: Request) -> list[dict[str, Any]]:
    settings = _settings(request)
    result: list[dict[str, Any]] = []
    for path in sorted(settings.cookies_path.glob("*.txt"), key=lambda p: p.stat().st_mtime, reverse=True):
        stat = path.stat()
        result.append(
            {
                "id": path.name,
                "size": stat.st_size,
                "updated_at": stat.st_mtime,
                "label": f"cookies-{path.stem[:8]} ({human_bytes(stat.st_size)})",
            }
        )
    return result[:20]


@router.post("/cookies", status_code=201)
async def upload_cookies(request: Request, file: UploadFile = File(...)) -> dict[str, Any]:
    settings = _settings(request)
    content = await file.read(3 * 1024 * 1024 + 1)
    if len(content) > 3 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Cookie file is too large (max 3 MB)")
    if not content.strip():
        raise HTTPException(status_code=400, detail="Cookie file is empty")
    head = content[:4096]
    if b"Netscape" not in head and b"\t" not in head:
        raise HTTPException(
            status_code=400,
            detail="Not a Netscape cookie file. Export cookies with a browser extension.",
        )
    digest = hashlib.sha256(content).hexdigest()[:16]
    path = settings.cookies_path / f"{digest}.txt"
    path.write_bytes(content)
    return {"id": path.name, "label": f"cookies-{digest[:8]}", "path": str(path)}
