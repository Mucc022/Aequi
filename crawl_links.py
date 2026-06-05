from __future__ import annotations

import argparse
import csv
import json
import re
import time
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

PAGE_EXTENSIONS = {"", ".html", ".htm", ".php", ".asp", ".aspx", ".jsp"}
VIDEO_EXTENSIONS = {".mp4", ".m3u8", ".webm", ".mov", ".mkv", ".avi", ".flv", ".m4v"}
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".wav", ".aac", ".flac", ".ogg", ".opus", ".wma"}
DOCUMENT_EXTENSIONS = {".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".txt", ".srt", ".vtt"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp", ".ico", ".avif"}
ARCHIVE_EXTENSIONS = {".zip", ".rar", ".7z", ".tar", ".gz"}
SCRIPT_EXTENSIONS = {".js", ".mjs"}
STYLE_EXTENSIONS = {".css", ".woff", ".woff2", ".ttf", ".otf"}
VIDEO_HOST_HINTS = {
    "youtube.com",
    "youtu.be",
    "vimeo.com",
    "player.vimeo.com",
    "bilibili.com",
    "youku.com",
    "dailymotion.com",
    "video.wixstatic.com",
}


@dataclass
class LinkRecord:
    url: str
    category: str
    tags: list[str]
    text: str
    source_url: str
    source_title: str
    attr: str
    depth: int
    same_host: bool
    status_code: int | None = None
    content_type: str = ""


def normalize_url(base_url: str, raw_url: str) -> str | None:
    value = (raw_url or "").strip().strip("\"'")
    if not value or value.startswith(("#", "javascript:", "data:", "mailto:", "tel:")):
        return None

    absolute = urljoin(base_url, value)
    parsed = urlparse(absolute)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None

    parsed = parsed._replace(fragment="")
    return urlunparse(parsed)


def browser_url(url: str) -> str:
    parsed = urlparse(url)
    path = quote(requests.utils.unquote(parsed.path), safe="/%:@")
    query = quote(requests.utils.unquote(parsed.query), safe="=&?/:@%+,$;")
    return urlunparse(parsed._replace(path=path, query=query))


def extension_of(url: str) -> str:
    path = urlparse(url).path.lower()
    dot = path.rfind(".")
    slash = path.rfind("/")
    if dot <= slash:
        return ""
    return path[dot:]


def classify_url(url: str, root_host: str, content_type: str = "") -> tuple[str, list[str]]:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    ext = extension_of(url)
    ct = content_type.lower()
    tags: list[str] = []

    tags.append("internal" if host == root_host else "external")
    if host.startswith("www."):
        tags.append(host[4:])
    else:
        tags.append(host)

    if ext:
        tags.append(ext.removeprefix("."))

    if ext in VIDEO_EXTENSIONS or any(hint in host for hint in VIDEO_HOST_HINTS) or "video/" in ct:
        return "video", tags
    if ext in AUDIO_EXTENSIONS or "audio/" in ct:
        return "audio", tags
    if ext in DOCUMENT_EXTENSIONS or any(token in ct for token in ("pdf", "msword", "presentation", "spreadsheet")):
        return "document", tags
    if ext in IMAGE_EXTENSIONS or "image/" in ct:
        return "image", tags
    if ext in ARCHIVE_EXTENSIONS:
        return "archive", tags
    if ext in SCRIPT_EXTENSIONS or "javascript" in ct:
        return "script", tags
    if ext in STYLE_EXTENSIONS or "text/css" in ct or "font/" in ct:
        return "style", tags
    if ext in PAGE_EXTENSIONS or "text/html" in ct:
        return ("page_internal" if host == root_host else "page_external"), tags
    return ("other_internal" if host == root_host else "other_external"), tags


def page_title(html_text: str) -> str:
    soup = BeautifulSoup(html_text, "html.parser")
    title = soup.find("title")
    if not title:
        return ""
    return " ".join(title.get_text(" ", strip=True).split())


def compact_text(value: str, max_len: int = 160) -> str:
    text = " ".join((value or "").split())
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "..."


def infer_course_subject(page_url: str, page_title_text: str = "") -> str:
    path_text = requests.utils.unquote(urlparse(page_url).path.strip("/"))
    title_text = requests.utils.unquote(page_title_text)
    for pattern in (r"学期([^/\s]+)$", r"\d{4}[-年]\d{1,2}[-月]\d{1,2}[日-]?\s*([^/\s]+)"):
        match = re.search(pattern, path_text)
        if match:
            return match.group(1).strip(" -_")
    text = f"{path_text} {title_text}"
    for pattern in (r"学期([^/\s]+)", r"\d{4}[-年]\d{1,2}[-月]\d{1,2}[日-]?\s*([^/\s]+)"):
        match = re.search(pattern, text)
        if match:
            return match.group(1).strip(" -_")
    return ""


def extract_wix_course_links(page_url: str, html_text: str, subject: str = "") -> list[tuple[str, str]]:
    try:
        from media2text.scraper_engine import extract_thunderbolt_payload_urls
    except Exception:
        return []

    payload_urls = extract_thunderbolt_payload_urls(page_url, html_text)
    if not payload_urls:
        return []

    session = requests.Session()
    session.headers.update({"User-Agent": DEFAULT_USER_AGENT, "Referer": page_url})
    root = f"{urlparse(page_url).scheme}://{urlparse(page_url).netloc}"
    found: dict[str, tuple[str, str]] = {}
    title_pattern = re.compile(r"\b20\d{2}[-年]\d{1,2}[-月]\d{1,2}")

    def collect_from_page_list(page_list: object) -> None:
        if not isinstance(page_list, dict):
            return
        for slug, item in page_list.items():
            if not isinstance(item, dict):
                continue
            title = compact_text(str(item.get("title") or ""), max_len=120).strip()
            if not title or not title_pattern.search(title):
                continue
            if subject and subject not in title and subject not in str(slug):
                continue
            link = normalize_url(root, "/" + str(slug).strip("/"))
            if link:
                found[title] = (browser_url(link), title)

    def walk(value: object) -> None:
        if isinstance(value, dict):
            if "pageList" in value:
                collect_from_page_list(value.get("pageList"))
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    for payload_url in payload_urls[:12]:
        try:
            payload = session.get(payload_url, timeout=20).json()
        except Exception:
            continue
        walk(payload)

    def sort_key(item: tuple[str, str]) -> str:
        title = item[1]
        match = re.search(r"(20\d{2})[-年](\d{1,2})[-月](\d{1,2})", title)
        if not match:
            return title
        return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}-{title}"

    return sorted(found.values(), key=sort_key)


def extract_links(page_url: str, html_text: str) -> list[tuple[str, str, str]]:
    soup = BeautifulSoup(html_text, "html.parser")
    links: list[tuple[str, str, str]] = []
    attrs = ("href", "src", "data-src", "data-url")

    for tag in soup.find_all(True):
        for attr in attrs:
            raw = tag.get(attr)
            if not raw:
                continue
            normalized = normalize_url(page_url, str(raw))
            if not normalized:
                continue
            text = " ".join(tag.get_text(" ", strip=True).split())
            if not text:
                text = str(tag.get("alt") or tag.get("title") or "").strip()
            links.append((normalized, compact_text(text), attr))

    return links


def fetch(session: requests.Session, url: str, timeout: int) -> requests.Response:
    response = session.get(url, timeout=timeout, allow_redirects=True)
    response.raise_for_status()
    return response


def crawl_links(
    root_url: str,
    max_depth: int,
    max_pages: int,
    timeout: int,
    delay: float,
    include_external_pages: bool,
    discover_video_candidates: bool,
) -> list[LinkRecord]:
    root_url = normalize_url(root_url, root_url) or root_url
    root_host = urlparse(root_url).netloc.lower()
    session = requests.Session()
    session.headers.update({"User-Agent": DEFAULT_USER_AGENT})

    queue: deque[tuple[str, int]] = deque([(root_url, 0)])
    queued = {root_url}
    visited_pages: set[str] = set()
    seen_records: set[tuple[str, str]] = set()
    records: list[LinkRecord] = []

    while queue and len(visited_pages) < max_pages:
        page_url, depth = queue.popleft()
        if page_url in visited_pages:
            continue

        try:
            response = fetch(session, page_url, timeout)
        except Exception as exc:  # noqa: BLE001
            records.append(
                LinkRecord(
                    url=page_url,
                    category="fetch_failed",
                    tags=["error"],
                    text=str(exc),
                    source_url=page_url,
                    source_title="",
                    attr="page",
                    depth=depth,
                    same_host=urlparse(page_url).netloc.lower() == root_host,
                )
            )
            continue

        visited_pages.add(page_url)
        content_type = response.headers.get("content-type", "")
        source_title = page_title(response.text) if "text/html" in content_type.lower() else ""
        page_category, page_tags = classify_url(page_url, root_host, content_type)
        records.append(
            LinkRecord(
                url=page_url,
                category=page_category,
                tags=page_tags,
                text="",
                source_url=page_url,
                source_title=source_title,
                attr="page",
                depth=depth,
                same_host=urlparse(page_url).netloc.lower() == root_host,
                status_code=response.status_code,
                content_type=content_type,
            )
        )

        if "text/html" not in content_type.lower():
            continue

        subject = infer_course_subject(page_url, source_title)
        for target_url, title in extract_wix_course_links(page_url, response.text, subject=subject):
            record_key = (target_url, source_url_key(page_url, "course"))
            if record_key in seen_records:
                continue
            seen_records.add(record_key)
            records.append(
                LinkRecord(
                    url=target_url,
                    category="course",
                    tags=["internal", root_host, subject or "course"],
                    text=title,
                    source_url=page_url,
                    source_title=source_title,
                    attr="course",
                    depth=depth + 1,
                    same_host=True,
                )
            )

        if discover_video_candidates:
            try:
                from media2text.scraper_engine import discover_targets

                video_candidates = discover_targets(
                    pages=[page_url],
                    timeout=timeout,
                    user_agent=DEFAULT_USER_AGENT,
                    always_try_page_url=False,
                )
            except Exception:
                video_candidates = set()

            for target_url in sorted(video_candidates):
                category, tags = classify_url(target_url, root_host)
                if category != "video":
                    continue
                record_key = (target_url, source_url_key(page_url, "video-candidate"))
                if record_key in seen_records:
                    continue
                seen_records.add(record_key)
                records.append(
                    LinkRecord(
                        url=target_url,
                        category=category,
                        tags=tags + ["deep-candidate"],
                        text="",
                        source_url=page_url,
                        source_title=source_title,
                        attr="video-candidate",
                        depth=depth + 1,
                        same_host=urlparse(target_url).netloc.lower() == root_host,
                    )
                )

        for target_url, text, attr in extract_links(page_url, response.text):
            target_host = urlparse(target_url).netloc.lower()
            same_host = target_host == root_host
            category, tags = classify_url(target_url, root_host)
            record_key = (target_url, source_url_key(page_url, attr))

            if record_key not in seen_records:
                seen_records.add(record_key)
                records.append(
                    LinkRecord(
                        url=target_url,
                        category=category,
                        tags=tags,
                        text=text,
                        source_url=page_url,
                        source_title=source_title,
                        attr=attr,
                        depth=depth + 1,
                        same_host=same_host,
                    )
                )

            should_crawl = category == "page_internal" or (include_external_pages and category == "page_external")
            if should_crawl and depth < max_depth and target_url not in queued and target_url not in visited_pages:
                queued.add(target_url)
                queue.append((target_url, depth + 1))

        if delay > 0:
            time.sleep(delay)

    return records


def source_url_key(source_url: str, attr: str) -> str:
    return f"{source_url}#{attr}"


def write_outputs(records: list[LinkRecord], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = [asdict(record) for record in records]

    (output_dir / "links.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    with (output_dir / "links.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        fieldnames = list(rows[0].keys()) if rows else [field.name for field in LinkRecord.__dataclass_fields__.values()]
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            row = dict(row)
            row["tags"] = ",".join(row["tags"])
            writer.writerow(row)

    by_category: dict[str, list[LinkRecord]] = {}
    for record in records:
        by_category.setdefault(record.category, []).append(record)

    lines: list[str] = []
    for category in sorted(by_category):
        lines.append(f"## {category} ({len(by_category[category])})")
        for record in by_category[category]:
            label = f" - {record.text}" if record.text else ""
            lines.append(f"{record.url}{label}")
        lines.append("")
    (output_dir / "links_by_category.txt").write_text("\n".join(lines), encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Crawl a parent page and export classified links.")
    parser.add_argument("url", help="Parent URL to crawl")
    parser.add_argument("--out", default="outputs/link_crawl", help="Output directory")
    parser.add_argument("--max-depth", type=int, default=2, help="How many internal page levels to crawl")
    parser.add_argument("--max-pages", type=int, default=200, help="Maximum pages to fetch")
    parser.add_argument("--timeout", type=int, default=20, help="Request timeout in seconds")
    parser.add_argument("--delay", type=float, default=0.2, help="Delay between fetched pages")
    parser.add_argument("--include-external-pages", action="store_true", help="Also crawl external HTML pages")
    parser.add_argument(
        "--no-video-candidates",
        action="store_true",
        help="Do not run Aequora's deeper video candidate discovery on each fetched page",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    records = crawl_links(
        root_url=args.url,
        max_depth=max(0, args.max_depth),
        max_pages=max(1, args.max_pages),
        timeout=max(1, args.timeout),
        delay=max(0.0, args.delay),
        include_external_pages=bool(args.include_external_pages),
        discover_video_candidates=not bool(args.no_video_candidates),
    )
    output_dir = Path(args.out).expanduser().resolve()
    write_outputs(records, output_dir)
    print(f"Done. links={len(records)} output={output_dir}")
    print(f"- {output_dir / 'links.json'}")
    print(f"- {output_dir / 'links.csv'}")
    print(f"- {output_dir / 'links_by_category.txt'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
