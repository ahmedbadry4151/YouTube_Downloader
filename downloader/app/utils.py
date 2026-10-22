from __future__ import annotations

import re
from pathlib import Path

_SPEC_PART = re.compile(r"^(\d+)(?:\s*-\s*(\d*))?$")
_RATE = re.compile(r"^(\d+(?:\.\d+)?)\s*([kmg]?)(?:/s)?$", re.IGNORECASE)

DEFAULT_OUTTMPL = "%(title).160B [%(id)s].%(ext)s"


class SelectionError(ValueError):
    """Raised for an invalid playlist item specification."""


def parse_item_spec(spec: str | None, total: int | None = None) -> list[int] | None:
    """Parse ``"1-5,8,10-12"`` into ordered, de-duplicated 1-based indexes.

    Returns ``None`` when the spec means "everything".
    """
    if spec is None:
        return None
    text = spec.strip()
    if not text or text.lower() in {"all", "*"}:
        return None

    parts = [p for p in re.split(r"[,\s]+", text) if p]
    if not parts:
        return None

    selected: list[int] = []
    for part in parts:
        match = _SPEC_PART.match(part)
        if not match:
            raise SelectionError(f"Invalid selection '{part}'. Try a format like 1-5,8,10-12")
        start = int(match.group(1))
        raw_end = match.group(2)
        open_ended = "-" in part and raw_end == ""
        if start < 1:
            raise SelectionError("Playlist positions start at 1")
        if open_ended:
            if total is None:
                raise SelectionError(f"Open range '{part}' needs a known playlist length")
            end = total
        else:
            end = int(raw_end) if raw_end else start
        if end < start:
            raise SelectionError(f"Range '{part}' is backwards")
        if total is not None and end > total:
            raise SelectionError(f"Range '{part}' exceeds the playlist length ({total})")
        selected.extend(range(start, end + 1))

    seen: set[int] = set()
    ordered: list[int] = []
    for index in selected:
        if index not in seen:
            seen.add(index)
            ordered.append(index)
    return ordered


def parse_rate_limit(value: str | None) -> int | None:
    """``"10M"`` -> ``10485760`` bytes/second. Empty -> ``None``."""
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    match = _RATE.match(text)
    if not match:
        raise ValueError("Rate limit must look like 500K, 10M or 1.5G")
    amount = float(match.group(1))
    unit = match.group(2).lower()
    multiplier = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3}[unit]
    result = int(amount * multiplier)
    if result <= 0:
        raise ValueError("Rate limit must be positive")
    return result


def parse_clock(value: str) -> float:
    """Parse ``90``, ``1:30`` or ``1:02:30`` into seconds."""
    text = value.strip()
    if not text:
        raise ValueError("Time value is empty")
    parts = text.split(":")
    if len(parts) > 3:
        raise ValueError(f"Invalid time '{value}' (use SS, MM:SS or HH:MM:SS)")
    try:
        numbers = [float(part) for part in parts]
    except ValueError as exc:
        raise ValueError(f"Invalid time '{value}' (use SS, MM:SS or HH:MM:SS)") from exc
    if any(number < 0 for number in numbers):
        raise ValueError("Time values cannot be negative")
    if len(parts) >= 2 and numbers[-1] >= 60:
        raise ValueError("Seconds must be below 60")
    if len(parts) == 3 and numbers[1] >= 60:
        raise ValueError("Minutes must be below 60")
    total = numbers[-1]
    if len(numbers) >= 2:
        total += numbers[-2] * 60
    if len(numbers) == 3:
        total += numbers[0] * 3600
    return total


def parse_section(value: str | None) -> tuple[float, float | None] | None:
    """Parse ``"00:30-02:10"`` into ``(start, end)`` (end may be ``None``)."""
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    if "-" not in text:
        raise ValueError("Section must look like 00:30-02:10 (or 00:30-)")
    start_text, end_text = text.split("-", 1)
    start = parse_clock(start_text)
    end = parse_clock(end_text) if end_text.strip() else None
    if end is not None and end <= start:
        raise ValueError("Section end must be after its start")
    return start, end


def parse_langs(value: str | None) -> list[str]:
    if not value:
        return ["en"]
    langs = [part.strip() for part in value.replace(";", ",").split(",") if part.strip()]
    if not langs:
        return ["en"]
    if len(langs) > 20:
        raise ValueError("Too many subtitle languages (max 20)")
    for lang in langs:
        if not re.match(r"^[A-Za-z0-9_.\-]{1,20}$", lang):
            raise ValueError(f"Invalid subtitle language '{lang}'")
    return langs


def normalize_outtmpl(template: str | None) -> str:
    text = (template or "").strip()
    if not text:
        return DEFAULT_OUTTMPL
    if "%(ext)" not in text:
        text = f"{text}.%(ext)s"
    if "%(id)" not in text:
        base, _, ext = text.rpartition(".")
        text = f"{base} [%(id)s].{ext}"
    return text


def human_bytes(size: float | int | None) -> str:
    if not size:
        return "0 B"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TB"


def human_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return ""
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def safe_join(base: Path, relative: str) -> Path:
    """Join and guarantee the result stays inside *base*."""
    candidate = (base / relative).resolve()
    if not candidate.is_relative_to(base.resolve()):
        raise ValueError("Resolved path escapes the download directory")
    return candidate
