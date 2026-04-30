from __future__ import annotations

import io
import os
import queue
import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

try:
    import requests
except Exception:  # noqa: BLE001
    requests = None

try:
    from PIL import Image, ImageTk
except Exception:  # noqa: BLE001
    Image = None
    ImageTk = None

try:
    from yt_dlp import YoutubeDL
except Exception:  # noqa: BLE001
    YoutubeDL = None

from .config import AppConfig, ensure_output_root, load_config
from .ffmpeg_utils import check_ffmpeg_available
from .io_utils import append_jsonl, is_url
from .run_ledger import rollback_from_ledger
from .scraper_engine import dedup_key, discover_targets, extract_youtube_video_id, load_seen_archive
from .snapshot_utils import (
    build_ai_prompt_from_srt,
    capture_snapshots,
    clamp_timepoints,
    dedupe_timepoints,
    extract_timepoints_from_ai_output,
    get_media_duration_seconds,
)


@dataclass
class CandidateItem:
    iid: str
    source_url: str
    url: str
    is_seen: bool
    checked: bool
    title: str
    thumbnail_url: str = ""
    thumb_bytes: bytes | None = None
    meta_loaded: bool = False
    meta_loading: bool = False


class MediaToolGUI(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Unified Media2Text")
        self.geometry("1280x920")
        self.minsize(1080, 780)

        self.input_text: tk.Text | None = None
        self.log_text: tk.Text | None = None
        self.log_queue: "queue.Queue[object]" = queue.Queue()

        self.worker_thread: threading.Thread | None = None
        self.proc: subprocess.Popen[str] | None = None
        self.advanced_visible = False
        self.stop_requested = False
        self._process_running = False

        self.current_run_id: str | None = None
        self.current_output_root: Path | None = None

        self._candidate_mode_active = False
        self._candidate_items: dict[str, CandidateItem] = {}
        self._candidate_order: list[str] = []
        self._pending_run_context: tuple[AppConfig, Path, Path, list[str]] | None = None
        self._preview_photo = None

        self.out_var = tk.StringVar(value="outputs")
        self.config_var = tk.StringVar(value="config.json")
        self.model_var = tk.StringVar(value="medium")
        self.language_var = tk.StringVar(value="zh")
        self.failed_log_var = tk.StringVar(value="")

        self.candidate_mode_var = tk.StringVar(value="select")
        self.quality_var = tk.StringVar(value="best")
        self.always_try_page_var = tk.BooleanVar(value=False)
        self.prefer_compatible_var = tk.BooleanVar(value=True)
        self.allow_separate_var = tk.BooleanVar(value=False)
        self.js_runtimes_var = tk.StringVar(value="deno,node")
        self.remote_components_var = tk.StringVar(value="ejs:github")
        self.download_archive_var = tk.StringVar(value="downloaded.txt")
        self.request_timeout_var = tk.StringVar(value="20")
        self.user_agent_var = tk.StringVar(value="")
        self.force_seen_var = tk.BooleanVar(value=True)

        self.snap_video_var = tk.StringVar(value="")
        self.snap_srt_var = tk.StringVar(value="")
        self.snap_prompt_out_var = tk.StringVar(value="")
        self.snap_ai_output_var = tk.StringVar(value="")
        self.snap_output_dir_var = tk.StringVar(value="")
        self.snap_max_shots_var = tk.StringVar(value="15")
        self.snap_min_gap_var = tk.StringVar(value="8")

        self.preview_title_var = tk.StringVar(value="未选择候选视频")
        self.preview_url_var = tk.StringVar(value="")
        self.preview_status_var = tk.StringVar(value="")
        self.candidate_summary_var = tk.StringVar(value="共 0 条，已勾选 0 条")

        self.run_btn: ttk.Button | None = None
        self.retry_btn: ttk.Button | None = None
        self.stop_btn: ttk.Button | None = None
        self.snap_prompt_btn: ttk.Button | None = None
        self.snap_capture_btn: ttk.Button | None = None
        self.adv_frame: ttk.Frame | None = None
        self.candidate_card: ttk.LabelFrame | None = None
        self.candidate_tree: ttk.Treeview | None = None
        self.preview_image_label: tk.Label | None = None

        self.action_buttons: list[ttk.Button] = []
        self.candidate_buttons: list[ttk.Button] = []

        self._build_ui()
        self.after(100, self._drain_logs)

    def _build_ui(self) -> None:
        style = ttk.Style()
        style.configure("Card.TLabelframe", padding=8)
        style.configure("Header.TLabel", font=("Microsoft YaHei UI", 18, "bold"))
        style.configure("Hint.TLabel", foreground="#355267")

        root = ttk.Frame(self, padding=12)
        root.pack(fill="both", expand=True)

        top = ttk.Frame(root)
        top.pack(fill="x", pady=(0, 8))
        ttk.Label(top, text="Unified Media2Text", style="Header.TLabel").pack(anchor="w")
        ttk.Label(
            top,
            text="输入本地音频/视频/链接，自动字幕优先，失败回退 fast-whisper",
            style="Hint.TLabel",
        ).pack(anchor="w")

        input_card = ttk.LabelFrame(root, text="输入（每行一个：文件 / 文件夹 / URL）", style="Card.TLabelframe")
        input_card.pack(fill="both", expand=False)

        self.input_text = tk.Text(input_card, height=8, wrap="word", font=("Consolas", 11))
        self.input_text.pack(fill="both", expand=True)

        row = ttk.Frame(input_card)
        row.pack(fill="x", pady=(8, 0))
        ttk.Button(row, text="添加文件", command=self._add_file).pack(side="left")
        ttk.Button(row, text="添加文件夹", command=self._add_folder).pack(side="left", padx=6)
        ttk.Button(row, text="添加链接", command=self._add_url).pack(side="left")
        ttk.Button(row, text="清空", command=self._clear_inputs).pack(side="left", padx=6)

        settings = ttk.LabelFrame(root, text="基础设置", style="Card.TLabelframe")
        settings.pack(fill="x", pady=(10, 0))

        s1 = ttk.Frame(settings)
        s1.pack(fill="x", pady=(0, 6))
        ttk.Label(s1, text="输出目录", width=10).pack(side="left")
        ttk.Entry(s1, textvariable=self.out_var).pack(side="left", fill="x", expand=True)
        ttk.Button(s1, text="选择", command=self._choose_output).pack(side="left", padx=6)

        s2 = ttk.Frame(settings)
        s2.pack(fill="x")
        ttk.Label(s2, text="配置文件", width=10).pack(side="left")
        ttk.Entry(s2, textvariable=self.config_var).pack(side="left", fill="x", expand=True)
        ttk.Button(s2, text="选择", command=self._choose_config).pack(side="left", padx=6)

        toggle_row = ttk.Frame(root)
        toggle_row.pack(fill="x", pady=(8, 0))
        ttk.Button(toggle_row, text="高级设置", command=self._toggle_advanced).pack(side="left")

        self.adv_frame = ttk.LabelFrame(root, text="高级设置（抓取参数默认隐藏）", style="Card.TLabelframe")

        a1 = ttk.Frame(self.adv_frame)
        a1.pack(fill="x", pady=(0, 6))
        ttk.Label(a1, text="Whisper 模型", width=10).pack(side="left")
        ttk.Combobox(
            a1,
            textvariable=self.model_var,
            values=["small", "medium", "large-v3"],
            width=12,
            state="readonly",
        ).pack(side="left")
        ttk.Label(a1, text="语言", width=8).pack(side="left", padx=(12, 0))
        ttk.Entry(a1, textvariable=self.language_var, width=10).pack(side="left")
        ttk.Label(a1, text="候选模式", width=8).pack(side="left", padx=(12, 0))
        ttk.Combobox(
            a1,
            textvariable=self.candidate_mode_var,
            values=["select", "auto"],
            width=10,
            state="readonly",
        ).pack(side="left")

        a2 = ttk.Frame(self.adv_frame)
        a2.pack(fill="x", pady=(0, 6))
        ttk.Label(a2, text="画质", width=10).pack(side="left")
        ttk.Combobox(
            a2,
            textvariable=self.quality_var,
            values=["best", "1080p", "720p", "480p", "360p", "worst"],
            width=12,
            state="readonly",
        ).pack(side="left")
        ttk.Checkbutton(a2, text="始终尝试页面URL", variable=self.always_try_page_var).pack(side="left", padx=(12, 0))
        ttk.Checkbutton(a2, text="优先兼容编码", variable=self.prefer_compatible_var).pack(side="left", padx=(12, 0))
        ttk.Checkbutton(a2, text="允许分离流", variable=self.allow_separate_var).pack(side="left", padx=(12, 0))

        a3 = ttk.Frame(self.adv_frame)
        a3.pack(fill="x", pady=(0, 6))
        ttk.Label(a3, text="JS Runtime", width=10).pack(side="left")
        ttk.Entry(a3, textvariable=self.js_runtimes_var).pack(side="left", fill="x", expand=True)
        ttk.Label(a3, text="Remote", width=8).pack(side="left", padx=(8, 0))
        ttk.Entry(a3, textvariable=self.remote_components_var).pack(side="left", fill="x", expand=True)

        a4 = ttk.Frame(self.adv_frame)
        a4.pack(fill="x")
        ttk.Label(a4, text="Archive", width=10).pack(side="left")
        ttk.Entry(a4, textvariable=self.download_archive_var).pack(side="left", fill="x", expand=True)
        ttk.Label(a4, text="Timeout", width=8).pack(side="left", padx=(8, 0))
        ttk.Entry(a4, textvariable=self.request_timeout_var, width=8).pack(side="left")
        ttk.Label(a4, text="失败日志", width=8).pack(side="left", padx=(10, 0))
        ttk.Entry(a4, textvariable=self.failed_log_var).pack(side="left", fill="x", expand=True)
        ttk.Button(a4, text="选择", command=self._choose_failed_log).pack(side="left", padx=6)

        actions = ttk.Frame(root)
        actions.pack(fill="x", pady=(10, 8))
        self.run_btn = ttk.Button(actions, text="开始处理", command=self._run_processing)
        self.run_btn.pack(side="left")
        self.action_buttons.append(self.run_btn)

        self.retry_btn = ttk.Button(actions, text="失败重跑", command=self._run_retry)
        self.retry_btn.pack(side="left", padx=8)
        self.action_buttons.append(self.retry_btn)

        self.stop_btn = ttk.Button(actions, text="停止", command=self._stop_processing, state="disabled")
        self.stop_btn.pack(side="left")

        ttk.Button(actions, text="打开输出目录", command=self._open_output_dir).pack(side="left", padx=8)

        self._build_candidate_card(root)
        self._build_snapshot_card(root)

        log_card = ttk.LabelFrame(root, text="运行日志", style="Card.TLabelframe")
        log_card.pack(fill="both", expand=True)
        self.log_text = tk.Text(log_card, wrap="word", bg="#0f1d28", fg="#d9f2ff", insertbackground="#d9f2ff")
        self.log_text.pack(fill="both", expand=True)

    def _build_candidate_card(self, parent: ttk.Frame) -> None:
        self.candidate_card = ttk.LabelFrame(parent, text="候选链接选择（主界面内嵌）", style="Card.TLabelframe")
        self.candidate_card.pack(fill="both", expand=False, pady=(8, 0))
        self.candidate_card.pack_forget()

        header = ttk.Frame(self.candidate_card)
        header.pack(fill="x")
        ttk.Label(
            header,
            text="可多选：点击“选择”列打勾；支持全选未下载/全选/反选。右侧显示封面预览。",
            style="Hint.TLabel",
        ).pack(side="left")
        ttk.Label(header, textvariable=self.candidate_summary_var, foreground="#1f4b6e").pack(side="right")

        body = ttk.Panedwindow(self.candidate_card, orient="horizontal")
        body.pack(fill="both", expand=True, pady=(8, 0))

        left = ttk.Frame(body)
        right = ttk.Frame(body, padding=(8, 0, 0, 0))
        body.add(left, weight=4)
        body.add(right, weight=3)

        tree_wrap = ttk.Frame(left)
        tree_wrap.pack(fill="both", expand=True)
        self.candidate_tree = ttk.Treeview(
            tree_wrap,
            columns=("pick", "status", "title", "url"),
            show="headings",
            selectmode="extended",
            height=10,
        )
        self.candidate_tree.heading("pick", text="选择")
        self.candidate_tree.heading("status", text="状态")
        self.candidate_tree.heading("title", text="标题")
        self.candidate_tree.heading("url", text="链接")
        self.candidate_tree.column("pick", width=60, anchor="center", stretch=False)
        self.candidate_tree.column("status", width=80, anchor="center", stretch=False)
        self.candidate_tree.column("title", width=260, anchor="w")
        self.candidate_tree.column("url", width=420, anchor="w")
        self.candidate_tree.pack(side="left", fill="both", expand=True)
        self.candidate_tree.bind("<Button-1>", self._on_candidate_tree_click, add="+")
        self.candidate_tree.bind("<<TreeviewSelect>>", self._on_candidate_tree_select, add="+")

        y_scroll = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.candidate_tree.yview)
        y_scroll.pack(side="right", fill="y")
        self.candidate_tree.configure(yscrollcommand=y_scroll.set)

        ctrl = ttk.Frame(left)
        ctrl.pack(fill="x", pady=(8, 0))
        btn_new = ttk.Button(ctrl, text="全选未下载", command=self._candidate_select_unseen)
        btn_new.pack(side="left")
        self.candidate_buttons.append(btn_new)

        btn_all = ttk.Button(ctrl, text="全选", command=self._candidate_select_all)
        btn_all.pack(side="left", padx=6)
        self.candidate_buttons.append(btn_all)

        btn_clear = ttk.Button(ctrl, text="清空", command=self._candidate_clear)
        btn_clear.pack(side="left")
        self.candidate_buttons.append(btn_clear)

        btn_invert = ttk.Button(ctrl, text="反选", command=self._candidate_invert)
        btn_invert.pack(side="left", padx=6)
        self.candidate_buttons.append(btn_invert)

        ttk.Checkbutton(ctrl, text="已下载项按强制重下处理", variable=self.force_seen_var).pack(side="left", padx=(12, 0))

        c_actions = ttk.Frame(left)
        c_actions.pack(fill="x", pady=(8, 0))
        btn_back = ttk.Button(c_actions, text="返回编辑", command=self._back_from_candidates)
        btn_back.pack(side="right")
        self.candidate_buttons.append(btn_back)
        btn_ok = ttk.Button(c_actions, text="确认并开始", command=self._confirm_candidates)
        btn_ok.pack(side="right", padx=8)
        self.candidate_buttons.append(btn_ok)

        ttk.Label(right, text="候选预览", font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w")
        self.preview_image_label = tk.Label(
            right,
            width=46,
            height=14,
            bg="#d7dee4",
            fg="#324250",
            text="暂无封面",
            relief="ridge",
        )
        self.preview_image_label.pack(fill="x", pady=(8, 8))

        ttk.Label(right, text="标题：", foreground="#4d5b66").pack(anchor="w")
        ttk.Label(right, textvariable=self.preview_title_var, wraplength=380).pack(anchor="w", pady=(0, 8))
        ttk.Label(right, text="状态：", foreground="#4d5b66").pack(anchor="w")
        ttk.Label(right, textvariable=self.preview_status_var, wraplength=380).pack(anchor="w", pady=(0, 8))
        ttk.Label(right, text="链接：", foreground="#4d5b66").pack(anchor="w")
        ttk.Label(right, textvariable=self.preview_url_var, wraplength=380, foreground="#1f4b6e").pack(anchor="w")

    def _build_snapshot_card(self, parent: ttk.Frame) -> None:
        snap_card = ttk.LabelFrame(parent, text="视频关键节点截图", style="Card.TLabelframe")
        snap_card.pack(fill="x", pady=(8, 0))

        r1 = ttk.Frame(snap_card)
        r1.pack(fill="x", pady=(0, 6))
        ttk.Label(r1, text="视频文件", width=10).pack(side="left")
        ttk.Entry(r1, textvariable=self.snap_video_var).pack(side="left", fill="x", expand=True)
        ttk.Button(r1, text="选择", command=self._choose_snap_video).pack(side="left", padx=6)

        r2 = ttk.Frame(snap_card)
        r2.pack(fill="x", pady=(0, 6))
        ttk.Label(r2, text="SRT文件", width=10).pack(side="left")
        ttk.Entry(r2, textvariable=self.snap_srt_var).pack(side="left", fill="x", expand=True)
        ttk.Button(r2, text="选择", command=self._choose_snap_srt).pack(side="left", padx=6)
        ttk.Label(r2, text="Prompt输出", width=10).pack(side="left", padx=(8, 0))
        ttk.Entry(r2, textvariable=self.snap_prompt_out_var).pack(side="left", fill="x", expand=True)
        ttk.Button(r2, text="选择", command=self._choose_snap_prompt_out).pack(side="left", padx=6)

        r3 = ttk.Frame(snap_card)
        r3.pack(fill="x")
        ttk.Label(r3, text="AI输出", width=10).pack(side="left")
        ttk.Entry(r3, textvariable=self.snap_ai_output_var).pack(side="left", fill="x", expand=True)
        ttk.Button(r3, text="选择", command=self._choose_snap_ai_output).pack(side="left", padx=6)
        ttk.Label(r3, text="截图目录", width=10).pack(side="left", padx=(8, 0))
        ttk.Entry(r3, textvariable=self.snap_output_dir_var).pack(side="left", fill="x", expand=True)
        ttk.Button(r3, text="选择", command=self._choose_snap_output_dir).pack(side="left", padx=6)
        ttk.Label(r3, text="最大张数").pack(side="left", padx=(8, 0))
        ttk.Entry(r3, textvariable=self.snap_max_shots_var, width=5).pack(side="left")
        ttk.Label(r3, text="最小间隔秒").pack(side="left", padx=(8, 0))
        ttk.Entry(r3, textvariable=self.snap_min_gap_var, width=5).pack(side="left")

        r4 = ttk.Frame(snap_card)
        r4.pack(fill="x", pady=(8, 0))
        self.snap_prompt_btn = ttk.Button(r4, text="1) 生成关键节点Prompt", command=self._snapshot_make_prompt)
        self.snap_prompt_btn.pack(side="left")
        self.snap_capture_btn = ttk.Button(r4, text="2) 解析AI输出并截图", command=self._snapshot_capture)
        self.snap_capture_btn.pack(side="left", padx=8)

    def _toggle_advanced(self) -> None:
        if self.adv_frame is None:
            return
        if self.advanced_visible:
            self.adv_frame.pack_forget()
            self.advanced_visible = False
        else:
            self.adv_frame.pack(fill="x", pady=(8, 0))
            self.advanced_visible = True

    def _add_file(self) -> None:
        path = filedialog.askopenfilename(
            title="选择音频/视频文件",
            filetypes=[("媒体文件", "*.mp3 *.wav *.m4a *.caf *.mp4 *.mkv *.mov *.webm"), ("全部文件", "*.*")],
        )
        if path:
            self._append_input(path)

    def _add_folder(self) -> None:
        path = filedialog.askdirectory(title="选择文件夹")
        if path:
            self._append_input(path)

    def _add_url(self) -> None:
        url = simpledialog.askstring("添加链接", "请输入 URL：")
        if url:
            self._append_input(url.strip())

    def _append_input(self, value: str) -> None:
        if not self.input_text:
            return
        text = self.input_text.get("1.0", "end").strip()
        if text:
            self.input_text.insert("end", "\n")
        self.input_text.insert("end", value)

    def _clear_inputs(self) -> None:
        if self.input_text:
            self.input_text.delete("1.0", "end")

    def _choose_output(self) -> None:
        path = filedialog.askdirectory(title="选择输出目录")
        if path:
            self.out_var.set(path)

    def _choose_config(self) -> None:
        path = filedialog.askopenfilename(title="选择配置文件", filetypes=[("JSON 文件", "*.json"), ("全部文件", "*.*")])
        if path:
            self.config_var.set(path)

    def _choose_failed_log(self) -> None:
        path = filedialog.askopenfilename(
            title="选择 failed_tasks.jsonl",
            filetypes=[("JSONL 文件", "*.jsonl"), ("全部文件", "*.*")],
        )
        if path:
            self.failed_log_var.set(path)

    def _choose_snap_video(self) -> None:
        path = filedialog.askopenfilename(title="选择视频文件", filetypes=[("视频文件", "*.mp4 *.mkv *.mov *.webm"), ("全部文件", "*.*")])
        if not path:
            return
        self.snap_video_var.set(path)
        video_path = Path(path)
        if not self.snap_srt_var.get().strip():
            self.snap_srt_var.set(str(video_path.with_suffix(".srt")))
        if not self.snap_prompt_out_var.get().strip():
            self.snap_prompt_out_var.set(str(video_path.with_name(f"{video_path.stem}_snapshot_prompt.txt")))
        if not self.snap_output_dir_var.get().strip():
            self.snap_output_dir_var.set(str((video_path.parent / "snapshots" / video_path.stem).resolve()))

    def _choose_snap_srt(self) -> None:
        path = filedialog.askopenfilename(title="选择SRT文件", filetypes=[("SRT 文件", "*.srt"), ("全部文件", "*.*")])
        if path:
            self.snap_srt_var.set(path)

    def _choose_snap_prompt_out(self) -> None:
        path = filedialog.asksaveasfilename(
            title="选择Prompt输出路径",
            defaultextension=".txt",
            filetypes=[("文本文件", "*.txt"), ("全部文件", "*.*")],
        )
        if path:
            self.snap_prompt_out_var.set(path)

    def _choose_snap_ai_output(self) -> None:
        path = filedialog.askopenfilename(title="选择AI输出文件", filetypes=[("文本/JSON", "*.txt *.json"), ("全部文件", "*.*")])
        if path:
            self.snap_ai_output_var.set(path)

    def _choose_snap_output_dir(self) -> None:
        path = filedialog.askdirectory(title="选择截图目录")
        if path:
            self.snap_output_dir_var.set(path)

    def _get_input_items(self) -> list[str]:
        if not self.input_text:
            return []
        raw = self.input_text.get("1.0", "end").splitlines()
        return [line.strip() for line in raw if line.strip()]

    def _split_csv(self, raw: str, default: list[str]) -> list[str]:
        if not raw:
            return list(default)
        values = [x.strip() for x in raw.split(",") if x.strip()]
        return values or list(default)

    def _safe_int(self, raw: str, default: int, min_value: int | None = None) -> int:
        try:
            value = int(raw)
        except ValueError:
            return default
        if min_value is not None and value < min_value:
            return default
        return value

    def _safe_float(self, raw: str, default: float, min_value: float | None = None) -> float:
        try:
            value = float(raw)
        except ValueError:
            return default
        if min_value is not None and value < min_value:
            return default
        return value

    def _build_runtime_config(self) -> tuple[AppConfig, Path, Path]:
        config_path = Path(self.config_var.get().strip() or "config.json").expanduser()
        if not config_path.is_absolute():
            config_path = (Path.cwd() / config_path).resolve()
        cfg = load_config(config_path if config_path.exists() else None)

        cfg.whisper.model = self.model_var.get().strip() or cfg.whisper.model
        cfg.whisper.language = self.language_var.get().strip() or cfg.whisper.language
        cfg.scraping.candidate_mode = self.candidate_mode_var.get().strip() or "select"
        cfg.download.quality = self.quality_var.get().strip() or cfg.download.quality
        cfg.scraping.always_try_page_url = bool(self.always_try_page_var.get())
        cfg.download.prefer_compatible_codecs = bool(self.prefer_compatible_var.get())
        cfg.download.allow_separate_streams = bool(self.allow_separate_var.get())
        cfg.download.js_runtimes = self._split_csv(self.js_runtimes_var.get().strip(), default=["deno", "node"])
        cfg.download.remote_components = self._split_csv(
            self.remote_components_var.get().strip(),
            default=["ejs:github"],
        )
        cfg.scraping.download_archive = self.download_archive_var.get().strip() or cfg.scraping.download_archive
        cfg.scraping.user_agent = self.user_agent_var.get().strip() or cfg.scraping.user_agent

        timeout_raw = self.request_timeout_var.get().strip()
        if timeout_raw:
            try:
                timeout = int(timeout_raw)
                if timeout > 0:
                    cfg.scraping.request_timeout_seconds = timeout
            except ValueError:
                pass

        base_dir = config_path.parent if config_path.exists() else Path.cwd()
        output_root = ensure_output_root(
            base_dir=base_dir,
            output_root=self.out_var.get().strip() or cfg.output_root,
        )
        self.current_output_root = output_root
        return cfg, config_path, output_root

    def _run_processing(self) -> None:
        if self._process_running:
            return
        if self._candidate_mode_active:
            messagebox.showinfo("提示", "请先在候选区确认或返回。")
            return

        input_items = self._get_input_items()
        if not input_items:
            messagebox.showwarning("提示", "请输入至少一个输入项。")
            return

        ffmpeg_ok, ffmpeg_message = check_ffmpeg_available("ffmpeg")
        if not ffmpeg_ok:
            messagebox.showerror("ffmpeg 不可用", ffmpeg_message)
            return
        self._append_log(f"[INFO] Using ffmpeg: {ffmpeg_message}")

        cfg, config_path, output_root = self._build_runtime_config()
        self._append_log(f"[INFO] 输出目录: {output_root}")

        if cfg.scraping.candidate_mode == "select" and any(is_url(item) for item in input_items):
            self._prepare_candidate_selection(input_items=input_items, cfg=cfg, output_root=output_root)
            return

        self._start_run_subprocess(
            cfg=cfg,
            config_path=config_path,
            output_root=output_root,
            input_items=input_items,
            force_keys=set(),
        )

    def _run_retry(self) -> None:
        if self._process_running or self._candidate_mode_active:
            return

        failed_log = self.failed_log_var.get().strip()
        if not failed_log:
            messagebox.showwarning("提示", "请先选择 failed_tasks.jsonl。")
            return

        failed_path = Path(failed_log).expanduser().resolve()
        if not failed_path.exists():
            messagebox.showerror("错误", f"文件不存在：{failed_path}")
            return

        cfg, config_path, output_root = self._build_runtime_config()
        run_id = uuid.uuid4().hex[:12]
        self.current_run_id = run_id
        self.current_output_root = output_root

        cmd = [
            sys.executable,
            str((Path(__file__).resolve().parents[1] / "media_tool.py")),
            "retry-failed",
            "--failed-log",
            str(failed_path),
            "--out",
            str(output_root),
            "--config",
            str(config_path),
            "--model",
            cfg.whisper.model,
            "--language",
            cfg.whisper.language,
            "--run-id",
            run_id,
        ]
        self._launch_subprocess(cmd)

    def _prepare_candidate_selection(self, input_items: list[str], cfg: AppConfig, output_root: Path) -> None:
        self._append_log("[INFO] 开始扫描候选链接...")

        archive_path = Path(cfg.scraping.download_archive).expanduser()
        if not archive_path.is_absolute():
            archive_path = (output_root / archive_path).resolve()
        seen_keys = load_seen_archive(archive_path)

        direct_inputs: list[str] = []
        candidate_urls: list[tuple[str, str]] = []
        seen_candidate_urls: set[str] = set()

        for raw in input_items:
            if not is_url(raw):
                direct_inputs.append(raw)
                continue

            try:
                found = discover_targets(
                    pages=[raw],
                    timeout=int(cfg.scraping.request_timeout_seconds),
                    user_agent=cfg.scraping.user_agent,
                    always_try_page_url=bool(cfg.scraping.always_try_page_url),
                )
                targets = sorted(found) if found else [raw]
            except Exception as exc:  # noqa: BLE001
                self._append_log(f"[WARN] 候选扫描失败，回退直接链接: {raw} ({exc})")
                targets = [raw]

            seen_local: set[str] = set()
            for target in targets:
                if target in seen_local:
                    continue
                if target in seen_candidate_urls:
                    continue
                seen_local.add(target)
                seen_candidate_urls.add(target)
                candidate_urls.append((raw, target))

        if not candidate_urls:
            self._append_log("[INFO] 没有候选链接，直接运行。")
            config_path = Path(self.config_var.get().strip() or "config.json").expanduser()
            if not config_path.is_absolute():
                config_path = (Path.cwd() / config_path).resolve()
            self._start_run_subprocess(
                cfg=cfg,
                config_path=config_path,
                output_root=output_root,
                input_items=direct_inputs,
                force_keys=set(),
            )
            return

        self._candidate_items.clear()
        self._candidate_order.clear()
        for idx, (source_url, target_url) in enumerate(candidate_urls, start=1):
            key = dedup_key(target_url)
            seen = key in seen_keys
            title_guess = self._guess_title_from_url(target_url)
            iid = f"cand-{idx}"
            self._candidate_items[iid] = CandidateItem(
                iid=iid,
                source_url=source_url,
                url=target_url,
                is_seen=seen,
                checked=not seen,
                title=title_guess,
            )
            self._candidate_order.append(iid)

        self._pending_run_context = (cfg, output_root, archive_path, direct_inputs)
        self._show_candidates()
        self._append_log(f"[INFO] 候选发现完成：{len(self._candidate_order)} 条。")

    def _show_candidates(self) -> None:
        if self.candidate_card is None or self.candidate_tree is None:
            return
        self._candidate_mode_active = True
        self._set_idle_buttons_state(enabled=False)
        if self.stop_btn:
            self.stop_btn.configure(state="disabled")

        self.candidate_card.pack(fill="both", expand=False, pady=(8, 0))
        self.candidate_tree.delete(*self.candidate_tree.get_children())
        for iid in self._candidate_order:
            item = self._candidate_items[iid]
            self.candidate_tree.insert(
                "",
                "end",
                iid=iid,
                values=(
                    "☑" if item.checked else "☐",
                    "已下载" if item.is_seen else "未下载",
                    item.title,
                    item.url,
                ),
            )

        self._refresh_candidate_summary()
        if self._candidate_order:
            first = self._candidate_order[0]
            self.candidate_tree.selection_set(first)
            self._update_candidate_preview(first)
            self._prefetch_candidate_meta(first)
            for iid in self._candidate_order[1:4]:
                self._prefetch_candidate_meta(iid)

        for btn in self.candidate_buttons:
            btn.configure(state="normal")

    def _hide_candidates(self) -> None:
        if self.candidate_card:
            self.candidate_card.pack_forget()
        self._candidate_mode_active = False
        self._pending_run_context = None
        self._candidate_items.clear()
        self._candidate_order.clear()
        self.preview_title_var.set("未选择候选视频")
        self.preview_url_var.set("")
        self.preview_status_var.set("")
        if self.preview_image_label:
            self.preview_image_label.configure(image="", text="暂无封面")
        self._preview_photo = None
        self._refresh_candidate_summary()
        self._set_idle_buttons_state(enabled=True)
        for btn in self.candidate_buttons:
            btn.configure(state="disabled")

    def _on_candidate_tree_click(self, event: tk.Event) -> str | None:
        if self.candidate_tree is None:
            return None
        row_id = self.candidate_tree.identify_row(event.y)
        col = self.candidate_tree.identify_column(event.x)
        if not row_id:
            return None
        if col == "#1":
            self._toggle_candidate(row_id)
            self.candidate_tree.selection_set(row_id)
            self._update_candidate_preview(row_id)
            self._prefetch_candidate_meta(row_id)
            return "break"
        return None

    def _on_candidate_tree_select(self, _event: tk.Event) -> None:
        if self.candidate_tree is None:
            return
        selected = self.candidate_tree.selection()
        if not selected:
            return
        iid = selected[0]
        self._update_candidate_preview(iid)
        self._prefetch_candidate_meta(iid)

    def _toggle_candidate(self, iid: str) -> None:
        item = self._candidate_items.get(iid)
        if not item or self.candidate_tree is None:
            return
        item.checked = not item.checked
        self._refresh_tree_row(iid)
        self._refresh_candidate_summary()

    def _candidate_select_unseen(self) -> None:
        for iid in self._candidate_order:
            item = self._candidate_items[iid]
            item.checked = not item.is_seen
            self._refresh_tree_row(iid)
        self._refresh_candidate_summary()

    def _candidate_select_all(self) -> None:
        for iid in self._candidate_order:
            self._candidate_items[iid].checked = True
            self._refresh_tree_row(iid)
        self._refresh_candidate_summary()

    def _candidate_clear(self) -> None:
        for iid in self._candidate_order:
            self._candidate_items[iid].checked = False
            self._refresh_tree_row(iid)
        self._refresh_candidate_summary()

    def _candidate_invert(self) -> None:
        for iid in self._candidate_order:
            item = self._candidate_items[iid]
            item.checked = not item.checked
            self._refresh_tree_row(iid)
        self._refresh_candidate_summary()

    def _refresh_tree_row(self, iid: str) -> None:
        item = self._candidate_items.get(iid)
        if not item or self.candidate_tree is None:
            return
        self.candidate_tree.item(
            iid,
            values=(
                "☑" if item.checked else "☐",
                "已下载" if item.is_seen else "未下载",
                item.title,
                item.url,
            ),
        )

    def _refresh_candidate_summary(self) -> None:
        total = len(self._candidate_order)
        selected = sum(1 for iid in self._candidate_order if self._candidate_items[iid].checked)
        self.candidate_summary_var.set(f"共 {total} 条，已勾选 {selected} 条")

    def _confirm_candidates(self) -> None:
        context = self._pending_run_context
        if context is None:
            self._hide_candidates()
            return
        cfg, output_root, _archive_path, direct_inputs = context

        picked: list[str] = []
        force_keys: set[str] = set()
        for iid in self._candidate_order:
            item = self._candidate_items[iid]
            if not item.checked:
                continue
            picked.append(item.url)
            if item.is_seen and self.force_seen_var.get():
                force_keys.add(dedup_key(item.url))

        if not picked and not direct_inputs:
            messagebox.showwarning("提示", "没有勾选任何候选。")
            return

        config_path = Path(self.config_var.get().strip() or "config.json").expanduser()
        if not config_path.is_absolute():
            config_path = (Path.cwd() / config_path).resolve()

        run_items = [*direct_inputs, *picked]
        self._hide_candidates()
        self._start_run_subprocess(
            cfg=cfg,
            config_path=config_path,
            output_root=output_root,
            input_items=run_items,
            force_keys=force_keys,
        )

    def _back_from_candidates(self) -> None:
        self._append_log("[INFO] 已返回输入编辑。")
        self._hide_candidates()

    def _update_candidate_preview(self, iid: str) -> None:
        item = self._candidate_items.get(iid)
        if not item:
            return
        self.preview_title_var.set(item.title)
        self.preview_url_var.set(item.url)
        source_host = urlparse(item.source_url).netloc or "unknown"
        status = "已下载" if item.is_seen else "未下载"
        self.preview_status_var.set(f"{status} | 来源页: {source_host}")
        self._render_preview_image(item)

    def _prefetch_candidate_meta(self, iid: str) -> None:
        item = self._candidate_items.get(iid)
        if not item or item.meta_loaded or item.meta_loading:
            return
        item.meta_loading = True
        worker = threading.Thread(target=self._candidate_meta_worker, args=(iid,), daemon=True)
        worker.start()

    def _candidate_meta_worker(self, iid: str) -> None:
        item = self._candidate_items.get(iid)
        if not item:
            return

        title = item.title
        thumb_url = item.thumbnail_url
        thumb_bytes: bytes | None = None

        try:
            meta = self._extract_video_meta(item.url)
            if meta.get("title"):
                title = str(meta["title"])
            if meta.get("thumbnail"):
                thumb_url = str(meta["thumbnail"])
        except Exception:
            pass

        if not thumb_url:
            vid = extract_youtube_video_id(item.url)
            if vid:
                thumb_url = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"

        if thumb_url and requests is not None:
            try:
                response = requests.get(
                    thumb_url,
                    timeout=8,
                    headers={"User-Agent": self.user_agent_var.get().strip() or "Mozilla/5.0"},
                )
                response.raise_for_status()
                if response.content:
                    thumb_bytes = response.content
            except Exception:
                thumb_bytes = None

        self.after(0, self._apply_candidate_meta, iid, title, thumb_url, thumb_bytes)

    def _apply_candidate_meta(self, iid: str, title: str, thumb_url: str, thumb_bytes: bytes | None) -> None:
        item = self._candidate_items.get(iid)
        if not item:
            return
        item.title = title or item.title
        item.thumbnail_url = thumb_url or item.thumbnail_url
        item.thumb_bytes = thumb_bytes
        item.meta_loaded = True
        item.meta_loading = False
        self._refresh_tree_row(iid)

        if self.candidate_tree is not None and self.candidate_tree.selection():
            selected = self.candidate_tree.selection()[0]
            if selected == iid:
                self._update_candidate_preview(iid)

    def _extract_video_meta(self, url: str) -> dict:
        if YoutubeDL is None:
            return {}
        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
            "extract_flat": False,
        }
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if not isinstance(info, dict):
            return {}
        return {
            "title": info.get("title"),
            "thumbnail": info.get("thumbnail"),
        }

    def _render_preview_image(self, item: CandidateItem) -> None:
        if self.preview_image_label is None:
            return
        if not item.thumb_bytes or Image is None or ImageTk is None:
            self._preview_photo = None
            self.preview_image_label.configure(image="", text="暂无封面")
            return

        try:
            image = Image.open(io.BytesIO(item.thumb_bytes))
            image.thumbnail((420, 240))
            photo = ImageTk.PhotoImage(image)
        except Exception:
            self._preview_photo = None
            self.preview_image_label.configure(image="", text="封面加载失败")
            return

        self._preview_photo = photo
        self.preview_image_label.configure(image=photo, text="")

    def _guess_title_from_url(self, url: str) -> str:
        parsed = urlparse(url)
        host = parsed.netloc.lower().replace("www.", "")
        tail = parsed.path.strip("/").split("/")[-1] if parsed.path else ""
        if tail:
            return f"{host}/{tail}"
        return host or url

    def _start_run_subprocess(
        self,
        cfg: AppConfig,
        config_path: Path,
        output_root: Path,
        input_items: list[str],
        force_keys: set[str],
    ) -> None:
        if not input_items:
            messagebox.showwarning("提示", "没有可执行的输入项。")
            return

        run_id = uuid.uuid4().hex[:12]
        self.current_run_id = run_id
        self.current_output_root = output_root
        self.stop_requested = False

        cmd = [
            sys.executable,
            str((Path(__file__).resolve().parents[1] / "media_tool.py")),
            "run",
            "--out",
            str(output_root),
            "--config",
            str(config_path),
            "--model",
            cfg.whisper.model,
            "--language",
            cfg.whisper.language,
            "--candidate-mode",
            "auto",
            "--download-archive",
            cfg.scraping.download_archive,
            "--quality",
            cfg.download.quality,
            "--request-timeout",
            str(cfg.scraping.request_timeout_seconds),
            "--run-id",
            run_id,
        ]
        cmd.append("--always-try-page" if cfg.scraping.always_try_page_url else "--no-always-try-page")
        cmd.append("--prefer-compatible-codecs" if cfg.download.prefer_compatible_codecs else "--no-prefer-compatible-codecs")
        cmd.append("--allow-separate-streams" if cfg.download.allow_separate_streams else "--no-allow-separate-streams")

        if cfg.scraping.user_agent:
            cmd.extend(["--user-agent", cfg.scraping.user_agent])
        for runtime in cfg.download.js_runtimes:
            cmd.extend(["--js-runtime", runtime])
        for component in cfg.download.remote_components:
            cmd.extend(["--remote-component", component])
        for key in sorted(force_keys):
            cmd.extend(["--force-key", key])
        for item in input_items:
            cmd.extend(["--input", item])

        self._launch_subprocess(cmd)

    def _launch_subprocess(self, cmd: list[str]) -> None:
        self._append_log(f"[INFO] 启动命令: {' '.join(cmd)}")
        popen_kwargs = {}
        if sys.platform.startswith("win"):
            popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                **popen_kwargs,
            )
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("启动失败", str(exc))
            return

        self.proc = proc
        self._set_running_state(True)
        self.worker_thread = threading.Thread(target=self._read_process_output, daemon=True)
        self.worker_thread.start()

    def _read_process_output(self) -> None:
        if self.proc is None:
            return
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self.log_queue.put(line.rstrip("\r\n"))
        code = self.proc.wait()
        self.log_queue.put(("__PROCESS_DONE__", code))

    def _drain_logs(self) -> None:
        while True:
            try:
                item = self.log_queue.get_nowait()
            except queue.Empty:
                break

            if isinstance(item, tuple) and item and item[0] == "__PROCESS_DONE__":
                self._on_process_finished(int(item[1]))
            elif isinstance(item, str):
                self._append_log(item)

        self.after(120, self._drain_logs)

    def _append_log(self, text: str) -> None:
        if not self.log_text:
            return
        self.log_text.insert("end", text + "\n")
        self.log_text.see("end")

    def _set_idle_buttons_state(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for button in self.action_buttons:
            button.configure(state=state)

    def _set_running_state(self, running: bool) -> None:
        self._process_running = running
        self._set_idle_buttons_state(enabled=not running and not self._candidate_mode_active)
        if self.stop_btn:
            self.stop_btn.configure(state="normal" if running else "disabled")
        if self.snap_prompt_btn:
            self.snap_prompt_btn.configure(state="disabled" if running else "normal")
        if self.snap_capture_btn:
            self.snap_capture_btn.configure(state="disabled" if running else "normal")
        state = "disabled" if running else "normal"
        for btn in self.candidate_buttons:
            btn.configure(state=state if self._candidate_mode_active else "disabled")

    def _stop_processing(self) -> None:
        if self.proc is None or self.proc.poll() is not None:
            return
        self.stop_requested = True
        self._append_log("[INFO] 收到停止请求，正在终止当前运行...")
        try:
            self.proc.terminate()
        except Exception as exc:  # noqa: BLE001
            self._append_log(f"[WARN] terminate 失败: {exc}")
        self.after(2000, self._force_kill_if_needed)

    def _force_kill_if_needed(self) -> None:
        if self.proc is None or self.proc.poll() is not None:
            return
        self._append_log("[WARN] 进程未退出，执行强制停止。")
        try:
            self.proc.kill()
        except Exception as exc:  # noqa: BLE001
            self._append_log(f"[WARN] kill 失败: {exc}")

    def _on_process_finished(self, return_code: int) -> None:
        self._set_running_state(False)
        self.proc = None
        self.worker_thread = None

        if self.stop_requested:
            self._append_log("[INFO] 已按用户请求停止。")
            self._append_stopped_record()
            self._ask_rollback_after_stop()
        elif return_code == 0:
            self._append_log("[DONE] 任务完成。")
        else:
            self._append_log(f"[ERROR] 进程退出码: {return_code}")

        self.stop_requested = False

    def _append_stopped_record(self) -> None:
        if not self.current_output_root:
            return
        failed_path = self.current_output_root / "failed_tasks.jsonl"
        if self.failed_log_var.get().strip():
            failed_path = Path(self.failed_log_var.get().strip()).expanduser().resolve()
        record = {
            "run_id": self.current_run_id or "",
            "status": "failed",
            "stage": "stopped_by_user",
            "error": "Stopped by user in GUI",
        }
        append_jsonl(failed_path, record)

    def _ask_rollback_after_stop(self) -> None:
        if not self.current_output_root or not self.current_run_id:
            return
        choice = messagebox.askyesnocancel(
            "停止后回退",
            "选择回退范围：\n是 = 仅回退当前任务\n否 = 回退本次运行\n取消 = 保留已生成内容",
        )
        if choice is None:
            self._append_log("[INFO] 保留当前已生成内容。")
            return
        scope = "task" if choice else "run"
        result = rollback_from_ledger(self.current_output_root, self.current_run_id, scope=scope)
        self._append_log(
            f"[INFO] 回退完成（scope={scope}）：删除 {result.get('deleted', 0)}，跳过 {result.get('skipped', 0)}。"
        )

    def _open_output_dir(self) -> None:
        _, _, output_root = self._build_runtime_config()
        try:
            os.startfile(str(output_root))  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("打开失败", str(exc))

    def _snapshot_make_prompt(self) -> None:
        video_path = Path(self.snap_video_var.get().strip()).expanduser()
        if not video_path.exists():
            messagebox.showerror("错误", "视频文件不存在。")
            return

        srt_path_raw = self.snap_srt_var.get().strip()
        srt_path = Path(srt_path_raw).expanduser() if srt_path_raw else video_path.with_suffix(".srt")
        if not srt_path.exists():
            messagebox.showerror("错误", f"SRT 不存在：{srt_path}")
            return

        out_raw = self.snap_prompt_out_var.get().strip()
        if not out_raw:
            out_raw = str(video_path.with_name(f"{video_path.stem}_snapshot_prompt.txt"))
            self.snap_prompt_out_var.set(out_raw)
        out_path = Path(out_raw).expanduser()

        max_shots = self._safe_int(self.snap_max_shots_var.get().strip(), default=15, min_value=1)

        try:
            srt_text = srt_path.read_text(encoding="utf-8", errors="replace")
            prompt = build_ai_prompt_from_srt(srt_text=srt_text, max_points=max_shots)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(prompt, encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("生成失败", str(exc))
            return

        self._append_log(f"[INFO] 快照Prompt已写入: {out_path}")
        messagebox.showinfo("完成", f"Prompt 已生成：\n{out_path}")

    def _snapshot_capture(self) -> None:
        ffmpeg_ok, ffmpeg_message = check_ffmpeg_available("ffmpeg")
        if not ffmpeg_ok:
            messagebox.showerror("ffmpeg 不可用", ffmpeg_message)
            return

        video_path = Path(self.snap_video_var.get().strip()).expanduser()
        ai_path = Path(self.snap_ai_output_var.get().strip()).expanduser()
        if not video_path.exists():
            messagebox.showerror("错误", "视频文件不存在。")
            return
        if not ai_path.exists():
            messagebox.showerror("错误", "AI 输出文件不存在。")
            return

        out_dir_raw = self.snap_output_dir_var.get().strip()
        if out_dir_raw:
            out_dir = Path(out_dir_raw).expanduser()
        else:
            out_dir = (video_path.parent / "snapshots" / video_path.stem).resolve()
            self.snap_output_dir_var.set(str(out_dir))

        max_shots = self._safe_int(self.snap_max_shots_var.get().strip(), default=15, min_value=1)
        min_gap = self._safe_float(self.snap_min_gap_var.get().strip(), default=8.0, min_value=0.0)

        try:
            ai_text = ai_path.read_text(encoding="utf-8", errors="replace")
            raw_points = extract_timepoints_from_ai_output(ai_text)
            if not raw_points:
                raise RuntimeError("AI 输出中没有可解析时间点")

            duration = get_media_duration_seconds(video_path, ffprobe_bin="ffprobe")
            points = clamp_timepoints(raw_points, duration_seconds=duration)
            points = dedupe_timepoints(points, min_gap_seconds=min_gap, max_points=max_shots)
            if not points:
                raise RuntimeError("过滤后没有可用时间点")

            shots = capture_snapshots(video_path, points, out_dir, ffmpeg_bin=ffmpeg_message)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("截图失败", str(exc))
            return

        self._append_log(f"[INFO] 已截图 {len(shots)} 张 -> {out_dir}")
        messagebox.showinfo("完成", f"截图完成：{len(shots)} 张\n目录：{out_dir}")


def main() -> None:
    app = MediaToolGUI()
    app.mainloop()


if __name__ == "__main__":
    main()
