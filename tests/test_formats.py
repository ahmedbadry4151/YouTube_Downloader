from __future__ import annotations

import pytest

from app import youtube
from app.models import DownloadOptions
from app.youtube import (
    build_download_params,
    build_format_selector,
    friendly_error,
)


def test_video_selectors():
    best = build_format_selector(DownloadOptions(quality="best", container="mp4"))
    assert "bv*" in best and "ba" in best
    capped = build_format_selector(DownloadOptions(quality="1080", container="mp4"))
    assert "[height<=1080]" in capped
    assert "[ext=mp4]" in capped
    webm = build_format_selector(DownloadOptions(quality="720", container="webm"))
    assert "[ext=webm]" in webm and "[height<=720]" in webm
    mkv = build_format_selector(DownloadOptions(quality="480", container="mkv"))
    assert "[ext=mp4]" not in mkv and "[height<=480]" in mkv
    assert build_format_selector(DownloadOptions(quality="worst")).startswith("wv*")
    assert build_format_selector(DownloadOptions(audio_only=True)) == "bestaudio/best"


def test_params_quality_and_paths(settings):
    params = build_download_params(
        DownloadOptions(quality="1080", container="mp4", fragments=8, retries=5),
        settings,
    )
    assert params["format"].startswith("bv*[height<=1080]")
    assert params["merge_output_format"] == "mp4"
    assert params["concurrent_fragment_downloads"] == 8
    assert params["retries"] == 5
    assert params["paths"]["home"] == str(settings.download_path)
    assert params["format_sort"][:2] == ["res", "fps"]
    assert "external_downloader" not in params

    custom = build_download_params(DownloadOptions(output_template="My Clip"), settings)
    assert custom["outtmpl"]["default"] == "My Clip [%(id)s].%(ext)s"


def test_audio_postprocessors(settings):
    params = build_download_params(
        DownloadOptions(audio_only=True, audio_format="mp3", embed_thumbnail=True),
        settings,
    )
    keys = [pp["key"] for pp in params["postprocessors"]]
    assert "FFmpegExtractAudio" in keys
    assert "EmbedThumbnail" in keys
    assert "FFmpegVideoRemuxer" not in keys
    assert params["format"] == "bestaudio/best"
    extract = next(pp for pp in params["postprocessors"] if pp["key"] == "FFmpegExtractAudio")
    assert extract["preferredcodec"] == "mp3"


def test_subtitles_and_sections(settings):
    params = build_download_params(
        DownloadOptions(subtitles=True, subtitle_langs="en,ar", section="00:30-01:00"),
        settings,
    )
    assert params["writesubtitles"] is True
    assert params["subtitleslangs"] == ["en", "ar"]
    keys = [pp["key"] for pp in params["postprocessors"]]
    assert "FFmpegEmbedSubtitle" in keys
    assert params["download_ranges"] is not None
    assert params["force_keyframes_at_cuts"] is True


def test_sponsorblock_remove(settings):
    params = build_download_params(DownloadOptions(sponsorblock="remove"), settings)
    by_key = {pp["key"]: pp for pp in params["postprocessors"]}
    assert by_key["SponsorBlock"]["categories"]
    assert by_key["ModifyChapters"]["remove_sponsor_segments"]
    assert by_key["ModifyChapters"]["force_keyframes"] is True


def test_aria2_arguments(settings, monkeypatch):
    settings.use_aria2 = True
    monkeypatch.setattr(youtube, "aria2_path", lambda: "C:/tools/aria2c.exe")
    params = build_download_params(DownloadOptions(), settings)
    assert params["external_downloader"]["default"].endswith("aria2c.exe")
    args = params["external_downloader_args"]["default"]
    assert any(arg.startswith("-x") for arg in args)


def test_rate_limit_and_cookies(settings, tmp_path):
    cookie = tmp_path / "cookies.txt"
    cookie.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    params = build_download_params(
        DownloadOptions(rate_limit="1.5M", cookies_file=str(cookie)),
        settings,
    )
    assert params["ratelimit"] == int(1.5 * 1024**2)
    assert params["cookiefile"] == str(cookie)


def test_friendly_error_hints():
    message = friendly_error("ERROR: Sign in to confirm you're not a bot")
    assert "cookies" in message.lower()
    assert friendly_error("something odd") == "something odd"
