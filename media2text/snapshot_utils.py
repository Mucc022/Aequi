from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

TIMESTAMP_PATTERN = re.compile(r"(?<!\d)(?:\d{1,2}:)?\d{1,2}:\d{2}(?:[.,]\d{1,3})?(?!\d)")


def _no_window_kwargs() -> dict:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


@dataclass
class TimePoint:
    seconds: float
    reason: str = ""


def parse_timestamp_to_seconds(value: str) -> float:
    text = value.strip().replace(",", ".")
    parts = text.split(":")
    if len(parts) == 2:
        hours = 0
        minutes = int(parts[0])
        seconds = float(parts[1])
    elif len(parts) == 3:
        hours = int(parts[0])
        minutes = int(parts[1])
        seconds = float(parts[2])
    else:
        raise ValueError(f"Invalid timestamp: {value}")
    return hours * 3600 + minutes * 60 + seconds


def seconds_to_timestamp(value: float) -> str:
    total_ms = int(round(max(value, 0) * 1000))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def parse_srt_entries(srt_text: str) -> list[TimePoint]:
    entries: list[TimePoint] = []
    blocks = re.split(r"\n\s*\n", srt_text.strip(), flags=re.MULTILINE)
    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if len(lines) < 2:
            continue
        time_line = next((line for line in lines if "-->" in line), None)
        if not time_line:
            continue
        left = time_line.split("-->", 1)[0].strip()
        text_lines = [line for line in lines if line != time_line and not line.isdigit()]
        text = " ".join(text_lines).strip()
        try:
            sec = parse_timestamp_to_seconds(left)
        except Exception:
            continue
        entries.append(TimePoint(seconds=sec, reason=text))
    return entries


def _extract_from_json_obj(obj, collector: list[TimePoint]) -> None:
    if isinstance(obj, dict):
        if "timestamps" in obj:
            _extract_from_json_obj(obj["timestamps"], collector)
        for key in ("time", "timestamp", "start", "start_time"):
            if key in obj and isinstance(obj[key], str):
                try:
                    sec = parse_timestamp_to_seconds(obj[key])
                except Exception:
                    sec = None
                if sec is not None:
                    reason = ""
                    for r_key in ("reason", "note", "summary", "desc", "description"):
                        if isinstance(obj.get(r_key), str):
                            reason = obj[r_key].strip()
                            break
                    collector.append(TimePoint(seconds=sec, reason=reason))
                    break
        for value in obj.values():
            _extract_from_json_obj(value, collector)
    elif isinstance(obj, list):
        for item in obj:
            _extract_from_json_obj(item, collector)
    elif isinstance(obj, str):
        for match in TIMESTAMP_PATTERN.findall(obj):
            try:
                collector.append(TimePoint(seconds=parse_timestamp_to_seconds(match)))
            except Exception:
                continue


def extract_timepoints_from_ai_output(text: str) -> list[TimePoint]:
    points: list[TimePoint] = []

    try:
        parsed = json.loads(text)
        _extract_from_json_obj(parsed, points)
    except Exception:
        pass

    if not points:
        for match in TIMESTAMP_PATTERN.finditer(text):
            ts = match.group(0)
            try:
                points.append(TimePoint(seconds=parse_timestamp_to_seconds(ts)))
            except Exception:
                continue

    return points


def dedupe_timepoints(
    points: Sequence[TimePoint],
    min_gap_seconds: float = 8.0,
    max_points: int | None = None,
) -> list[TimePoint]:
    sorted_points = sorted(points, key=lambda p: p.seconds)
    filtered: list[TimePoint] = []
    for p in sorted_points:
        if not filtered:
            filtered.append(p)
            continue
        if p.seconds - filtered[-1].seconds >= min_gap_seconds:
            filtered.append(p)

    if max_points is not None:
        filtered = filtered[:max_points]
    return filtered


def clamp_timepoints(points: Iterable[TimePoint], duration_seconds: float) -> list[TimePoint]:
    clamped: list[TimePoint] = []
    upper = max(0.0, duration_seconds - 0.2)
    for p in points:
        sec = min(max(0.0, p.seconds), upper)
        clamped.append(TimePoint(seconds=sec, reason=p.reason))
    return clamped


def get_media_duration_seconds(media_path: Path, ffprobe_bin: str = "ffprobe") -> float:
    cmd = [
        ffprobe_bin,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(media_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False, **_no_window_kwargs())
    if result.returncode != 0:
        raise RuntimeError((result.stderr or "ffprobe failed").strip())
    try:
        return float(result.stdout.strip())
    except Exception as exc:
        raise RuntimeError("Unable to parse media duration from ffprobe output") from exc


def build_ai_prompt_from_srt(
    srt_text: str,
    max_points: int = 15,
    max_lines: int = 500,
) -> str:
    entries = parse_srt_entries(srt_text)
    clipped = entries[:max_lines]

    timeline_lines = []
    for item in clipped:
        timeline_lines.append(f"- {seconds_to_timestamp(item.seconds)} | {item.reason}")

    timeline_block = "\n".join(timeline_lines) if timeline_lines else "(No transcript lines found)"

    return (
        "You are selecting key visual moments in a course video.\n"
        "Given the timeline transcript below, choose the most important timestamps for screenshots.\n"
        f"Return at most {max_points} points. Avoid near-duplicates.\n\n"
        "Output STRICT JSON only, no markdown:\n"
        "{\n"
        "  \"timestamps\": [\n"
        "    {\"time\": \"00:12:34\", \"reason\": \"why this moment matters\"}\n"
        "  ]\n"
        "}\n\n"
        "Timeline transcript:\n"
        f"{timeline_block}\n"
    )


def capture_snapshots(
    video_path: Path,
    points: Sequence[TimePoint],
    output_dir: Path,
    ffmpeg_bin: str = "ffmpeg",
) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    outputs: list[Path] = []

    for idx, point in enumerate(points, start=1):
        ts_text = seconds_to_timestamp(point.seconds).replace(":", "-")
        img_path = output_dir / f"{idx:03d}_{ts_text}.jpg"

        cmd = [
            ffmpeg_bin,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{point.seconds:.3f}",
            "-i",
            str(video_path),
            "-frames:v",
            "1",
            "-q:v",
            "2",
            str(img_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=False, **_no_window_kwargs())
        if result.returncode != 0:
            stderr = (result.stderr or "").strip() or "Unknown ffmpeg error"
            raise RuntimeError(f"Failed to capture at {seconds_to_timestamp(point.seconds)}: {stderr}")
        outputs.append(img_path)

    manifest = output_dir / "snapshot_manifest.json"
    manifest_data = {
        "video": str(video_path),
        "count": len(outputs),
        "snapshots": [
            {
                "file": p.name,
                "time": seconds_to_timestamp(tp.seconds),
                "reason": tp.reason,
            }
            for p, tp in zip(outputs, points)
        ],
    }
    manifest.write_text(json.dumps(manifest_data, ensure_ascii=False, indent=2), encoding="utf-8")
    return outputs
