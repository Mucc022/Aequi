from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_SUBTITLE_LANGS = ["zh-Hans", "zh-CN", "zh", "en", "en.*"]
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


@dataclass
class WhisperConfig:
    model: str = "medium"
    language: str = "zh"
    device: str = "auto"
    compute_type: str | None = None


@dataclass
class DownloadConfig:
    keep_original: bool = True
    save_metadata: bool = False
    cookie_mode: str = "none"  # none/cookies_file/browser
    cookies_file: str = ""
    cookies_browser: str = "chrome"  # chrome/edge/firefox
    quality: str = "best"
    prefer_compatible_codecs: bool = True
    allow_separate_streams: bool = False
    js_runtimes: list[str] = field(default_factory=lambda: ["deno", "node"])
    remote_components: list[str] = field(default_factory=lambda: ["ejs:github"])
    download_subtitles: bool = False
    write_auto_subtitles: bool = True
    embed_subtitles: bool = False
    subtitle_output_format: str = "srt"  # srt/vtt/ass
    export_audio: bool = False
    export_audio_format: str = "mp3"  # mp3/wav
    output_txt: bool = True
    output_srt: bool = True


@dataclass
class ScrapingConfig:
    request_timeout_seconds: int = 20
    user_agent: str = DEFAULT_USER_AGENT
    always_try_page_url: bool = False
    candidate_mode: str = "select"  # select / auto
    download_archive: str = "downloaded.txt"


@dataclass
class AppConfig:
    output_root: str = "outputs"
    naming_template: str = "{platform}_{date}_{title}"
    include_result_index: bool = False
    subtitle_priority: str = "subtitle_first_then_whisper"
    subtitle_langs: list[str] = field(default_factory=lambda: list(DEFAULT_SUBTITLE_LANGS))
    whisper: WhisperConfig = field(default_factory=WhisperConfig)
    download: DownloadConfig = field(default_factory=DownloadConfig)
    scraping: ScrapingConfig = field(default_factory=ScrapingConfig)


def _merge_into_dataclass(instance: Any, raw: dict[str, Any]) -> Any:
    for key, value in raw.items():
        if not hasattr(instance, key):
            continue
        current = getattr(instance, key)
        if hasattr(current, "__dataclass_fields__") and isinstance(value, dict):
            _merge_into_dataclass(current, value)
        else:
            setattr(instance, key, value)
    return instance


def _apply_legacy_keys(cfg: AppConfig, raw: dict[str, Any]) -> None:
    # from old Video Scraping config shape
    if "request_timeout_seconds" in raw:
        cfg.scraping.request_timeout_seconds = int(raw["request_timeout_seconds"])
    if "user_agent" in raw:
        cfg.scraping.user_agent = str(raw["user_agent"])
    if "always_try_page_url" in raw:
        cfg.scraping.always_try_page_url = bool(raw["always_try_page_url"])
    if "download_archive" in raw:
        cfg.scraping.download_archive = str(raw["download_archive"])

    if "cookie_file" in raw and not cfg.download.cookies_file:
        cfg.download.cookies_file = str(raw["cookie_file"])
        cfg.download.cookie_mode = "cookies_file"

    if "quality" in raw:
        cfg.download.quality = str(raw["quality"])
    if "save_metadata" in raw:
        cfg.download.save_metadata = bool(raw["save_metadata"])
    if "prefer_compatible_codecs" in raw:
        cfg.download.prefer_compatible_codecs = bool(raw["prefer_compatible_codecs"])
    if "allow_separate_streams" in raw:
        cfg.download.allow_separate_streams = bool(raw["allow_separate_streams"])
    if "js_runtimes" in raw and isinstance(raw["js_runtimes"], list):
        cfg.download.js_runtimes = [str(x) for x in raw["js_runtimes"] if str(x).strip()]
    if "remote_components" in raw:
        rc = raw["remote_components"]
        if isinstance(rc, str):
            cfg.download.remote_components = [x.strip() for x in rc.split(",") if x.strip()]
        elif isinstance(rc, list):
            cfg.download.remote_components = [str(x) for x in rc if str(x).strip()]

    if "download_subtitles" in raw:
        cfg.download.download_subtitles = bool(raw["download_subtitles"])
    if "write_auto_subtitles" in raw:
        cfg.download.write_auto_subtitles = bool(raw["write_auto_subtitles"])
    if "embed_subtitles" in raw:
        cfg.download.embed_subtitles = bool(raw["embed_subtitles"])
    if "subtitle_output_format" in raw:
        cfg.download.subtitle_output_format = str(raw["subtitle_output_format"])
    if "export_audio" in raw:
        cfg.download.export_audio = bool(raw["export_audio"])
    if "export_audio_format" in raw:
        cfg.download.export_audio_format = str(raw["export_audio_format"])
    if "output_txt" in raw:
        cfg.download.output_txt = bool(raw["output_txt"])
    if "output_srt" in raw:
        cfg.download.output_srt = bool(raw["output_srt"])

    # legacy nested platform field from earlier plan text
    platform = raw.get("platform")
    if isinstance(platform, dict):
        cookies = platform.get("cookies_file")
        if cookies and not cfg.download.cookies_file:
            cfg.download.cookies_file = str(cookies)
            cfg.download.cookie_mode = "cookies_file"



def load_config(config_path: Path | None) -> AppConfig:
    cfg = AppConfig()
    if config_path is None or not config_path.exists():
        return cfg

    data = json.loads(config_path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        return cfg

    _merge_into_dataclass(cfg, data)
    _apply_legacy_keys(cfg, data)

    if cfg.scraping.candidate_mode not in {"select", "auto"}:
        cfg.scraping.candidate_mode = "select"

    if cfg.download.cookie_mode not in {"none", "cookies_file", "browser"}:
        cfg.download.cookie_mode = "none"
    if cfg.download.cookies_browser not in {"chrome", "edge", "firefox"}:
        cfg.download.cookies_browser = "chrome"

    if not cfg.download.js_runtimes:
        cfg.download.js_runtimes = ["deno", "node"]
    if not cfg.download.remote_components:
        cfg.download.remote_components = ["ejs:github"]

    if cfg.download.subtitle_output_format not in {"srt", "vtt", "ass"}:
        cfg.download.subtitle_output_format = "srt"
    if cfg.download.export_audio_format not in {"mp3", "wav"}:
        cfg.download.export_audio_format = "mp3"
    if not (cfg.download.output_txt or cfg.download.output_srt):
        cfg.download.output_txt = True

    if cfg.subtitle_priority not in {"subtitle_first_then_whisper", "platform_only", "whisper_only", "skip_text"}:
        cfg.subtitle_priority = "subtitle_first_then_whisper"

    return cfg


def ensure_output_root(base_dir: Path, output_root: str) -> Path:
    out = Path(output_root).expanduser()
    if not out.is_absolute():
        out = (base_dir / out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    return out
