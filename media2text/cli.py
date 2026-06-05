from __future__ import annotations

import argparse
import logging
import os
import sys
import uuid
from pathlib import Path

from .config import AppConfig, ensure_output_root, load_config
from .ffmpeg_utils import check_ffmpeg_available
from .io_utils import is_direct_media_url, is_url
from .orchestrator import MediaOrchestrator, retry_from_failed_log
from .scraper_engine import dedup_key, discover_targets, load_seen_archive, parse_indices
from .snapshot_utils import (
    build_ai_prompt_from_srt,
    capture_snapshots,
    clamp_timepoints,
    dedupe_timepoints,
    extract_timepoints_from_ai_output,
    get_media_duration_seconds,
)


def setup_logging() -> None:
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    os.environ.setdefault("PYTHONUTF8", "1")
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Aequora media organizer")
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", help="Process input paths or URLs")
    run_parser.add_argument("--input", action="append", required=True, help="Path/URL input, repeatable")
    run_parser.add_argument("--out", default=None, help="Output root directory")
    run_parser.add_argument("--config", default="config.json", help="Config JSON path")
    run_parser.add_argument(
        "--result-index",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Prefix output filenames with a running number",
    )
    run_parser.add_argument("--model", default=None, help="Override whisper model")
    run_parser.add_argument("--language", default=None, help="Override whisper language (e.g. zh/en/auto)")
    run_parser.add_argument("--candidate-mode", choices=["select", "auto"], default=None)
    run_parser.add_argument("--download-archive", default=None)
    run_parser.add_argument("--always-try-page", action=argparse.BooleanOptionalAction, default=None)
    run_parser.add_argument("--quality", default=None)
    run_parser.add_argument("--keep-original", action=argparse.BooleanOptionalAction, default=None)
    run_parser.add_argument("--save-metadata", action=argparse.BooleanOptionalAction, default=None)
    run_parser.add_argument("--export-audio", action=argparse.BooleanOptionalAction, default=None)
    run_parser.add_argument("--audio-format", choices=["mp3", "wav"], default=None)
    run_parser.add_argument("--subtitle-format", choices=["srt", "vtt", "ass"], default=None)
    run_parser.add_argument(
        "--subtitle-priority",
        choices=["subtitle_first_then_whisper", "platform_only", "whisper_only", "skip_text"],
        default=None,
    )
    run_parser.add_argument("--text-output", choices=["txt+srt", "txt", "srt", "none"], default=None)
    run_parser.add_argument("--prefer-compatible-codecs", action=argparse.BooleanOptionalAction, default=None)
    run_parser.add_argument("--allow-separate-streams", action=argparse.BooleanOptionalAction, default=None)
    run_parser.add_argument("--js-runtime", action="append", default=None)
    run_parser.add_argument("--remote-component", action="append", default=None)
    run_parser.add_argument("--request-timeout", type=int, default=None)
    run_parser.add_argument("--user-agent", default=None)
    run_parser.add_argument("--force-key", action="append", default=None)
    run_parser.add_argument("--source-page", action="append", default=None, help="Media/source mapping as MEDIA_URL||PAGE_URL")
    run_parser.add_argument("--cookies-file", default=None)
    run_parser.add_argument("--cookies-from-browser", choices=["chrome", "edge", "firefox"], default=None)
    run_parser.add_argument("--run-id", default=None)

    retry_parser = sub.add_parser("retry-failed", help="Retry failed tasks from failed_tasks.jsonl")
    retry_parser.add_argument("--failed-log", required=True, help="Path to failed_tasks.jsonl")
    retry_parser.add_argument("--out", default=None, help="Output root directory")
    retry_parser.add_argument("--config", default="config.json", help="Config JSON path")
    retry_parser.add_argument("--model", default=None, help="Override whisper model")
    retry_parser.add_argument("--language", default=None, help="Override whisper language")
    retry_parser.add_argument("--run-id", default=None)

    snap_prompt = sub.add_parser("snapshot-make-prompt", help="Build AI prompt from SRT transcript")
    snap_prompt.add_argument("--video", required=True, help="Video file path")
    snap_prompt.add_argument("--srt", default=None, help="SRT path (default: same stem as video)")
    snap_prompt.add_argument("--out", required=True, help="Prompt output txt path")
    snap_prompt.add_argument("--max-shots", type=int, default=15, help="Max key moments")

    snap_capture = sub.add_parser("snapshot-capture", help="Capture keyframe screenshots from AI output")
    snap_capture.add_argument("--video", required=True, help="Video file path")
    snap_capture.add_argument("--ai-output", required=True, help="AI output file (json/txt with timestamps)")
    snap_capture.add_argument(
        "--out-dir",
        default=None,
        help="Snapshot output directory (default: <video_dir>/snapshots/<video_stem>)",
    )
    snap_capture.add_argument("--max-shots", type=int, default=15, help="Max screenshots")
    snap_capture.add_argument("--min-gap", type=float, default=8.0, help="Min seconds between screenshots")
    snap_capture.add_argument("--ffprobe-bin", default="ffprobe", help="ffprobe executable name/path")

    return parser


def apply_common_overrides(cfg: AppConfig, args: argparse.Namespace) -> None:
    if getattr(args, "model", None):
        cfg.whisper.model = args.model
    if getattr(args, "language", None):
        cfg.whisper.language = args.language


def apply_run_overrides(cfg: AppConfig, args: argparse.Namespace) -> None:
    apply_common_overrides(cfg, args)

    if args.candidate_mode:
        cfg.scraping.candidate_mode = args.candidate_mode
    if args.result_index is not None:
        cfg.include_result_index = bool(args.result_index)
    if args.download_archive:
        cfg.scraping.download_archive = args.download_archive
    if args.always_try_page is not None:
        cfg.scraping.always_try_page_url = bool(args.always_try_page)
    if args.request_timeout is not None and args.request_timeout > 0:
        cfg.scraping.request_timeout_seconds = int(args.request_timeout)
    if args.user_agent:
        cfg.scraping.user_agent = str(args.user_agent)
    if args.cookies_file:
        cfg.download.cookie_mode = "cookies_file"
        cfg.download.cookies_file = str(args.cookies_file)
    if args.cookies_from_browser:
        cfg.download.cookie_mode = "browser"
        cfg.download.cookies_browser = str(args.cookies_from_browser)

    if args.quality:
        cfg.download.quality = str(args.quality)
    if args.keep_original is not None:
        cfg.download.keep_original = bool(args.keep_original)
    if args.save_metadata is not None:
        cfg.download.save_metadata = bool(args.save_metadata)
    if args.export_audio is not None:
        cfg.download.export_audio = bool(args.export_audio)
    if args.audio_format:
        cfg.download.export_audio_format = str(args.audio_format)
    if args.subtitle_format:
        cfg.download.subtitle_output_format = str(args.subtitle_format)
    if args.subtitle_priority:
        cfg.subtitle_priority = str(args.subtitle_priority)
    if args.text_output:
        if args.text_output == "txt+srt":
            cfg.download.output_txt = True
            cfg.download.output_srt = True
        elif args.text_output == "txt":
            cfg.download.output_txt = True
            cfg.download.output_srt = False
        elif args.text_output == "srt":
            cfg.download.output_txt = False
            cfg.download.output_srt = True
        else:
            cfg.download.output_txt = False
            cfg.download.output_srt = False
    if args.prefer_compatible_codecs is not None:
        cfg.download.prefer_compatible_codecs = bool(args.prefer_compatible_codecs)
    if args.allow_separate_streams is not None:
        cfg.download.allow_separate_streams = bool(args.allow_separate_streams)
    if args.js_runtime:
        cfg.download.js_runtimes = [x.strip() for x in args.js_runtime if x.strip()]
    if args.remote_component:
        cfg.download.remote_components = [x.strip() for x in args.remote_component if x.strip()]


def parse_source_page_pairs(values: list[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in values or []:
        text = str(raw).strip()
        if "||" not in text:
            continue
        media_url, page_url = text.split("||", 1)
        media_url = media_url.strip()
        page_url = page_url.strip()
        if media_url and page_url:
            out[media_url] = page_url
    return out


def run_snapshot_make_prompt(args: argparse.Namespace) -> int:
    video_path = Path(args.video).expanduser().resolve()
    if not video_path.exists() or not video_path.is_file():
        logging.error("Video not found: %s", video_path)
        return 1

    srt_path = Path(args.srt).expanduser().resolve() if args.srt else video_path.with_suffix(".srt")
    if not srt_path.exists():
        logging.error("SRT not found: %s", srt_path)
        return 1

    prompt_out = Path(args.out).expanduser().resolve()
    srt_text = srt_path.read_text(encoding="utf-8", errors="replace")
    prompt = build_ai_prompt_from_srt(srt_text=srt_text, max_points=args.max_shots)
    prompt_out.parent.mkdir(parents=True, exist_ok=True)
    prompt_out.write_text(prompt, encoding="utf-8")
    logging.info("Prompt written: %s", prompt_out)
    return 0


def run_snapshot_capture(args: argparse.Namespace, ffmpeg_bin: str) -> int:
    video_path = Path(args.video).expanduser().resolve()
    if not video_path.exists() or not video_path.is_file():
        logging.error("Video not found: %s", video_path)
        return 1

    ai_output_path = Path(args.ai_output).expanduser().resolve()
    if not ai_output_path.exists() or not ai_output_path.is_file():
        logging.error("AI output file not found: %s", ai_output_path)
        return 1

    ai_text = ai_output_path.read_text(encoding="utf-8", errors="replace")
    raw_points = extract_timepoints_from_ai_output(ai_text)
    if not raw_points:
        logging.error("No valid timestamps found in AI output")
        return 1

    duration = get_media_duration_seconds(media_path=video_path, ffprobe_bin=args.ffprobe_bin)
    points = clamp_timepoints(raw_points, duration_seconds=duration)
    points = dedupe_timepoints(points, min_gap_seconds=args.min_gap, max_points=args.max_shots)
    if not points:
        logging.error("No timestamps left after filtering")
        return 1

    if args.out_dir:
        output_dir = Path(args.out_dir).expanduser().resolve()
    else:
        output_dir = (video_path.parent / "snapshots" / video_path.stem).resolve()

    shots = capture_snapshots(
        video_path=video_path,
        points=points,
        output_dir=output_dir,
        ffmpeg_bin=ffmpeg_bin,
    )
    logging.info("Captured %d snapshots -> %s", len(shots), output_dir)
    return 0


def _prompt_select_targets_for_url(url: str, targets: list[str], seen_keys: set[str]) -> tuple[list[str], set[str], bool]:
    if not targets:
        return [], set(), False

    print(f"\nDiscovered candidates for: {url}")
    for idx, target in enumerate(targets, start=1):
        mark = "已下载" if dedup_key(target) in seen_keys else "未下载"
        print(f"[{idx}] ({mark}) {target}")

    print("\n输入序号下载（如: 1,3 或 1-3），all=全部未下载，all+=全部包含已下载，skip=跳过该URL，q=退出")

    while True:
        raw = input("你的选择: ").strip().lower()
        if raw in {"q", "quit", "exit"}:
            return [], set(), True
        if raw in {"skip", "s"}:
            return [], set(), False
        if raw in {"all", "a"}:
            picked = [t for t in targets if dedup_key(t) not in seen_keys]
            return picked, set(), False
        if raw in {"all+", "a+", "allplus"}:
            force = {dedup_key(t) for t in targets if dedup_key(t) in seen_keys}
            return list(targets), force, False

        try:
            indices = parse_indices(raw, len(targets))
        except ValueError:
            print("输入格式错误，请重试")
            continue

        if not indices:
            print("没有有效序号，请重试")
            continue

        picked = [targets[i - 1] for i in sorted(indices)]
        force = {dedup_key(t) for t in picked if dedup_key(t) in seen_keys}
        return picked, force, False


def resolve_inputs_for_run(
    raw_inputs: list[str],
    cfg: AppConfig,
    output_root: Path,
) -> tuple[list[str], set[str], bool, dict[str, str]]:
    mode = cfg.scraping.candidate_mode

    archive_path = Path(cfg.scraping.download_archive).expanduser()
    if not archive_path.is_absolute():
        archive_path = (output_root / "后台数据" / archive_path).resolve()
    seen_keys = load_seen_archive(archive_path)

    final_inputs: list[str] = []
    force_keys: set[str] = set()
    source_pages: dict[str, str] = {}

    for item in raw_inputs:
        raw = item.strip()
        if not raw:
            continue

        if not is_url(raw):
            final_inputs.append(raw)
            continue
        if is_direct_media_url(raw):
            logging.info("检测到直接媒体链接，跳过网页扫描: %s", raw)
            final_inputs.append(raw)
            continue

        try:
            discovered = discover_targets(
                pages=[raw],
                timeout=int(cfg.scraping.request_timeout_seconds),
                user_agent=cfg.scraping.user_agent,
                always_try_page_url=bool(cfg.scraping.always_try_page_url),
            )
            targets = sorted(discovered) if discovered else [raw]
        except Exception as exc:  # noqa: BLE001
            logging.warning("URL discover failed, fallback to direct URL: %s (%s)", raw, exc)
            targets = [raw]

        for target in targets:
            if target != raw:
                source_pages[target] = raw

        if mode == "auto":
            final_inputs.extend(targets)
            continue

        selected, local_force, requested_quit = _prompt_select_targets_for_url(raw, targets, seen_keys)
        if requested_quit:
            return final_inputs, force_keys, True, source_pages

        final_inputs.extend(selected)
        force_keys.update(local_force)

    # keep order and dedupe
    deduped_inputs: list[str] = []
    seen_input: set[str] = set()
    for item in final_inputs:
        if item in seen_input:
            continue
        seen_input.add(item)
        deduped_inputs.append(item)

    return deduped_inputs, force_keys, False, source_pages


def _ensure_ffmpeg() -> tuple[bool, str]:
    ffmpeg_ok, ffmpeg_message = check_ffmpeg_available("ffmpeg")
    if not ffmpeg_ok:
        logging.error(ffmpeg_message)
        return False, ffmpeg_message
    logging.info("Using ffmpeg: %s", ffmpeg_message)
    return True, ffmpeg_message


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "snapshot-make-prompt":
        return run_snapshot_make_prompt(args)

    if args.command == "snapshot-capture":
        ok, ffmpeg_message = _ensure_ffmpeg()
        if not ok:
            return 1
        return run_snapshot_capture(args, ffmpeg_bin=ffmpeg_message)

    ok, ffmpeg_message = _ensure_ffmpeg()
    if not ok:
        return 1

    config_path = Path(args.config).expanduser()
    cfg = load_config(config_path if config_path.exists() else None)

    if args.command == "run":
        apply_run_overrides(cfg, args)
    else:
        apply_common_overrides(cfg, args)

    base_dir = config_path.parent if config_path.exists() else Path.cwd()
    output_root = ensure_output_root(base_dir=base_dir, output_root=args.out or cfg.output_root)

    if args.command == "run":
        resolved_inputs, discovered_force_keys, requested_quit, discovered_source_pages = resolve_inputs_for_run(args.input, cfg, output_root)
        if requested_quit:
            logging.info("Cancelled by user during candidate selection")
            return 0

        force_keys = set(args.force_key or [])
        force_keys.update(discovered_force_keys)
        source_pages = discovered_source_pages
        source_pages.update(parse_source_page_pairs(args.source_page))

        if not resolved_inputs:
            logging.info("No inputs selected after candidate filtering")
            return 0

        run_id = args.run_id or uuid.uuid4().hex[:12]
        orchestrator = MediaOrchestrator(
            config=cfg,
            output_root=output_root,
            ffmpeg_bin=ffmpeg_message,
            run_id=run_id,
            force_keys=force_keys,
            source_pages=source_pages,
        )
        summary = orchestrator.run(resolved_inputs)
    else:
        run_id = args.run_id or uuid.uuid4().hex[:12]
        summary = retry_from_failed_log(
            failed_log_path=Path(args.failed_log).expanduser().resolve(),
            config=cfg,
            output_root=output_root,
            ffmpeg_bin=ffmpeg_message,
            run_id=run_id,
        )

    logging.info(
        "Done. total=%d success=%d failed=%d\nrun_id=%s\nmanifest=%s\nfailed_log=%s\nledger=%s",
        summary.total,
        summary.succeeded,
        summary.failed,
        summary.run_id,
        summary.manifest_path,
        summary.failed_log_path,
        summary.ledger_path,
    )
    return 0 if summary.failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
