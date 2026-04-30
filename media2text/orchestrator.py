from __future__ import annotations

import logging
import hashlib
import re
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

from yt_dlp.utils import DownloadError

from .config import AppConfig
from .ffmpeg_utils import discover_local_media, extract_audio_to_file, extract_audio_to_wav
from .io_utils import (
    append_jsonl,
    is_url,
    normalize_platform,
    pick_date_yy_mm_dd,
    read_jsonl,
    sanitize_filename,
    save_json,
)
from .run_ledger import log_ledger_event, make_ledger_path
from .scraper_engine import append_seen_archive, dedup_key, load_seen_archive
from .subtitle_utils import subtitle_file_to_srt_and_text
from .transcriber import WhisperTranscriber
from .ytdlp_pipeline import download_best_subtitle, download_media_file, extract_info

logger = logging.getLogger(__name__)

LogFn = Callable[[str], None] | None
GENERIC_OUTPUT_TITLES = {"file", "video", "watch", "index", "master", "unknown", "untitled"}
RESULT_INDEX_RE = re.compile(r"^(\d{3,})_")
CANDIDATE_SUFFIX_RE = re.compile(r"(?:\s*[-_]\s*)?候选\s*\d{1,5}$", re.IGNORECASE)


class TaskFailure(RuntimeError):
    def __init__(self, message: str, stage: str, retry_suggestion: str = "") -> None:
        super().__init__(message)
        self.stage = stage
        self.retry_suggestion = retry_suggestion


@dataclass
class SourceTask:
    source: str
    resolved_input: str
    source_type: str  # local_file/url


@dataclass
class BatchSummary:
    total: int
    succeeded: int
    failed: int
    manifest_path: Path
    failed_log_path: Path
    run_id: str
    ledger_path: Path


class MediaOrchestrator:
    def __init__(
        self,
        config: AppConfig,
        output_root: Path,
        ffmpeg_bin: str = "ffmpeg",
        run_id: str | None = None,
        force_keys: set[str] | None = None,
        log_fn: LogFn = None,
    ) -> None:
        self.config = config
        self.output_root = output_root
        self.ffmpeg_bin = ffmpeg_bin
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.force_keys = force_keys or set()
        self.log_fn = log_fn

        self.system_dir = output_root / "后台数据"
        self.result_dir = output_root / "结果"
        self.manifest_path = self.system_dir / "manifest.jsonl"
        self.failed_path = self.system_dir / "failed_tasks.jsonl"
        self.ledger_path = make_ledger_path(output_root, self.run_id)

        archive_value = self.config.scraping.download_archive.strip() or "downloaded.txt"
        archive_path = Path(archive_value).expanduser()
        if not archive_path.is_absolute():
            archive_path = (self.system_dir / archive_path).resolve()
        self.archive_path = archive_path
        self.seen_keys = load_seen_archive(self.archive_path)

        self._transcriber: WhisperTranscriber | None = None
        self._next_result_index = self._scan_next_result_index()

    def _log(self, message: str) -> None:
        logger.info(message)
        if self.log_fn:
            self.log_fn(message)

    def _ledger(self, event: str, **payload) -> None:
        log_ledger_event(
            self.ledger_path,
            event=event,
            run_id=self.run_id,
            timestamp=datetime.now().isoformat(timespec="seconds"),
            **payload,
        )

    def _record_artifact(self, task_id: str, file_path: Path, kind: str) -> None:
        if not file_path.exists() or not file_path.is_file():
            return
        self._ledger("artifact", task_id=task_id, path=str(file_path), kind=kind)

    def _get_transcriber(self) -> WhisperTranscriber:
        if self._transcriber is None:
            self._transcriber = WhisperTranscriber(
                model_size=self.config.whisper.model,
                language=self.config.whisper.language,
                device=self.config.whisper.device,
                compute_type=self.config.whisper.compute_type,
            )
        return self._transcriber

    def expand_inputs(self, inputs: Iterable[str]) -> list[SourceTask]:
        expanded: list[SourceTask] = []
        for raw in inputs:
            item = raw.strip()
            if not item:
                continue

            if is_url(item):
                expanded.append(SourceTask(source=item, resolved_input=item, source_type="url"))
                continue

            path = Path(item).expanduser()
            if not path.exists():
                expanded.append(SourceTask(source=item, resolved_input=item, source_type="missing"))
                continue

            media_files = discover_local_media(path)
            if not media_files:
                expanded.append(SourceTask(source=item, resolved_input=str(path.resolve()), source_type="missing"))
                continue

            for media_file in media_files:
                expanded.append(
                    SourceTask(
                        source=item,
                        resolved_input=str(media_file.resolve()),
                        source_type="local_file",
                    )
                )

        deduped: list[SourceTask] = []
        seen: set[tuple[str, str]] = set()
        for task in expanded:
            key = (task.source_type, task.resolved_input)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(task)
        return deduped

    @staticmethod
    def _display_date(date_str: str) -> str:
        parts = (date_str or "").split("-")
        if len(parts) == 3 and len(parts[0]) == 2:
            return f"20{parts[0]}-{parts[1]}-{parts[2]}"
        return date_str or datetime.now().strftime("%Y-%m-%d")

    @staticmethod
    def _title_for_filename(title: str) -> str:
        safe_title = sanitize_filename(title, default="untitled", max_len=96)
        safe_title = CANDIDATE_SUFFIX_RE.sub("", safe_title).strip(" -_")
        stem = safe_title.rsplit(".", 1)[0].strip().lower()
        if stem in GENERIC_OUTPUT_TITLES:
            return "media"
        return safe_title or "media"

    def _scan_next_result_index(self) -> int:
        max_index = 0
        if self.result_dir.exists():
            for path in self.result_dir.iterdir():
                if not path.is_file():
                    continue
                match = RESULT_INDEX_RE.match(path.name)
                if not match:
                    continue
                try:
                    max_index = max(max_index, int(match.group(1)))
                except ValueError:
                    continue
        return max_index + 1

    def _claim_result_base_name(self, task: SourceTask, date_str: str, title: str) -> tuple[str, int, str]:
        title_part = self._title_for_filename(title)
        url_hash = hashlib.sha1(task.resolved_input.encode("utf-8", errors="ignore")).hexdigest()[:8]
        while True:
            result_index = self._next_result_index
            self._next_result_index += 1
            base_name = sanitize_filename(
                f"{result_index:03d}_{self._display_date(date_str)}_{title_part}_{url_hash}",
                default=f"{result_index:03d}_media_{url_hash}",
                max_len=150,
            )
            if not any(self.result_dir.glob(base_name + ".*")):
                return base_name, result_index, url_hash

    def _build_output_paths(
        self,
        task: SourceTask,
        task_index: int,
        date_str: str,
        title: str,
    ) -> dict[str, Path | str]:
        base_name, result_index, url_hash = self._claim_result_base_name(task=task, date_str=date_str, title=title)
        return {
            "base_name": base_name,
            "result_index": result_index,
            "candidate_index": task_index,
            "candidate_title": title,
            "url_hash": url_hash,
            "result_dir": self.result_dir,
            "audio_dir": self.result_dir,
            "text_dir": self.result_dir,
            "subtitle_dir": self.result_dir,
            "video_dir": self.result_dir,
            "metadata_dir": self.system_dir / "metadata",
            "temp_dir": self.system_dir / "cache",
        }

    def run(self, input_items: list[str]) -> BatchSummary:
        tasks = self.expand_inputs(input_items)
        self._log(f"[INFO] Expanded to {len(tasks)} task(s).")
        self._ledger("run_start", total=len(tasks))

        success = 0
        failed = 0

        try:
            for index, task in enumerate(tasks, start=1):
                self._log(f"[TASK {index}/{len(tasks)}] {task.resolved_input}")
                started = time.time()
                task_id = uuid.uuid4().hex[:12]
                self._ledger("task_start", task_id=task_id, source=task.source, resolved_input=task.resolved_input)

                try:
                    record = self._process_task(task=task, task_id=task_id, task_index=index)
                    record["duration_seconds"] = round(time.time() - started, 2)
                    append_jsonl(self.manifest_path, record)
                    task_status = str(record.get("status") or "success")
                    if task_status == "failed":
                        failed += 1
                    elif task_status == "skipped":
                        self._log(f"[SKIPPED] {task.resolved_input}: {record.get('error') or 'skipped'}")
                    else:
                        success += 1
                        self._log(f"[DONE] {task.resolved_input}")
                    self._ledger("task_end", task_id=task_id, status=task_status)
                except TaskFailure as exc:
                    failed += 1
                    elapsed = round(time.time() - started, 2)
                    fail_record = {
                        "run_id": self.run_id,
                        "task_id": task_id,
                        "candidate_index": index,
                        "candidate_title": "",
                        "source": task.source,
                        "original_url": task.resolved_input if task.source_type == "url" else "",
                        "retry_input": task.resolved_input,
                        "status": "failed",
                        "stage": exc.stage,
                        "error": str(exc),
                        "retry_suggestion": exc.retry_suggestion
                        or "Retry with: media_tool run --input \"<same-input>\" --language auto",
                        "duration_seconds": elapsed,
                        "created_at": datetime.now().isoformat(timespec="seconds"),
                    }
                    append_jsonl(self.failed_path, fail_record)
                    append_jsonl(self.manifest_path, fail_record)
                    self._ledger("task_end", task_id=task_id, status="failed", stage=exc.stage, error=str(exc))
                    self._log(f"[FAILED] {task.resolved_input}: {exc}")
                except KeyboardInterrupt:
                    elapsed = round(time.time() - started, 2)
                    stop_record = {
                        "run_id": self.run_id,
                        "task_id": task_id,
                        "candidate_index": index,
                        "candidate_title": "",
                        "source": task.source,
                        "original_url": task.resolved_input if task.source_type == "url" else "",
                        "retry_input": task.resolved_input,
                        "status": "failed",
                        "stage": "stopped_by_user",
                        "error": "Stopped by user",
                        "retry_suggestion": "Retry with: media_tool run --input \"<same-input>\"",
                        "duration_seconds": elapsed,
                        "created_at": datetime.now().isoformat(timespec="seconds"),
                    }
                    append_jsonl(self.failed_path, stop_record)
                    append_jsonl(self.manifest_path, stop_record)
                    self._ledger("task_end", task_id=task_id, status="failed", stage="stopped_by_user")
                    raise
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    elapsed = round(time.time() - started, 2)
                    fail_record = {
                        "run_id": self.run_id,
                        "task_id": task_id,
                        "candidate_index": index,
                        "candidate_title": "",
                        "source": task.source,
                        "original_url": task.resolved_input if task.source_type == "url" else "",
                        "retry_input": task.resolved_input,
                        "status": "failed",
                        "stage": "processing",
                        "error": str(exc),
                        "retry_suggestion": "Retry with: media_tool run --input \"<same-input>\" --language auto",
                        "duration_seconds": elapsed,
                        "created_at": datetime.now().isoformat(timespec="seconds"),
                    }
                    append_jsonl(self.failed_path, fail_record)
                    append_jsonl(self.manifest_path, fail_record)
                    self._ledger("task_end", task_id=task_id, status="failed", stage="processing", error=str(exc))
                    self._log(f"[FAILED] {task.resolved_input}: {exc}")
        finally:
            self._ledger("run_end", success=success, failed=failed)

        return BatchSummary(
            total=len(tasks),
            succeeded=success,
            failed=failed,
            manifest_path=self.manifest_path,
            failed_log_path=self.failed_path,
            run_id=self.run_id,
            ledger_path=self.ledger_path,
        )

    def _process_task(self, task: SourceTask, task_id: str, task_index: int) -> dict:
        if task.source_type == "missing":
            raise TaskFailure(
                f"Input not found or unsupported: {task.resolved_input}",
                stage="input",
                retry_suggestion="Check file path/URL and rerun.",
            )

        if task.source_type == "local_file":
            return self._process_local(task, task_id, task_index)

        if task.source_type == "url":
            return self._process_url(task, task_id, task_index)

        raise RuntimeError(f"Unsupported task type: {task.source_type}")

    def _process_local(self, task: SourceTask, task_id: str, task_index: int) -> dict:
        media_path = Path(task.resolved_input)
        date_str = datetime.now().strftime("%y-%m-%d")
        platform = "local"
        channel = media_path.parent.name or "unknown"
        title = media_path.stem
        paths = self._build_output_paths(task=task, task_index=task_index, date_str=date_str, title=title)
        base_name = str(paths["base_name"])

        txt_path = Path(paths["text_dir"]) / f"{base_name}.txt"
        srt_path = Path(paths["subtitle_dir"]) / f"{base_name}.srt"
        meta_path = Path(paths["metadata_dir"]) / f"{base_name}.json"
        audio_export_path: Path | None = None

        wants_text = self.config.download.output_txt or self.config.download.output_srt
        if wants_text and self.config.subtitle_priority != "skip_text":
            self._log("  extracting audio for Whisper...")
            self._transcribe_media(media_path=media_path, txt_path=txt_path, srt_path=srt_path)
            self._log("  writing output files...")
            txt_path, srt_path = self._apply_text_output_policy(task_id=task_id, txt_path=txt_path, srt_path=srt_path)
        else:
            self._log("  text/subtitle output disabled; skipping Whisper transcription.")
            txt_path = None
            srt_path = None

        if self.config.download.export_audio:
            self._log("  exporting audio...")
            audio_export_path = self._export_audio(
                media_path=media_path,
                audio_path=Path(paths["audio_dir"]) / f"{base_name}.{self._audio_extension()}",
                task_id=task_id,
            )

        record = {
            "run_id": self.run_id,
            "task_id": task_id,
            "candidate_index": int(paths["candidate_index"]),
            "candidate_title": str(paths["candidate_title"]),
            "result_index": int(paths["result_index"]),
            "source": task.source,
            "original_url": "",
            "resolved_input": task.resolved_input,
            "platform": platform,
            "title": title,
            "date": date_str,
            "transcript_source": "whisper" if txt_path or srt_path else "none",
            "status": "success",
            "artifacts": {
                "txt": str(txt_path) if txt_path else "",
                "srt": str(srt_path) if srt_path else "",
                "json": str(meta_path) if self.config.download.save_metadata else "",
                "media": str(media_path) if self.config.download.keep_original else "",
                "audio": str(audio_export_path) if audio_export_path else "",
            },
            "output_audio_path": str(audio_export_path) if audio_export_path else "",
            "error": "",
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        if self.config.download.save_metadata:
            save_json(meta_path, record)
            self._record_artifact(task_id, meta_path, "json")
        return record

    def _process_url(self, task: SourceTask, task_id: str, task_index: int) -> dict:
        key = dedup_key(task.resolved_input)
        if key in self.seen_keys and key not in self.force_keys:
            return {
                "run_id": self.run_id,
                "task_id": task_id,
                "candidate_index": task_index,
                "candidate_title": "",
                "source": task.source,
                "original_url": task.resolved_input,
                "resolved_input": task.resolved_input,
                "status": "skipped",
                "stage": "dedup",
                "error": "Already downloaded",
                "created_at": datetime.now().isoformat(timespec="seconds"),
            }

        try:
            self._log("  extracting metadata...")
            info = extract_info(task.resolved_input, download_cfg=self.config.download)
        except Exception as exc:  # noqa: BLE001
            raise TaskFailure(
                f"Failed to extract URL info: {exc}",
                stage="metadata",
                retry_suggestion="The URL may be restricted/unsupported. Try cookies or another URL.",
            ) from exc

        title = str(info.get("title") or "untitled")
        upload_date = str(info.get("upload_date") or "")
        date_str = pick_date_yy_mm_dd(upload_date)
        channel = str(info.get("channel") or info.get("uploader") or "unknown")
        platform = normalize_platform(task.resolved_input, extractor=str(info.get("extractor") or ""))

        paths = self._build_output_paths(task=task, task_index=task_index, date_str=date_str, title=title)
        base_name = str(paths["base_name"])
        txt_path = Path(paths["text_dir"]) / f"{base_name}.txt"
        srt_path = Path(paths["subtitle_dir"]) / f"{base_name}.srt"
        meta_path = Path(paths["metadata_dir"]) / f"{base_name}.json"
        media_path: Path | None = None
        audio_export_path: Path | None = None

        wants_text = self.config.download.output_txt or self.config.download.output_srt
        wants_platform_subtitle = wants_text and self.config.subtitle_priority in {
            "subtitle_first_then_whisper",
            "platform_only",
        }
        wants_whisper = wants_text and self.config.subtitle_priority in {
            "subtitle_first_then_whisper",
            "whisper_only",
        }
        transcript_source = "none"

        subtitle_path = None
        if wants_platform_subtitle:
            self._log("  downloading platform subtitles...")
            subtitle_path = download_best_subtitle(
                url=task.resolved_input,
                subtitle_stem=Path(paths["temp_dir"]) / f"{base_name}__subtitle",
                subtitle_langs=self.config.subtitle_langs,
                download_cfg=self.config.download,
            )

        if subtitle_path:
            self._log("  writing subtitle and text files...")
            srt_text, plain_text = subtitle_file_to_srt_and_text(subtitle_path)
            srt_path.parent.mkdir(parents=True, exist_ok=True)
            txt_path.parent.mkdir(parents=True, exist_ok=True)
            srt_path.write_text(srt_text, encoding="utf-8")
            txt_path.write_text(plain_text, encoding="utf-8")
            transcript_source = "platform_subtitle"
            self._record_artifact(task_id, subtitle_path, "subtitle_raw")
            txt_path, srt_path = self._apply_text_output_policy(task_id=task_id, txt_path=txt_path, srt_path=srt_path)
            self._log(f"  subtitle success: {subtitle_path.name}")
        elif wants_whisper:
            self._log("  subtitle unavailable, falling back to whisper...")
        else:
            txt_path = None
            srt_path = None
            if self.config.subtitle_priority == "skip_text":
                self._log("  subtitle/text strategy is skip; skipping platform subtitles and Whisper.")
            elif not wants_text:
                self._log("  text/subtitle output disabled; skipping platform subtitles and Whisper.")

        needs_whisper = transcript_source != "platform_subtitle" and wants_whisper
        needs_media = self.config.download.keep_original or self.config.download.export_audio or needs_whisper
        if needs_media:
            try:
                self._log("  downloading media...")
                media_dir = Path(paths["video_dir"]) if self.config.download.keep_original else Path(paths["temp_dir"])
                media_path = download_media_file(
                    url=task.resolved_input,
                    media_stem=media_dir / f"{base_name}__media",
                    download_cfg=self.config.download,
                )
                self._record_artifact(task_id, media_path, "media")
                self._log(f"  media downloaded: {media_path.name}")
            except DownloadError as exc:
                if transcript_source == "platform_subtitle":
                    self._log(f"  [WARN] media download failed but subtitle output already exists: {exc}")
                else:
                    raise TaskFailure(
                        f"Failed downloading media: {exc}",
                        stage="download",
                        retry_suggestion="Retry later or provide cookies if login is required.",
                    ) from exc
            except Exception as exc:  # noqa: BLE001
                if transcript_source == "platform_subtitle":
                    self._log(f"  [WARN] media download failed but subtitle output already exists: {exc}")
                else:
                    raise TaskFailure(
                        f"Failed downloading media: {exc}",
                        stage="download",
                        retry_suggestion="Retry later or provide cookies if login is required.",
                    ) from exc

        if needs_whisper:
            if media_path is None:
                raise TaskFailure(
                    "No subtitle and no media downloaded; cannot transcribe",
                    stage="download",
                    retry_suggestion="Check URL availability and retry.",
                )
            self._log("  running Whisper transcription...")
            self._transcribe_media(media_path=media_path, txt_path=txt_path, srt_path=srt_path)
            self._log("  writing output files...")
            txt_path, srt_path = self._apply_text_output_policy(task_id=task_id, txt_path=txt_path, srt_path=srt_path)
            transcript_source = "whisper"

        if self.config.download.export_audio and media_path is not None:
            self._log("  exporting audio...")
            audio_export_path = self._export_audio(
                media_path=media_path,
                audio_path=Path(paths["audio_dir"]) / f"{base_name}.{self._audio_extension()}",
                task_id=task_id,
            )

        media_record_path = str(media_path) if media_path else ""
        if media_path is not None and not self.config.download.keep_original:
            self._log("  deleting temporary media...")
            if self._safe_unlink(media_path):
                self._log("  removed temporary media")
            else:
                self._log(f"  [WARN] temporary media was not removed: {media_path}")
            media_record_path = ""

        if key not in self.seen_keys:
            self.seen_keys.add(key)
            append_seen_archive(self.archive_path, key, task.resolved_input)

        record = {
            "run_id": self.run_id,
            "task_id": task_id,
            "candidate_index": int(paths["candidate_index"]),
            "candidate_title": str(paths["candidate_title"]),
            "result_index": int(paths["result_index"]),
            "source": task.source,
            "original_url": task.resolved_input,
            "resolved_input": task.resolved_input,
            "platform": platform,
            "title": title,
            "date": date_str,
            "transcript_source": transcript_source,
            "status": "success",
            "artifacts": {
                "txt": str(txt_path) if txt_path else "",
                "srt": str(srt_path) if srt_path else "",
                "json": str(meta_path) if self.config.download.save_metadata else "",
                "media": media_record_path,
                "audio": str(audio_export_path) if audio_export_path else "",
            },
            "output_audio_path": str(audio_export_path) if audio_export_path else "",
            "error": "",
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        if self.config.download.save_metadata:
            save_json(meta_path, record)
            self._record_artifact(task_id, meta_path, "json")
        return record

    def _transcribe_media(self, media_path: Path, txt_path: Path, srt_path: Path) -> None:
        transcriber = self._get_transcriber()
        with tempfile.TemporaryDirectory(prefix="media2text_") as tmp_dir:
            wav_path = Path(tmp_dir) / f"{media_path.stem}.wav"
            try:
                self._log("  extracting audio...")
                extract_audio_to_wav(media_path=media_path, wav_path=wav_path, ffmpeg_bin=self.ffmpeg_bin)
            except Exception as exc:  # noqa: BLE001
                raise TaskFailure(
                    str(exc),
                    stage="audio_extract",
                    retry_suggestion="Check media integrity. For m4a/caf voice memos, retry with --language auto.",
                ) from exc

            try:
                self._log("  Whisper transcribing...")
                seg_count = transcriber.transcribe_to_files(
                    audio_path=wav_path,
                    txt_output_path=txt_path,
                    srt_output_path=srt_path,
                )
            except Exception as exc:  # noqa: BLE001
                raise TaskFailure(
                    str(exc),
                    stage="transcribe",
                    retry_suggestion="Retry with --language auto or switch model to large-v3.",
                ) from exc

            if seg_count == 0:
                raise TaskFailure(
                    "No output segments produced.",
                    stage="transcribe",
                    retry_suggestion="Retry with --language auto.",
                )
            self._log("  writing transcript files...")

    def _apply_text_output_policy(self, task_id: str, txt_path: Path, srt_path: Path) -> tuple[Path | None, Path | None]:
        txt_out: Path | None = txt_path
        srt_out: Path | None = srt_path

        if not self.config.download.output_txt:
            self._safe_unlink(txt_path)
            txt_out = None
        else:
            self._log("  writing text output...")
            self._record_artifact(task_id, txt_path, "txt")

        if not self.config.download.output_srt:
            self._safe_unlink(srt_path)
            srt_out = None
        else:
            self._log("  writing subtitle output...")
            self._record_artifact(task_id, srt_path, "srt")

        return txt_out, srt_out

    def _audio_extension(self) -> str:
        fmt = (self.config.download.export_audio_format or "mp3").strip().lower()
        if fmt not in {"mp3", "wav"}:
            fmt = "mp3"
        return fmt

    def _export_audio(self, media_path: Path, audio_path: Path, task_id: str) -> Path | None:
        fmt = self._audio_extension()
        try:
            self._log("  extracting audio export...")
            extract_audio_to_file(
                media_path=media_path,
                output_audio_path=audio_path,
                ffmpeg_bin=self.ffmpeg_bin,
                audio_format=fmt,
            )
        except Exception as exc:  # noqa: BLE001
            self._log(f"  [WARN] audio export failed: {exc}")
            return None
        self._record_artifact(task_id, audio_path, "audio")
        self._log(f"  audio export success: {audio_path.name}")
        return audio_path

    @staticmethod
    def _safe_unlink(path: Path) -> bool:
        last_error: Exception | None = None
        for _attempt in range(3):
            try:
                if path.exists() and path.is_file():
                    path.unlink()
                if not path.exists():
                    return True
            except Exception as exc:
                last_error = exc
                time.sleep(0.2)
        if last_error:
            logger.warning("Failed to remove file %s: %s", path, last_error)
        return not path.exists()


def retry_from_failed_log(
    failed_log_path: Path,
    config: AppConfig,
    output_root: Path,
    ffmpeg_bin: str = "ffmpeg",
    run_id: str | None = None,
    log_fn: LogFn = None,
) -> BatchSummary:
    rows = read_jsonl(failed_log_path)
    retry_inputs: list[str] = []
    for row in rows:
        candidate = str(row.get("retry_input") or row.get("source") or "").strip()
        if candidate:
            retry_inputs.append(candidate)

    unique_retry_inputs: list[str] = []
    seen: set[str] = set()
    for item in retry_inputs:
        if item in seen:
            continue
        seen.add(item)
        unique_retry_inputs.append(item)

    orchestrator = MediaOrchestrator(
        config=config,
        output_root=output_root,
        ffmpeg_bin=ffmpeg_bin,
        run_id=run_id,
        log_fn=log_fn,
    )
    return orchestrator.run(unique_retry_inputs)
