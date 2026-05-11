from __future__ import annotations

import html
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

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
    ".m4a",
    ".mp3",
    ".m3u8",
    ".webm",
    ".mov",
    ".mkv",
    ".avi",
    ".flv",
)
DOCUMENT_EXTENSIONS = (".pdf",)

URL_REGEX = re.compile(r"https?://[^\s\"'<>\\]+", re.IGNORECASE)
ESCAPED_URL_REGEX = re.compile(r"https?:\\\\/\\\\/[^\s\"'<>]+", re.IGNORECASE)


def normalize_url(base_url: str, raw_url: str) -> str | None:
    if not raw_url:
        return None
    cleaned = raw_url.strip().strip("\"'")
    if cleaned.startswith(("http://", "https://", "//")):
        cleaned = cleaned.replace("&amp;", "&")
    else:
        cleaned = html.unescape(cleaned)
    if any(token in cleaned for token in ("${", "`", "{", "}")):
        return None
    if not cleaned or cleaned.startswith("javascript:") or cleaned.startswith("data:"):
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
    if ".m3u8" in path:
        return True
    if "facebook.com" in host:
        return any(token in path for token in ("/watch", "/videos", "video.php", "/reel"))
    if "youtube.com" in host or "youtu.be" in host:
        if any(token in path for token in ("/watch", "/shorts", "/live", "/embed")):
            return True
        return "v=" in query
    if "vimeo.com" in host:
        return any(token in path for token in ("/video/", "/channels/", "/ondemand/")) or path.strip("/").isdigit()
    if any(hint in host for hint in VIDEO_HOST_HINTS):
        return True

    same_host = host == page_host
    if same_host and any(token in path for token in ("/video", "/watch", "/embed", "/player")):
        return True

    return False


def is_probably_document_url(url: str) -> bool:
    parsed = urlparse(url)
    path = parsed.path.lower()
    return any(path.endswith(ext) for ext in DOCUMENT_EXTENSIONS)


def extract_youtube_video_id(url: str) -> str | None:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.strip("/")
    query = parse_qs(parsed.query)

    if "youtu.be" in host:
        vid = path.split("/", 1)[0].strip()
        return vid or None

    if "youtube.com" in host:
        if path == "watch":
            vid = query.get("v", [""])[0].strip()
            return vid or None
        if path.startswith("embed/"):
            vid = path.split("/", 1)[1].split("/", 1)[0].strip()
            return vid or None
        if path.startswith("shorts/"):
            vid = path.split("/", 1)[1].split("/", 1)[0].strip()
            return vid or None
    return None


def canonicalize_video_candidate(url: str) -> str | None:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.lower()

    if host == "s.youtube.com":
        return None
    if "youtube.com" in host and path.startswith("/api/"):
        return None

    yt_id = extract_youtube_video_id(url)
    if yt_id:
        return f"https://www.youtube.com/watch?v={yt_id}"

    if "youtube.com" in host and path == "/watch":
        return None

    return url


def extract_candidates(page_url: str, html_text: str) -> set[str]:
    soup = BeautifulSoup(html_text, "html.parser")
    page_host = urlparse(page_url).netloc.lower()
    found: set[str] = set()

    attrs = ("src", "href", "data-src", "data-url", "content")
    for tag in soup.find_all(True):
        for attr in attrs:
            raw_value = tag.get(attr)
            if not raw_value:
                continue
            normalized = normalize_url(page_url, raw_value)
            if normalized and (is_probably_video_url(normalized, page_host) or is_probably_document_url(normalized)):
                canonical = canonicalize_video_candidate(normalized)
                if canonical:
                    found.add(canonical)

    for match in URL_REGEX.findall(html_text):
        normalized = normalize_url(page_url, match)
        if normalized and (is_probably_video_url(normalized, page_host) or is_probably_document_url(normalized)):
            canonical = canonicalize_video_candidate(normalized)
            if canonical:
                found.add(canonical)

    return found


def iter_strings(value: object) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from iter_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from iter_strings(item)


def extract_thunderbolt_payload_urls(page_url: str, html_text: str) -> list[str]:
    soup = BeautifulSoup(html_text, "html.parser")
    urls: list[str] = []
    for link in soup.find_all("link"):
        href = link.get("href")
        if not href:
            continue
        if "siteassets.parastorage.com/pages/pages/thunderbolt" in href and "pageId=" in href:
            normalized = normalize_url(page_url, href)
            if normalized:
                urls.append(normalized)
    return sorted(set(urls))


def extract_candidates_from_thunderbolt_payloads(
    page_url: str,
    payload_urls: list[str],
    timeout: int,
    user_agent: str,
) -> set[str]:
    page_host = urlparse(page_url).netloc.lower()
    headers = {"User-Agent": user_agent, "Referer": page_url}
    found: set[str] = set()

    for payload_url in payload_urls[:12]:
        try:
            response = requests.get(payload_url, timeout=timeout, headers=headers)
            response.raise_for_status()
            payload = response.json()
            body = response.text
        except Exception as exc:  # noqa: BLE001
            logger.debug("Thunderbolt JSON fetch failed: %s (%s)", payload_url, exc)
            continue

        for match in ESCAPED_URL_REGEX.findall(body):
            decoded = match.replace("\\/", "/")
            normalized = normalize_url(page_url, decoded)
            if normalized and (is_probably_video_url(normalized, page_host) or is_probably_document_url(normalized)):
                canonical = canonicalize_video_candidate(normalized)
                if canonical:
                    found.add(canonical)

        for match in URL_REGEX.findall(body):
            normalized = normalize_url(page_url, match)
            if normalized and (is_probably_video_url(normalized, page_host) or is_probably_document_url(normalized)):
                canonical = canonicalize_video_candidate(normalized)
                if canonical:
                    found.add(canonical)

        for text in iter_strings(payload):
            normalized_text = text.replace("\\/", "/")
            if "http" not in normalized_text and "//" not in normalized_text:
                continue
            for match in URL_REGEX.findall(normalized_text):
                normalized = normalize_url(page_url, match)
                if normalized and (is_probably_video_url(normalized, page_host) or is_probably_document_url(normalized)):
                    canonical = canonicalize_video_candidate(normalized)
                    if canonical:
                        found.add(canonical)
            normalized_direct = normalize_url(page_url, normalized_text)
            if normalized_direct and (is_probably_video_url(normalized_direct, page_host) or is_probably_document_url(normalized_direct)):
                canonical = canonicalize_video_candidate(normalized_direct)
                if canonical:
                    found.add(canonical)

    return found


def fetch_page(url: str, timeout: int, user_agent: str) -> str:
    headers = {"User-Agent": user_agent}
    response = requests.get(url, timeout=timeout, headers=headers)
    response.raise_for_status()
    return response.text


def discover_targets(
    pages: list[str],
    timeout: int,
    user_agent: str,
    always_try_page_url: bool,
) -> set[str]:
    all_targets: set[str] = set()

    for page in pages:
        if urlparse(page).path.lower().endswith(VIDEO_EXTENSIONS + DOCUMENT_EXTENSIONS):
            logger.info("检测到直接资源链接，跳过网页扫描: %s", page)
            all_targets.add(page)
            continue
        logger.info("Scanning page: %s", page)
        try:
            html_text = fetch_page(page, timeout=timeout, user_agent=user_agent)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Page fetch failed: %s (%s)", page, exc)
            continue

        candidates = extract_candidates(page, html_text)
        payload_urls = extract_thunderbolt_payload_urls(page, html_text)
        if payload_urls:
            logger.info("Found Thunderbolt payloads: %d", len(payload_urls))
            deep_candidates = extract_candidates_from_thunderbolt_payloads(
                page_url=page,
                payload_urls=payload_urls,
                timeout=timeout,
                user_agent=user_agent,
            )
            candidates.update(deep_candidates)

        if candidates:
            logger.info("Candidates found: %d", len(candidates))
            all_targets.update(candidates)
        else:
            logger.info("No clear candidates, still try page URL directly")
            all_targets.add(page)

        if always_try_page_url:
            all_targets.add(page)

    return all_targets


def canonicalize_url(url: str) -> str:
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    host = parsed.netloc.lower()
    path = parsed.path or "/"
    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)))
    return urlunparse((scheme, host, path, "", query, ""))


def dedup_key(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.strip("/")
    qs = parse_qs(parsed.query)

    if "youtube.com" in host:
        vid = qs.get("v", [""])[0].strip()
        if vid:
            return f"youtube:{vid}"
    if "youtu.be" in host:
        vid = path.split("/")[0].strip()
        if vid:
            return f"youtube:{vid}"
    if "vimeo.com" in host:
        if path:
            return f"vimeo:{path}"

    return f"url:{canonicalize_url(url)}"


def load_seen_archive(path: Path) -> set[str]:
    seen: set[str] = set()
    if not path.exists():
        return seen

    for line in path.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
        raw = line.strip()
        if not raw:
            continue
        if raw.startswith("{"):
            try:
                obj = json.loads(raw)
                key = str(obj.get("key", "")).strip()
                if key:
                    seen.add(key)
                continue
            except json.JSONDecodeError:
                pass

        parts = raw.split()
        if len(parts) == 2 and parts[0].lower() == "youtube":
            seen.add(f"youtube:{parts[1].strip()}")
            continue

        if "\t" in raw:
            key = raw.split("\t", 1)[0].strip()
            if key:
                seen.add(key)
            continue

        seen.add(raw)

    return seen


def append_seen_archive(path: Path, key: str, url: str) -> None:
    entry = {
        "key": key,
        "url": url,
        "downloaded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def parse_indices(raw: str, max_index: int) -> set[int]:
    selected: set[int] = set()
    chunks = [c.strip() for c in raw.split(",") if c.strip()]
    for chunk in chunks:
        if "-" in chunk:
            start_s, end_s = chunk.split("-", 1)
            start = int(start_s.strip())
            end = int(end_s.strip())
            if start > end:
                start, end = end, start
            for i in range(start, end + 1):
                if 1 <= i <= max_index:
                    selected.add(i)
        else:
            i = int(chunk)
            if 1 <= i <= max_index:
                selected.add(i)
    return selected
