from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from .config import DownloadConfig


def build_format_selector(
    quality: str,
    prefer_compatible_codecs: bool = True,
    allow_separate_streams: bool = False,
) -> str:
    q = (quality or "best").strip().lower()
    if q in {"best", "auto"}:
        if prefer_compatible_codecs:
            if allow_separate_streams:
                return (
                    "bv*[vcodec^=avc1]+ba[acodec^=mp4a]/"
                    "b[ext=mp4][vcodec^=avc1][acodec^=mp4a]/"
                    "b[ext=mp4]/b"
                )
            return "b[ext=mp4][vcodec^=avc1][acodec^=mp4a]/b[ext=mp4]/b"
        return "bv*+ba/b"

    if q == "worst":
        return "worst[ext=mp4]/worst" if prefer_compatible_codecs else "worst"

    m = re.fullmatch(r"(2160|1440|1080|720|480|360|240)p", q)
    if not m:
        return "b[ext=mp4][vcodec^=avc1][acodec^=mp4a]/b[ext=mp4]/b" if prefer_compatible_codecs else "bv*+ba/b"

    h = int(m.group(1))
    if prefer_compatible_codecs:
        if allow_separate_streams:
            return (
                f"bv*[height<={h}][vcodec^=avc1]+ba[acodec^=mp4a]/"
                f"b[height<={h}][ext=mp4][vcodec^=avc1][acodec^=mp4a]/"
                f"b[height<={h}][ext=mp4]/b[height<={h}]"
            )
        return (
            f"b[height<={h}][ext=mp4][vcodec^=avc1][acodec^=mp4a]/"
            f"b[height<={h}][ext=mp4]/b[height<={h}]"
        )

    return f"bv*[height<={h}]+ba/b[height<={h}]/best[height<={h}]/b"


def _base_opts(download_cfg: DownloadConfig) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "quiet": False,
        "no_warnings": False,
        "ignoreerrors": False,
        "windowsfilenames": True,
        "noplaylist": False,
        "retries": 3,
        "fragment_retries": 3,
        "overwrites": False,
    }

    cookies_file = (download_cfg.cookies_file or "").strip()
    if cookies_file:
        cookie_path = Path(cookies_file).expanduser()
        if cookie_path.exists():
            opts["cookiefile"] = str(cookie_path)

    runtimes = [x.strip().lower() for x in download_cfg.js_runtimes if str(x).strip()]
    if runtimes:
        opts["js_runtimes"] = {name: {} for name in runtimes}

    remote_components = [x.strip() for x in download_cfg.remote_components if str(x).strip()]
    if remote_components:
        opts["remote_components"] = remote_components

    return opts


def _normalize_info(info: dict[str, Any]) -> dict[str, Any]:
    if "entries" in info and isinstance(info["entries"], list):
        for entry in info["entries"]:
            if isinstance(entry, dict):
                return entry
    return info


def extract_info(url: str, download_cfg: DownloadConfig) -> dict[str, Any]:
    opts = _base_opts(download_cfg)
    opts.update({"skip_download": True})

    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    if not isinstance(info, dict):
        raise RuntimeError(f"Unsupported info payload from yt-dlp for URL: {url}")
    return _normalize_info(info)


def download_best_subtitle(
    url: str,
    subtitle_stem: Path,
    subtitle_langs: list[str],
    download_cfg: DownloadConfig,
) -> Path | None:
    subtitle_stem.parent.mkdir(parents=True, exist_ok=True)

    opts = _base_opts(download_cfg)
    subtitle_format = (download_cfg.subtitle_output_format or "srt").strip().lower()
    if subtitle_format not in {"srt", "vtt", "ass"}:
        subtitle_format = "srt"
    opts.update(
        {
            "skip_download": True,
            "writesubtitles": True,
            "writeautomaticsub": bool(download_cfg.write_auto_subtitles),
            "subtitleslangs": subtitle_langs or ["all"],
            "subtitlesformat": f"{subtitle_format}/best",
            "outtmpl": {"default": str(subtitle_stem) + ".%(ext)s"},
        }
    )

    try:
        with YoutubeDL(opts) as ydl:
            ydl.download([url])
    except DownloadError:
        return None

    candidates = sorted(
        subtitle_stem.parent.glob(subtitle_stem.name + ".*"),
        key=lambda p: (p.suffix.lower() != ".srt", p.name),
    )
    for file_path in candidates:
        if file_path.suffix.lower() in {".srt", ".vtt", ".ass", ".ttml"}:
            return file_path
    return None


def download_media_file(
    url: str,
    media_stem: Path,
    download_cfg: DownloadConfig,
) -> Path:
    media_stem.parent.mkdir(parents=True, exist_ok=True)

    opts = _base_opts(download_cfg)
    opts.update(
        {
            "format": build_format_selector(
                quality=download_cfg.quality,
                prefer_compatible_codecs=download_cfg.prefer_compatible_codecs,
                allow_separate_streams=download_cfg.allow_separate_streams,
            ),
            "merge_output_format": "mp4",
            "outtmpl": {"default": str(media_stem) + ".%(ext)s"},
        }
    )

    with YoutubeDL(opts) as ydl:
        ydl.download([url])

    files = sorted(media_stem.parent.glob(media_stem.name + ".*"), key=lambda p: p.stat().st_mtime, reverse=True)
    for candidate in files:
        if candidate.suffix.lower() in {
            ".mp4",
            ".mkv",
            ".webm",
            ".mov",
            ".m4a",
            ".mp3",
            ".wav",
            ".flac",
            ".aac",
            ".ogg",
            ".opus",
        }:
            return candidate

    raise RuntimeError(f"yt-dlp reported success but no media file was found for URL: {url}")
