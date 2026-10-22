from __future__ import annotations

import pytest

from app.utils import (
    SelectionError,
    human_bytes,
    human_duration,
    normalize_outtmpl,
    parse_clock,
    parse_item_spec,
    parse_langs,
    parse_rate_limit,
    parse_section,
    safe_join,
)


def test_spec_all_variants():
    assert parse_item_spec(None) is None
    assert parse_item_spec("") is None
    assert parse_item_spec("   ") is None
    assert parse_item_spec("all") is None
    assert parse_item_spec("*") is None


def test_spec_ranges_and_dedupe():
    assert parse_item_spec("1-3,5") == [1, 2, 3, 5]
    assert parse_item_spec("1-2,2-3,3") == [1, 2, 3]
    assert parse_item_spec(" 7 , 2 , 4-5 ") == [7, 2, 4, 5]
    assert parse_item_spec("10-", total=12) == [10, 11, 12]
    assert parse_item_spec("4", total=4) == [4]


@pytest.mark.parametrize("spec", ["a", "0", "3-1", "1-3"])
def test_spec_invalid(spec):
    with pytest.raises(SelectionError):
        parse_item_spec(spec, total=2)


def test_open_range_requires_total():
    with pytest.raises(SelectionError):
        parse_item_spec("3-")


def test_rate_limit():
    assert parse_rate_limit("") is None
    assert parse_rate_limit(None) is None
    assert parse_rate_limit("500K") == 500 * 1024
    assert parse_rate_limit("1.5M") == int(1.5 * 1024**2)
    assert parse_rate_limit("2G") == 2 * 1024**3
    with pytest.raises(ValueError):
        parse_rate_limit("fast")


def test_clock_and_section():
    assert parse_clock("90") == 90
    assert parse_clock("1:30") == 90
    assert parse_clock("1:02:03") == 3723
    assert parse_section("00:30-02:10") == (30, 130)
    assert parse_section("1:00-") == (60, None)
    assert parse_section("") is None
    with pytest.raises(ValueError):
        parse_section("02:00-01:00")
    with pytest.raises(ValueError):
        parse_clock("abc")


def test_langs():
    assert parse_langs("") == ["en"]
    assert parse_langs("en, ar ;fr") == ["en", "ar", "fr"]
    with pytest.raises(ValueError):
        parse_langs("en;rm -rf")


def test_outtmpl():
    assert normalize_outtmpl("") == "%(title).160B [%(id)s].%(ext)s"
    assert "%(id)" in normalize_outtmpl("custom")
    assert normalize_outtmpl("custom").endswith(".%(ext)s")
    template = normalize_outtmpl("%(title)s [%(id)s]")
    assert template == "%(title)s [%(id)s].%(ext)s"


def test_humanizers():
    assert human_bytes(0) == "0 B"
    assert human_bytes(1536) == "1.5 KB"
    assert human_duration(65) == "1:05"
    assert human_duration(3723) == "1:02:03"


def test_safe_join(tmp_path):
    base = tmp_path / "downloads"
    base.mkdir()
    assert safe_join(base, "file.mp4") == (base / "file.mp4").resolve()
    with pytest.raises(ValueError):
        safe_join(base, "../escape.mp4")
