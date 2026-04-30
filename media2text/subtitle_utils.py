from __future__ import annotations

import re
from pathlib import Path

TIMECODE_SRT = re.compile(r"\d{2}:\d{2}:\d{2},\d{3}\s+-->\s+\d{2}:\d{2}:\d{2},\d{3}")
TIMECODE_VTT = re.compile(r"\d{2}:\d{2}:\d{2}\.\d{3}\s+-->\s+\d{2}:\d{2}:\d{2}\.\d{3}")
TAG_RE = re.compile(r"<[^>]+>")


def _clean_line(line: str) -> str:
    return TAG_RE.sub("", line).strip()


def srt_text_to_plain_text(srt_text: str) -> str:
    lines: list[str] = []
    for raw in srt_text.splitlines():
        line = raw.strip()
        if not line or line.isdigit() or "-->" in line:
            continue
        cleaned = _clean_line(line)
        if cleaned:
            lines.append(cleaned)
    return "\n".join(lines)


def vtt_to_srt(vtt_text: str) -> str:
    out_lines: list[str] = []
    index = 0
    buffer: list[str] = []

    def flush() -> None:
        nonlocal index, buffer
        if not buffer:
            return
        time_line = ""
        texts: list[str] = []
        for line in buffer:
            if "-->" in line and not time_line:
                time_line = line.replace(".", ",")
            else:
                cleaned = _clean_line(line)
                if cleaned:
                    texts.append(cleaned)
        if time_line and texts:
            index += 1
            out_lines.append(str(index))
            out_lines.append(time_line)
            out_lines.extend(texts)
            out_lines.append("")
        buffer = []

    for raw in vtt_text.splitlines():
        line = raw.rstrip("\n")
        if line.strip().upper().startswith("WEBVTT"):
            continue
        if not line.strip():
            flush()
            continue
        if line.strip().isdigit():
            continue
        buffer.append(line)
    flush()

    return "\n".join(out_lines).strip() + "\n"


def subtitle_file_to_srt_and_text(subtitle_path: Path) -> tuple[str, str]:
    raw = subtitle_path.read_text(encoding="utf-8", errors="replace")
    ext = subtitle_path.suffix.lower()

    if ext == ".srt" or TIMECODE_SRT.search(raw):
        srt_text = raw
    elif ext == ".vtt" or TIMECODE_VTT.search(raw):
        srt_text = vtt_to_srt(raw)
    else:
        # best effort for unknown subtitle-like text
        if "-->" in raw:
            srt_text = raw.replace(".", ",")
        else:
            srt_text = "1\n00:00:00,000 --> 00:00:00,500\n" + raw.strip() + "\n"

    plain_text = srt_text_to_plain_text(srt_text)
    return srt_text, plain_text
