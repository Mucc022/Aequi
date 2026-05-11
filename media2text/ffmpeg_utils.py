from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

MEDIA_EXTENSIONS = {
    ".mp4",
    ".mkv",
    ".mov",
    ".avi",
    ".flv",
    ".wmv",
    ".m4v",
    ".webm",
    ".m4a",
    ".mp3",
    ".wav",
    ".flac",
    ".aac",
    ".ogg",
    ".opus",
    ".wma",
    ".m4b",
    ".aiff",
    ".aif",
    ".caf",
    ".amr",
}

DOCUMENT_EXTENSIONS = {".pdf"}

APPLE_MEMO_EXTENSIONS = {".m4a", ".caf"}


def _no_window_kwargs() -> dict:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def _find_ffmpeg_from_winget() -> str | None:
    local_appdata = os.environ.get("LOCALAPPDATA")
    if not local_appdata:
        return None

    link_path = Path(local_appdata) / "Microsoft" / "WinGet" / "Links" / "ffmpeg.exe"
    if link_path.is_file():
        return str(link_path)

    packages_dir = Path(local_appdata) / "Microsoft" / "WinGet" / "Packages"
    if not packages_dir.is_dir():
        return None

    candidates = sorted(
        packages_dir.glob("Gyan.FFmpeg_*/*/bin/ffmpeg.exe"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if candidates:
        return str(candidates[0])
    return None


def check_ffmpeg_available(ffmpeg_bin: str = "ffmpeg") -> tuple[bool, str]:
    candidate_path = Path(ffmpeg_bin).expanduser()
    if candidate_path.is_file():
        ffmpeg_path = str(candidate_path.resolve())
    else:
        ffmpeg_path = shutil.which(ffmpeg_bin)
        if not ffmpeg_path and ffmpeg_bin == "ffmpeg":
            ffmpeg_path = _find_ffmpeg_from_winget()

    if not ffmpeg_path:
        return False, (
            "FFmpeg not found in PATH. Install FFmpeg first.\n"
            "Windows: winget install Gyan.FFmpeg"
        )

    result = subprocess.run([ffmpeg_path, "-version"], capture_output=True, text=True, check=False, **_no_window_kwargs())
    if result.returncode != 0:
        return False, "FFmpeg exists but failed to execute."

    return True, ffmpeg_path


def discover_local_media(input_path: Path) -> list[Path]:
    if input_path.is_file():
        return [input_path] if input_path.suffix.lower() in MEDIA_EXTENSIONS else []
    if not input_path.is_dir():
        return []

    files = [p for p in input_path.rglob("*") if p.is_file() and p.suffix.lower() in MEDIA_EXTENSIONS]
    files.sort()
    return files


def discover_local_documents(input_path: Path) -> list[Path]:
    if input_path.is_file():
        return [input_path] if input_path.suffix.lower() in DOCUMENT_EXTENSIONS else []
    if not input_path.is_dir():
        return []

    files = [p for p in input_path.rglob("*") if p.is_file() and p.suffix.lower() in DOCUMENT_EXTENSIONS]
    files.sort()
    return files


def extract_audio_to_wav(
    media_path: Path,
    wav_path: Path,
    ffmpeg_bin: str,
    sample_rate: int = 16000,
    channels: int = 1,
) -> None:
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(media_path),
        "-vn",
        "-ac",
        str(channels),
        "-ar",
        str(sample_rate),
        "-c:a",
        "pcm_s16le",
        str(wav_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False, **_no_window_kwargs())
    if result.returncode != 0:
        stderr = (result.stderr or "").strip() or "Unknown ffmpeg error"
        ext = media_path.suffix.lower()
        if ext in APPLE_MEMO_EXTENSIONS:
            raise RuntimeError(
                f"ffmpeg failed for Apple memo format '{ext}': {stderr}. "
                "Please verify the file is playable and retry with --language auto."
            )
        raise RuntimeError(f"ffmpeg failed for '{media_path.name}': {stderr}")


def extract_audio_to_file(
    media_path: Path,
    output_audio_path: Path,
    ffmpeg_bin: str,
    audio_format: str = "mp3",
) -> None:
    output_audio_path.parent.mkdir(parents=True, exist_ok=True)
    fmt = (audio_format or "").strip().lower()
    if fmt not in {"mp3", "wav"}:
        fmt = "mp3"

    if fmt == "wav":
        codec_args = ["-c:a", "pcm_s16le"]
    else:
        codec_args = ["-c:a", "libmp3lame", "-q:a", "2"]

    cmd = [
        ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(media_path),
        "-vn",
        *codec_args,
        str(output_audio_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False, **_no_window_kwargs())
    if result.returncode != 0:
        stderr = (result.stderr or "").strip() or "Unknown ffmpeg error"
        raise RuntimeError(f"ffmpeg audio export failed for '{media_path.name}': {stderr}")
