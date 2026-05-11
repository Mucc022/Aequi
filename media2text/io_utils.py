from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*]+')
WHITESPACE = re.compile(r"\s+")
DIRECT_MEDIA_EXTENSIONS = {".mp4", ".m4a", ".mp3", ".webm", ".mkv", ".mov", ".m3u8"}
DOCUMENT_EXTENSIONS = {".pdf"}


def is_url(value: str) -> bool:
    text = value.strip().lower()
    return text.startswith("http://") or text.startswith("https://")


def is_direct_media_url(value: str) -> bool:
    if not is_url(value):
        return False
    path = urlparse(value.strip()).path.lower()
    return any(path.endswith(ext) for ext in DIRECT_MEDIA_EXTENSIONS)


def is_direct_document_url(value: str) -> bool:
    if not is_url(value):
        return False
    parsed = urlparse(value.strip())
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    if "drive.google.com" in host:
        parts = [part for part in path.split("/") if part]
        query = parsed.query.lower()
        if len(parts) >= 3 and parts[0] == "file" and parts[1] == "d":
            if len(parts) == 3:
                return True
            tail = parts[3].lower()
            return tail in {"view", "preview"} or tail.endswith(".pdf")
        if path.endswith("/uc") and "id=" in query:
            return True
    return any(path.endswith(ext) for ext in DOCUMENT_EXTENSIONS)


def sanitize_filename(value: str, default: str = "untitled", max_len: int = 96) -> str:
    text = INVALID_FILENAME_CHARS.sub("_", value.strip())
    text = WHITESPACE.sub(" ", text).strip(" ._")
    if not text:
        text = default
    if len(text) > max_len:
        text = text[:max_len].rstrip(" ._")
    return text or default


def normalize_channel(value: str | None) -> str:
    return sanitize_filename(value or "unknown", default="unknown", max_len=48)


def normalize_platform(url: str | None, extractor: str | None = None) -> str:
    if extractor:
        low = extractor.lower()
        if "youtube" in low:
            return "youtube"
        if "bilibili" in low:
            return "bilibili"

    if not url:
        return "local"

    host = urlparse(url).netloc.lower()
    if "youtu" in host:
        return "youtube"
    if "bilibili" in host:
        return "bilibili"
    if not host:
        return "web"

    return sanitize_filename(host.removeprefix("www."), default="web", max_len=32).lower()


def pick_date_yy_mm_dd(upload_date: str | None) -> str:
    if upload_date and len(upload_date) == 8 and upload_date.isdigit():
        dt = datetime.strptime(upload_date, "%Y%m%d")
        return dt.strftime("%y-%m-%d")
    return datetime.now().strftime("%y-%m-%d")


def append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    return rows
