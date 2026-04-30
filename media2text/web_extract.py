from __future__ import annotations

import html
import re
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

VIDEO_HOST_HINTS = {
    "youtube.com",
    "youtu.be",
    "vimeo.com",
    "player.vimeo.com",
    "facebook.com",
    "dailymotion.com",
    "youku.com",
    "bilibili.com",
    "xigua.com",
    "douyin.com",
    "video.wixstatic.com",
}

VIDEO_EXTENSIONS = (
    ".mp4",
    ".m3u8",
    ".webm",
    ".mov",
    ".mkv",
    ".avi",
    ".flv",
)

URL_REGEX = re.compile(r"https?://[^\s\"'<>\\]+", re.IGNORECASE)


def normalize_url(base_url: str, raw_url: str) -> str | None:
    if not raw_url:
        return None

    cleaned = raw_url.strip().strip("\"'")
    cleaned = html.unescape(cleaned)
    if any(token in cleaned for token in ("${", "`", "{", "}")):
        return None
    if cleaned.startswith("javascript:") or cleaned.startswith("data:"):
        return None

    absolute = urljoin(base_url, cleaned)
    parsed = urlparse(absolute)
    if parsed.scheme not in {"http", "https"}:
        return None

    parsed = parsed._replace(fragment="")
    return urlunparse(parsed)


def is_probably_video_url(url: str, page_host: str) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    query = parsed.query.lower()

    if any(path.endswith(ext) for ext in VIDEO_EXTENSIONS):
        return True
    if "youtube.com" in host or "youtu.be" in host:
        return any(token in path for token in ("/watch", "/shorts", "/live", "/embed")) or "v=" in query
    if "bilibili.com" in host:
        return True
    if "vimeo.com" in host:
        return True
    if any(hint in host for hint in VIDEO_HOST_HINTS):
        return True

    if host == page_host and any(token in path for token in ("/video", "/watch", "/embed", "/player")):
        return True

    return False


def extract_candidates(page_url: str, html_text: str) -> list[str]:
    soup = BeautifulSoup(html_text, "html.parser")
    page_host = urlparse(page_url).netloc.lower()
    found: list[str] = []

    attrs = ("src", "href", "data-src", "data-url", "content")
    for tag in soup.find_all(True):
        for attr in attrs:
            raw_value = tag.get(attr)
            if not raw_value:
                continue
            normalized = normalize_url(page_url, raw_value)
            if normalized and is_probably_video_url(normalized, page_host):
                found.append(normalized)

    for match in URL_REGEX.findall(html_text):
        normalized = normalize_url(page_url, match)
        if normalized and is_probably_video_url(normalized, page_host):
            found.append(normalized)

    unique: list[str] = []
    seen: set[str] = set()
    for item in found:
        if item in seen:
            continue
        seen.add(item)
        unique.append(item)
    return unique


def discover_video_targets(page_url: str, timeout: int = 20) -> list[str]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
    }
    response = requests.get(page_url, timeout=timeout, headers=headers)
    response.raise_for_status()
    html_text = response.text

    targets = extract_candidates(page_url=page_url, html_text=html_text)
    if not targets:
        return [page_url]
    return targets
