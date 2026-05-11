from __future__ import annotations

import json
import sys
import threading
import uuid
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from urllib.parse import unquote, urlparse

try:
    import requests
except Exception:  # noqa: BLE001
    requests = None

try:
    from yt_dlp import YoutubeDL
except Exception:  # noqa: BLE001
    YoutubeDL = None

from .config import AppConfig, ensure_output_root, load_config
from .ffmpeg_utils import check_ffmpeg_available
from .io_utils import append_jsonl, is_direct_document_url, is_direct_media_url, is_url, read_jsonl
from .run_ledger import rollback_from_ledger
from .scraper_engine import dedup_key, discover_targets, extract_youtube_video_id, load_seen_archive

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QProcess, QProcessEnvironment, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QTabWidget,
    QTableView,
    QTextEdit,
    QToolButton,
    QStyledItemDelegate,
    QStyle,
    QVBoxLayout,
    QWidget,
)

try:
    from qfluentwidgets import Theme, setTheme
except Exception:  # noqa: BLE001
    Theme = None
    setTheme = None


@dataclass
class CandidateItem:
    item_id: str
    source_url: str
    url: str
    is_seen: bool
    checked: bool
    title: str
    source_kind: str = "url"
    thumbnail_url: str = ""
    thumb_bytes: bytes | None = None
    meta_loaded: bool = False
    meta_loading: bool = False


class UiStep(Enum):
    INPUT = "input"
    SELECTING = "selecting"
    PROCESSING = "processing"
    DONE = "done"


INVALID_CANDIDATE_TITLES = {"file", "file.mp4", "video", "watch", "index", "master", "unknown"}


class CandidateTableModel(QAbstractTableModel):
    HEADERS = ["选择", "状态", "标题", "来源"]

    def __init__(self, window: "FluentMedia2TextWindow") -> None:
        super().__init__(window)
        self.window = window

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return len(self.window._candidate_order)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return len(self.HEADERS)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self.HEADERS[section]
        return None

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid():
            return None
        item = self.window._candidate_item_at(index.row())
        if item is None:
            return None

        col = index.column()
        if role == Qt.DisplayRole:
            if col == 0:
                return ""
            if col == 1:
                return self.window._candidate_status_text(item)
            if col == 2:
                return item.title
            if col == 3:
                return self.window._candidate_source_text(item)
        if role == Qt.TextAlignmentRole and col == 0:
            return Qt.AlignCenter
        if role == Qt.DecorationRole and col == 2:
            return self.window._build_thumb_icon(item)
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.NoItemFlags
        return Qt.ItemIsEnabled | Qt.ItemIsSelectable

    def reset_rows(self) -> None:
        self.beginResetModel()
        self.endResetModel()

    def refresh_row(self, row: int) -> None:
        if row < 0 or row >= self.rowCount():
            return
        left = self.index(row, 0)
        right = self.index(row, self.columnCount() - 1)
        self.dataChanged.emit(left, right, [Qt.DisplayRole, Qt.DecorationRole, Qt.TextAlignmentRole])


class CandidateCheckDelegate(QStyledItemDelegate):
    def __init__(self, window: "FluentMedia2TextWindow") -> None:
        super().__init__(window)
        self.window = window

    def paint(self, painter: QPainter, option, index: QModelIndex) -> None:
        if index.column() != 0:
            super().paint(painter, option, index)
            return
        item = self.window._candidate_item_at(index.row())
        if item is None:
            return

        self.initStyleOption(option, index)
        painter.save()
        style = option.widget.style() if option.widget else QApplication.style()
        style.drawPrimitive(QStyle.PE_PanelItemViewItem, option, painter, option.widget)

        checkbox = QCheckBox(option.widget)
        checkbox.setObjectName("TableCheckBox")
        checkbox.setChecked(item.checked)
        checkbox.resize(22, 22)
        checkbox.ensurePolished()
        pixmap = checkbox.grab()
        point = option.rect.center() - pixmap.rect().center()
        painter.drawPixmap(point, pixmap)
        checkbox.deleteLater()
        painter.restore()


class NewTaskPage(QWidget):
    def __init__(self, window: "FluentMedia2TextWindow") -> None:
        super().__init__(window)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)
        layout.addWidget(window._build_input_card())
        layout.addStretch(1)


class CandidateSelectionPage(QWidget):
    def __init__(self, window: "FluentMedia2TextWindow") -> None:
        super().__init__(window)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)
        layout.addWidget(window._build_candidate_group())


class ProcessingPage(QWidget):
    def __init__(self, window: "FluentMedia2TextWindow") -> None:
        super().__init__(window)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)
        layout.addWidget(window._build_processing_group())
        layout.addStretch(1)


class ResultsPage(QWidget):
    def __init__(self, window: "FluentMedia2TextWindow") -> None:
        super().__init__(window)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)

        title = QLabel("结果输出")
        title.setObjectName("PageTitle")
        layout.addWidget(title)

        window.result_text = QTextEdit()
        window.result_text.setReadOnly(True)
        window.result_text.setPlaceholderText("处理完成后，文本结果会显示在这里。")
        layout.addWidget(window.result_text, stretch=1)

        row = QHBoxLayout()
        row.addStretch(1)
        export_txt = QPushButton("导出 TXT")
        export_md = QPushButton("导出 MD")
        export_txt.clicked.connect(lambda: window._export_result_text("txt"))
        export_md.clicked.connect(lambda: window._export_result_text("md"))
        row.addWidget(export_txt)
        row.addWidget(export_md)
        layout.addLayout(row)


class SettingsPage(QWidget):
    def __init__(self, window: "FluentMedia2TextWindow") -> None:
        super().__init__(window)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)
        layout.addWidget(window._build_settings_card())
        layout.addStretch(1)


class FluentMedia2TextWindow(QMainWindow):
    candidate_scan_finished = Signal(object)
    candidate_meta_ready = Signal(str, str, str, object)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Aequora")
        self.resize(1360, 940)
        self.setMinimumSize(1120, 780)

        self.proc: QProcess | None = None
        self.stop_requested = False
        self._process_running = False
        self._table_updating = False
        self._scan_in_progress = False
        self.current_step = UiStep.INPUT

        self.current_run_id: str | None = None
        self.current_output_root: Path | None = None

        self._candidate_mode_active = False
        self._candidate_items: dict[str, CandidateItem] = {}
        self._candidate_order: list[str] = []
        self._candidate_rows: list[str] = []
        self._pending_run_context: tuple[AppConfig, Path, Path, list[str], Path] | None = None
        self._parsed_input_items: list[str] = []
        self._candidate_scan_token = 0
        self._input_mode: str = "auto"
        self._progress_total = 0
        self._progress_completed = 0
        self._progress_success = 0
        self._progress_failed = 0
        self._progress_skipped = 0
        self._progress_current_index = 0
        self._current_task_total = 0
        self._current_task_name = ""
        self._current_step_name = ""

        self.stack: QStackedWidget | None = None
        self.nav_buttons: dict[UiStep | str, QPushButton] = {}
        self.input_group: QWidget | None = None
        self.input_edit: QTextEdit | None = None
        self.log_edit: QPlainTextEdit | None = None
        self.log_group: QGroupBox | None = None
        self.candidate_group: QGroupBox | None = None
        self.candidate_table: QTableView | None = None
        self.candidate_model: CandidateTableModel | None = None
        self.preview_image: QLabel | None = None
        self.preview_title: QLabel | None = None
        self.preview_status: QLabel | None = None
        self.preview_url: QLabel | None = None
        self.candidate_summary: QLabel | None = None
        self.settings_group: QGroupBox | None = None
        self.actions_row_widget: QWidget | None = None
        self.processing_group: QGroupBox | None = None
        self.step_label: QLabel | None = None
        self.progress_label: QLabel | None = None
        self.processing_total_label: QLabel | None = None
        self.processing_current_label: QLabel | None = None
        self.processing_step_label: QLabel | None = None
        self.processing_stats_label: QLabel | None = None
        self.processing_progress_bar: QProgressBar | None = None
        self.result_text: QTextEdit | None = None
        self.input_mode_combo: QComboBox | None = None
        self.parse_btn: QPushButton | None = None
        self.theme_combo: QComboBox | None = None
        self.theme_key = "classic_blue"
        self.advanced_content: QWidget | None = None
        self.candidate_naming_combo: QComboBox | None = None
        self.task_out_edit: QLineEdit | None = None
        self.task_keep_video_chk: QCheckBox | None = None
        self.task_save_thumbnail_chk: QCheckBox | None = None
        self.task_save_metadata_chk: QCheckBox | None = None
        self.task_export_audio_chk: QCheckBox | None = None
        self.task_download_subtitle_chk: QCheckBox | None = None
        self.task_download_pdf_chk: QCheckBox | None = None
        self.task_export_text_chk: QCheckBox | None = None
        self.task_strategy_hint: QLabel | None = None
        self.task_media_retention_combo: QComboBox | None = None
        self.task_audio_format_combo: QComboBox | None = None
        self.task_subtitle_strategy_combo: QComboBox | None = None
        self.task_subtitle_format_combo: QComboBox | None = None
        self.task_text_output_combo: QComboBox | None = None
        self.task_model_combo: QComboBox | None = None
        self.task_language_edit: QLineEdit | None = None
        self.task_quality_combo: QComboBox | None = None
        self.task_prefer_compatible_chk: QCheckBox | None = None
        self.task_allow_separate_chk: QCheckBox | None = None
        self.cookie_mode_combo: QComboBox | None = None
        self.cookies_file_edit: QLineEdit | None = None
        self.cookies_file_btn: QPushButton | None = None
        self.cookies_browser_combo: QComboBox | None = None

        self.out_edit = QLineEdit("outputs")
        self.config_edit = QLineEdit("config.json")
        self.model_combo = QComboBox()
        self.language_edit = QLineEdit("zh")
        self.candidate_mode_combo = QComboBox()
        self.quality_combo = QComboBox()
        self.always_try_chk = QCheckBox("始终尝试页面URL")
        self.prefer_compatible_chk = QCheckBox("优先兼容编码")
        self.allow_separate_chk = QCheckBox("允许分离流")
        self.js_runtime_edit = QLineEdit("deno,node")
        self.remote_component_edit = QLineEdit("ejs:github")
        self.archive_edit = QLineEdit("downloaded.txt")
        self.timeout_edit = QLineEdit("20")
        self.user_agent_edit = QLineEdit("")
        self.failed_log_edit = QLineEdit("")
        self.cookies_file_edit = QLineEdit("")
        self.cookies_file_btn = QPushButton("选择")
        self.cookies_browser_combo = QComboBox()
        self.force_seen_chk = QCheckBox("已下载项按强制重下处理")
        self.keep_video_chk = QCheckBox("下载视频")
        self.export_audio_chk = QCheckBox("导出处理后音频")
        self.audio_format_combo = QComboBox()
        self.subtitle_format_combo = QComboBox()
        self.text_output_combo = QComboBox()

        self.run_btn: QPushButton | None = None
        self.retry_btn: QPushButton | None = None
        self.stop_btn: QPushButton | None = None
        self.open_out_btn: QPushButton | None = None
        self.open_result_btn: QPushButton | None = None
        self.reveal_result_btn: QPushButton | None = None
        self.confirm_btn: QPushButton | None = None
        self.back_btn: QPushButton | None = None

        self._build_ui()
        self._bind_signals()
        self._load_cookie_defaults()
        self._update_cookie_controls()

    def _build_ui(self) -> None:
        container = QWidget(self)
        self.setCentralWidget(container)
        root = QHBoxLayout(container)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_nav(), stretch=0)

        self.stack = QStackedWidget()
        self.input_group = NewTaskPage(self)
        self.candidate_group = CandidateSelectionPage(self)
        self.processing_group = ProcessingPage(self)
        results_page = ResultsPage(self)
        self.settings_group = SettingsPage(self)

        self.stack.addWidget(self.input_group)
        self.stack.addWidget(self.candidate_group)
        self.stack.addWidget(self.processing_group)
        self.stack.addWidget(results_page)
        self.stack.addWidget(self.settings_group)
        root.addWidget(self.stack, stretch=1)

        self._load_gui_preferences()
        self._apply_styles()
        self._set_step(UiStep.INPUT)

    def _build_nav(self) -> QWidget:
        nav = QFrame()
        nav.setObjectName("SideNav")
        nav.setFixedWidth(180)
        layout = QVBoxLayout(nav)
        layout.setContentsMargins(12, 16, 12, 16)
        layout.setSpacing(8)

        title = QLabel("Aequora")
        title.setObjectName("NavTitle")
        layout.addWidget(title)

        self.step_label = QLabel("新建任务")
        self.step_label.setObjectName("StepLabel")
        layout.addWidget(self.step_label)

        def add_nav(label: str, key: UiStep | str, target_index: int) -> None:
            button = QPushButton(label)
            button.setObjectName("NavButton")
            button.clicked.connect(lambda _checked=False, idx=target_index, k=key: self._navigate(idx, k))
            self.nav_buttons[key] = button
            layout.addWidget(button)

        add_nav("新建任务", UiStep.INPUT, 0)
        add_nav("候选选择", UiStep.SELECTING, 1)
        add_nav("处理进度", UiStep.PROCESSING, 2)
        add_nav("结果输出", UiStep.DONE, 3)
        layout.addStretch(1)
        add_nav("设置", "settings", 4)
        return nav

    def _navigate(self, index: int, key: UiStep | str) -> None:
        if self.stack:
            self.stack.setCurrentIndex(index)
        if isinstance(key, UiStep):
            self.current_step = key
        self._refresh_nav(key)
        self._set_running_state(self._process_running)

    def _refresh_nav(self, active: UiStep | str) -> None:
        for key, button in self.nav_buttons.items():
            button.setProperty("active", key == active)
            button.style().unpolish(button)
            button.style().polish(button)

    def _gui_prefs_path(self) -> Path:
        return Path.cwd() / ".media2text_gui_prefs.json"

    def _set_combo_by_data(self, combo: QComboBox | None, value: str) -> bool:
        if not combo:
            return False
        for index in range(combo.count()):
            if combo.itemData(index) == value:
                combo.setCurrentIndex(index)
                return True
        return False

    def _load_gui_preferences(self) -> None:
        prefs_path = self._gui_prefs_path()
        prefs: dict[str, object] = {}
        if prefs_path.exists():
            try:
                loaded = json.loads(prefs_path.read_text(encoding="utf-8-sig"))
                if isinstance(loaded, dict):
                    prefs = loaded
            except Exception:
                prefs = {}

        theme = str(prefs.get("theme", "classic_blue"))
        if self.theme_combo:
            self.theme_combo.blockSignals(True)
            if not self._set_combo_by_data(self.theme_combo, theme):
                self.theme_combo.setCurrentIndex(0)
                theme = "classic_blue"
            self.theme_combo.blockSignals(False)
        self.theme_key = theme

        naming_mode = str(prefs.get("candidate_naming_mode", "page_title"))
        if self.candidate_naming_combo:
            self.candidate_naming_combo.blockSignals(True)
            if not self._set_combo_by_data(self.candidate_naming_combo, naming_mode):
                self.candidate_naming_combo.setCurrentIndex(0)
            self.candidate_naming_combo.blockSignals(False)

    def _save_gui_preferences(self) -> None:
        prefs = {
            "theme": self.theme_combo.currentData() if self.theme_combo else self.theme_key,
            "candidate_naming_mode": self.candidate_naming_combo.currentData() if self.candidate_naming_combo else "page_title",
        }
        try:
            self._gui_prefs_path().write_text(json.dumps(prefs, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            self._append_log(f"[WARN] GUI 偏好保存失败：{exc}")

    def _build_input_card(self) -> QWidget:
        card = QGroupBox("阶段 1：输入")
        layout = QVBoxLayout(card)

        top = QHBoxLayout()
        top.addWidget(QLabel("输入类型"))
        self.input_mode_combo = QComboBox()
        self.input_mode_combo.addItems(["自动识别", "链接", "文件/文件夹"])
        self.input_mode_combo.setCurrentIndex(0)
        top.addWidget(self.input_mode_combo)
        top.addStretch(1)
        self.parse_btn = QPushButton("开始解析")
        self.parse_btn.clicked.connect(self._run_processing)
        top.addWidget(self.parse_btn)
        layout.addLayout(top)

        self.input_edit = QTextEdit()
        self.input_edit.setPlaceholderText("粘贴本地路径或链接，每行一条。")
        self.input_edit.setFixedHeight(150)
        layout.addWidget(self.input_edit)

        row = QHBoxLayout()
        add_file_btn = QPushButton("添加文件")
        add_folder_btn = QPushButton("添加文件夹")
        add_url_btn = QPushButton("添加链接")
        clear_btn = QPushButton("清空")
        add_file_btn.clicked.connect(self._add_file)
        add_folder_btn.clicked.connect(self._add_folder)
        add_url_btn.clicked.connect(self._add_url)
        clear_btn.clicked.connect(self._clear_inputs)
        row.addWidget(add_file_btn)
        row.addWidget(add_folder_btn)
        row.addWidget(add_url_btn)
        row.addWidget(clear_btn)
        hint = QLabel("推荐用自动识别：可以混合粘贴链接和本地路径。")
        hint.setObjectName("HintLabel")
        row.addWidget(hint, 1)
        layout.addLayout(row)
        return card

    def _build_settings_card(self) -> QWidget:
        card = QGroupBox("设置")
        root = QVBoxLayout(card)

        tabs = QTabWidget()
        root.addWidget(tabs)

        appearance_tab = QWidget()
        appearance_layout = QVBoxLayout(appearance_tab)
        theme_row = QHBoxLayout()
        theme_row.addWidget(QLabel("界面主题"))
        self.theme_combo = QComboBox()
        self.theme_combo.addItem("经典雅蓝", "classic_blue")
        self.theme_combo.addItem("质感黑灰", "graphite")
        self.theme_combo.addItem("护眼暖阳", "warm_sun")
        self.theme_combo.currentIndexChanged.connect(self._on_theme_changed)
        theme_row.addWidget(self.theme_combo)
        theme_row.addStretch(1)
        appearance_layout.addLayout(theme_row)
        appearance_layout.addStretch(1)
        tabs.addTab(appearance_tab, "外观")

        output_tab = QWidget()
        output_layout = QVBoxLayout(output_tab)
        line1 = QHBoxLayout()
        line1.addWidget(QLabel("默认输出目录"), 0)
        line1.addWidget(self.out_edit, 1)
        out_btn = QPushButton("选择")
        out_btn.clicked.connect(self._choose_output)
        line1.addWidget(out_btn, 0)
        output_layout.addLayout(line1)

        line2 = QHBoxLayout()
        line2.addWidget(QLabel("配置文件"), 0)
        line2.addWidget(self.config_edit, 1)
        cfg_btn = QPushButton("选择")
        cfg_btn.clicked.connect(self._choose_config)
        line2.addWidget(cfg_btn, 0)
        output_layout.addLayout(line2)
        output_layout.addStretch(1)
        tabs.addTab(output_tab, "默认输出")

        defaults_tab = QWidget()
        p_layout = QGridLayout(defaults_tab)
        self.keep_video_chk.setChecked(True)
        self.export_audio_chk.setChecked(False)
        self.audio_format_combo.addItems(["mp3", "wav"])
        self.audio_format_combo.setCurrentText("mp3")
        self.subtitle_format_combo.addItems(["srt", "vtt", "ass"])
        self.subtitle_format_combo.setCurrentText("srt")
        self.text_output_combo.addItems(["txt+srt", "txt", "srt"])
        self.text_output_combo.setCurrentText("txt+srt")

        p_layout.addWidget(self.keep_video_chk, 0, 0)
        p_layout.addWidget(self.export_audio_chk, 0, 1)
        p_layout.addWidget(QLabel("音频格式"), 0, 2)
        p_layout.addWidget(self.audio_format_combo, 0, 3)
        p_layout.addWidget(QLabel("字幕格式"), 1, 0)
        p_layout.addWidget(self.subtitle_format_combo, 1, 1)
        p_layout.addWidget(QLabel("文本导出"), 1, 2)
        p_layout.addWidget(self.text_output_combo, 1, 3)

        self.model_combo.addItems(["small", "medium", "large-v3"])
        self.model_combo.setCurrentText("medium")
        self.quality_combo.addItems(["best", "1080p", "720p", "480p", "360p", "worst"])
        self.quality_combo.setCurrentText("best")
        self.prefer_compatible_chk.setChecked(True)
        self.force_seen_chk.setChecked(True)

        p_layout.addWidget(QLabel("Whisper模型"), 2, 0)
        p_layout.addWidget(self.model_combo, 2, 1)
        p_layout.addWidget(QLabel("语言"), 2, 2)
        p_layout.addWidget(self.language_edit, 2, 3)
        p_layout.addWidget(QLabel("画质"), 3, 0)
        p_layout.addWidget(self.quality_combo, 3, 1)
        p_layout.addWidget(self.prefer_compatible_chk, 3, 2)
        p_layout.addWidget(self.allow_separate_chk, 3, 3)
        p_layout.setRowStretch(4, 1)
        tabs.addTab(defaults_tab, "处理默认值")

        preset_tab = QWidget()
        preset_layout = QVBoxLayout(preset_tab)
        preset_hint = QLabel("预设系统占位：后续可保存“只下载视频 / 只要字幕 / 学习笔记整理”等处理组合。")
        preset_hint.setObjectName("HintLabel")
        preset_hint.setWordWrap(True)
        preset_layout.addWidget(preset_hint)
        preset_layout.addStretch(1)
        tabs.addTab(preset_tab, "预设")

        advanced_tab = QWidget()
        adv_layout = QGridLayout(advanced_tab)
        self.candidate_mode_combo.addItems(["select", "auto"])
        self.candidate_mode_combo.setCurrentText("select")
        self.candidate_naming_combo = QComboBox()
        self.candidate_naming_combo.addItem("页面标题优先", "page_title")
        self.candidate_naming_combo.addItem("域名 + 序号", "domain_index")
        self.candidate_naming_combo.addItem("统一编号", "sequence")
        self.candidate_naming_combo.currentIndexChanged.connect(lambda _index: self._save_gui_preferences())
        adv_layout.addWidget(QLabel("候选模式"), 0, 0)
        adv_layout.addWidget(self.candidate_mode_combo, 0, 1)
        adv_layout.addWidget(self.always_try_chk, 0, 2)
        adv_layout.addWidget(QLabel("候选命名模式"), 1, 0)
        adv_layout.addWidget(self.candidate_naming_combo, 1, 1, 1, 2)
        adv_layout.addWidget(QLabel("JS Runtime"), 2, 0)
        adv_layout.addWidget(self.js_runtime_edit, 2, 1, 1, 2)
        adv_layout.addWidget(QLabel("Remote"), 3, 0)
        adv_layout.addWidget(self.remote_component_edit, 3, 1, 1, 2)
        adv_layout.addWidget(QLabel("Archive"), 4, 0)
        adv_layout.addWidget(self.archive_edit, 4, 1, 1, 2)
        adv_layout.addWidget(QLabel("Timeout"), 5, 0)
        adv_layout.addWidget(self.timeout_edit, 5, 1)
        adv_layout.addWidget(QLabel("失败日志"), 6, 0)
        adv_layout.addWidget(self.failed_log_edit, 6, 1, 1, 2)
        failed_btn = QPushButton("选择")
        failed_btn.clicked.connect(self._choose_failed_log)
        adv_layout.addWidget(failed_btn, 6, 3)
        adv_layout.addWidget(QLabel("User-Agent"), 7, 0)
        adv_layout.addWidget(self.user_agent_edit, 7, 1, 1, 3)
        cookie_box = QGroupBox("账号验证 / Cookie")
        cookie_layout = QGridLayout(cookie_box)
        self.cookie_mode_combo = QComboBox()
        self.cookie_mode_combo.addItem("不使用 Cookie", "none")
        self.cookie_mode_combo.addItem("导入 cookies.txt", "cookies_file")
        self.cookie_mode_combo.addItem("从浏览器读取", "browser")
        if self.cookies_browser_combo:
            self.cookies_browser_combo.addItem("Chrome", "chrome")
            self.cookies_browser_combo.addItem("Edge", "edge")
            self.cookies_browser_combo.addItem("Firefox", "firefox")
        if self.cookies_file_btn:
            self.cookies_file_btn.clicked.connect(self._choose_cookies_file)
        self.cookie_mode_combo.currentIndexChanged.connect(self._update_cookie_controls)
        cookie_layout.addWidget(QLabel("Cookie 使用方式"), 0, 0)
        cookie_layout.addWidget(self.cookie_mode_combo, 0, 1, 1, 2)
        cookie_layout.addWidget(QLabel("Cookie 文件"), 1, 0)
        cookie_layout.addWidget(self.cookies_file_edit, 1, 1)
        cookie_layout.addWidget(self.cookies_file_btn, 1, 2)
        cookie_layout.addWidget(QLabel("浏览器"), 2, 0)
        cookie_layout.addWidget(self.cookies_browser_combo, 2, 1, 1, 2)
        cookie_hint = QLabel("用于处理需要登录、权限验证或防盗链的视频资源。Cookie 只保存在本地配置中。")
        cookie_hint.setWordWrap(True)
        cookie_hint.setObjectName("HintLabel")
        cookie_layout.addWidget(cookie_hint, 3, 0, 1, 3)
        adv_layout.addWidget(cookie_box, 8, 0, 1, 4)
        adv_layout.setRowStretch(9, 1)
        tabs.addTab(advanced_tab, "组件 / 高级")

        return card

    def _build_actions_row(self) -> QWidget:
        row_widget = QGroupBox("候选操作")
        row = QHBoxLayout(row_widget)
        row.setContentsMargins(12, 10, 12, 10)
        row.setSpacing(10)

        self.run_btn = QPushButton("返回输入")
        self.retry_btn = QPushButton("失败重跑")

        self.run_btn.clicked.connect(self._back_from_candidates)
        self.retry_btn.clicked.connect(self._run_retry)

        hint = QLabel("勾选候选后点击开始处理。")
        hint.setObjectName("HintLabel")
        row.addWidget(hint)
        row.addStretch(1)
        row.addWidget(self.run_btn)
        row.addWidget(self.retry_btn)
        return row_widget

    def _build_candidate_group(self) -> QGroupBox:
        group = QGroupBox("阶段 2：候选选择")
        layout = QVBoxLayout(group)
        layout.setSpacing(12)
        top = QHBoxLayout()
        top.addWidget(QLabel("点击任意行可切换选择，右侧显示当前候选详情。"))
        self.candidate_summary = QLabel("共 0 条，已选择 0 条")
        self.candidate_summary.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        top.addWidget(self.candidate_summary, 1)
        layout.addLayout(top)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_candidate_left())
        splitter.addWidget(self._build_candidate_right())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([780, 420])
        layout.addWidget(splitter)
        return group

    def _build_candidate_left(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setSpacing(10)

        self.candidate_model = CandidateTableModel(self)
        self.candidate_table = QTableView()
        self.candidate_table.setModel(self.candidate_model)
        self.candidate_table.setItemDelegateForColumn(0, CandidateCheckDelegate(self))
        self.candidate_table.setMinimumHeight(360)
        self.candidate_table.verticalHeader().setVisible(False)
        self.candidate_table.setAlternatingRowColors(True)
        header = self.candidate_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.candidate_table.setSelectionBehavior(QTableView.SelectRows)
        self.candidate_table.setSelectionMode(QTableView.SingleSelection)
        self.candidate_table.setEditTriggers(QTableView.NoEditTriggers)
        self.candidate_table.setMouseTracking(True)
        layout.addWidget(self.candidate_table)

        control = QHBoxLayout()
        control.setSpacing(10)
        pick_new_btn = QPushButton("全选未下载")
        pick_all_btn = QPushButton("全选")
        pick_clear_btn = QPushButton("清空")
        pick_invert_btn = QPushButton("反选")
        pick_new_btn.clicked.connect(self._candidate_select_unseen)
        pick_all_btn.clicked.connect(self._candidate_select_all)
        pick_clear_btn.clicked.connect(self._candidate_clear)
        pick_invert_btn.clicked.connect(self._candidate_invert)
        control.addWidget(pick_new_btn)
        control.addWidget(pick_all_btn)
        control.addWidget(pick_clear_btn)
        control.addWidget(pick_invert_btn)
        control.addStretch(1)
        layout.addLayout(control)

        meta_row = QHBoxLayout()
        meta_row.setSpacing(10)
        meta_row.addWidget(self.force_seen_chk)
        meta_row.addStretch(1)
        actions = QHBoxLayout()
        actions.setSpacing(10)
        self.back_btn = QPushButton("返回编辑")
        self.confirm_btn = QPushButton("下一步：开始处理")
        self.back_btn.clicked.connect(self._back_from_candidates)
        self.confirm_btn.clicked.connect(self._confirm_candidates)
        actions.addWidget(self.back_btn)
        actions.addWidget(self.confirm_btn)
        meta_row.addLayout(actions)
        layout.addLayout(meta_row)
        return panel

    def _build_candidate_right(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        panel = QWidget()
        panel.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        layout = QVBoxLayout(panel)
        layout.setSpacing(12)
        layout.addWidget(self._build_candidate_preview())
        layout.addWidget(self._build_task_strategy_panel())
        layout.addStretch(1)

        scroll.setWidget(panel)
        return scroll

    def _build_candidate_preview(self) -> QWidget:
        panel = QGroupBox("候选预览")
        layout = QVBoxLayout(panel)
        layout.setSpacing(8)
        title = QLabel("当前选中项")
        title.setObjectName("PreviewTitle")
        layout.addWidget(title)

        self.preview_image = QLabel("暂无封面")
        self.preview_image.setAlignment(Qt.AlignCenter)
        self.preview_image.setMinimumSize(360, 200)
        self.preview_image.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.preview_image.setObjectName("PreviewImage")
        layout.addWidget(self.preview_image)

        self.preview_title = QLabel("未选择候选视频")
        self.preview_title.setWordWrap(True)
        self.preview_status = QLabel("")
        self.preview_status.setWordWrap(True)
        self.preview_url = QLabel("")
        self.preview_url.setWordWrap(True)
        self.preview_url.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(QLabel("标题："))
        layout.addWidget(self.preview_title)
        layout.addWidget(QLabel("状态："))
        layout.addWidget(self.preview_status)
        layout.addWidget(QLabel("链接："))
        layout.addWidget(self.preview_url)
        layout.addStretch(1)
        return panel

    def _help_button(self, tooltip: str) -> QPushButton:
        button = QPushButton("i")
        button.setObjectName("HelpButton")
        button.setToolTip(tooltip)
        button.setFixedSize(14, 14)
        button.setFocusPolicy(Qt.NoFocus)
        return button

    def _with_help(self, widget: QWidget, tooltip: str) -> QWidget:
        widget.setToolTip(tooltip)
        wrapper = QWidget()
        row = QHBoxLayout(wrapper)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        row.addWidget(widget)
        row.addWidget(self._help_button(tooltip))
        row.addStretch(1)
        return wrapper

    def _label_with_help(self, text: str, tooltip: str) -> QWidget:
        label = QLabel(text)
        label.setToolTip(tooltip)
        wrapper = QWidget()
        row = QHBoxLayout(wrapper)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        row.addWidget(label)
        row.addWidget(self._help_button(tooltip))
        row.addStretch(1)
        return wrapper

    def _build_task_strategy_panel(self) -> QGroupBox:
        panel = QGroupBox("本次处理策略")
        root = QVBoxLayout(panel)
        root.setSpacing(12)

        self.task_out_edit = QLineEdit()
        self.task_out_edit.setReadOnly(True)
        self.task_out_edit.setPlaceholderText("请选择本次任务输出目录")
        out_btn = QPushButton("选择")
        out_btn.clicked.connect(self._choose_task_output)
        out_row = QHBoxLayout()
        out_row.addWidget(QLabel("本次输出目录"))
        out_row.addWidget(self.task_out_edit, 1)
        out_row.addWidget(out_btn)
        root.addLayout(out_row)

        core_group = QGroupBox("我要什么")
        core_layout = QGridLayout(core_group)
        core_layout.setHorizontalSpacing(16)
        core_layout.setVerticalSpacing(8)

        self.task_keep_video_chk = QCheckBox("保留视频")
        self.task_save_thumbnail_chk = QCheckBox("保存封面")
        self.task_save_metadata_chk = QCheckBox("保存来源信息（标题、链接、时长等）")
        self.task_save_metadata_chk.setToolTip("保存这条资料的标题、原始链接、平台信息和处理记录，方便以后追溯来源。")
        self.task_export_audio_chk = QCheckBox("导出音频")
        self.task_download_subtitle_chk = QCheckBox("下载字幕")
        self.task_download_pdf_chk = QCheckBox("下载PDF")
        self.task_export_text_chk = QCheckBox("导出文本")
        self.task_download_subtitle_chk.setChecked(True)
        self.task_download_pdf_chk.setChecked(True)
        self.task_export_text_chk.setChecked(True)
        for checkbox in (
            self.task_keep_video_chk,
            self.task_download_subtitle_chk,
            self.task_download_pdf_chk,
            self.task_export_audio_chk,
            self.task_export_text_chk,
        ):
            checkbox.setObjectName("CoreOption")
            checkbox.stateChanged.connect(lambda _state: self._update_task_strategy_hint())
        core_layout.addWidget(self.task_keep_video_chk, 0, 0)
        core_layout.addWidget(self.task_download_subtitle_chk, 0, 1)
        core_layout.addWidget(self.task_download_pdf_chk, 1, 0)
        core_layout.addWidget(self.task_export_text_chk, 1, 1)
        core_layout.addWidget(self.task_export_audio_chk, 2, 0)
        root.addWidget(core_group)

        self.task_media_retention_combo = QComboBox()
        self.task_media_retention_combo.addItem("仅保留最终结果，不保留中间媒体", "final_only")
        self.task_media_retention_combo.addItem("使用临时缓存，处理后删除中间文件", "temporary_cache")
        self.task_media_retention_combo.addItem("完整保留原视频与结果文件", "keep_all")
        self.task_audio_format_combo = QComboBox()
        self.task_audio_format_combo.addItems(["mp3", "wav"])
        self.task_subtitle_strategy_combo = QComboBox()
        self.task_subtitle_strategy_combo.addItem("只下载平台字幕", "platform_only")
        self.task_subtitle_strategy_combo.addItem("平台字幕优先，没有则 Whisper", "platform_then_whisper")
        self.task_subtitle_strategy_combo.addItem("强制 Whisper 转写", "whisper_only")
        self.task_subtitle_strategy_combo.addItem("跳过字幕", "skip_text")
        self.task_subtitle_format_combo = QComboBox()
        self.task_subtitle_format_combo.addItems(["srt", "vtt", "ass"])
        self.task_text_output_combo = QComboBox()
        self.task_text_output_combo.addItems(["txt+srt", "txt", "srt", "txt+md+srt", "md"])

        strategy_group = QGroupBox("处理方式")
        strategy_layout = QGridLayout(strategy_group)
        strategy_layout.setHorizontalSpacing(10)
        strategy_hint = QLabel("系统将自动选择最优处理方式；需要精细控制时再展开高级设置。")
        strategy_hint.setObjectName("HintLabel")
        strategy_hint.setWordWrap(True)
        strategy_layout.addWidget(strategy_hint, 0, 0, 1, 4)
        strategy_layout.addWidget(self._label_with_help("字幕策略", "决定字幕来源：只用平台现成字幕、没有字幕时转写、强制转写，或跳过字幕。"), 1, 0)
        strategy_layout.addWidget(self.task_subtitle_strategy_combo, 1, 1, 1, 3)
        self.task_subtitle_strategy_combo.currentIndexChanged.connect(lambda _index: self._update_task_strategy_hint())
        self.task_text_output_combo.currentIndexChanged.connect(lambda _index: self._update_task_strategy_hint())
        root.addWidget(strategy_group)

        self.task_strategy_hint = QLabel("")
        self.task_strategy_hint.setObjectName("HintLabel")
        self.task_strategy_hint.setWordWrap(True)
        root.addWidget(self.task_strategy_hint)

        self.task_model_combo = QComboBox()
        self.task_model_combo.addItems(["small", "medium", "large-v3"])
        self.task_language_edit = QLineEdit()
        self.task_quality_combo = QComboBox()
        self.task_quality_combo.addItems(["best", "1080p", "720p", "480p", "360p", "worst"])

        self.task_prefer_compatible_chk = QCheckBox("优先兼容编码")
        self.task_allow_separate_chk = QCheckBox("允许分离流")
        self.task_media_retention_combo.currentIndexChanged.connect(lambda _index: self._update_task_strategy_hint())
        self.task_keep_video_chk.stateChanged.connect(lambda _state: self._sync_media_retention_from_keep_video())

        advanced_button = QToolButton()
        advanced_button.setText("高级设置")
        advanced_button.setCheckable(True)
        advanced_button.setChecked(False)
        advanced_button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        advanced_button.setArrowType(Qt.RightArrow)

        advanced_panel = QWidget()
        advanced_panel.setVisible(False)
        advanced_panel.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        advanced_layout = QVBoxLayout(advanced_panel)
        advanced_layout.setContentsMargins(0, 0, 0, 0)
        advanced_layout.setSpacing(8)

        media_group = QGroupBox("媒体控制")
        media_layout = QGridLayout(media_group)
        media_layout.addWidget(self.task_save_thumbnail_chk, 0, 0)
        media_layout.addWidget(self.task_save_metadata_chk, 0, 1)
        media_layout.addWidget(self._label_with_help("媒体保留策略", "决定处理中间文件怎么处理：只留最终结果、临时缓存后删除，或完整保留视频和结果。"), 1, 0)
        media_layout.addWidget(self.task_media_retention_combo, 1, 1)
        advanced_layout.addWidget(media_group)

        format_group = QGroupBox("格式")
        format_layout = QGridLayout(format_group)
        format_layout.addWidget(QLabel("音频格式"), 0, 0)
        format_layout.addWidget(self.task_audio_format_combo, 0, 1)
        format_layout.addWidget(QLabel("字幕格式"), 1, 0)
        format_layout.addWidget(self.task_subtitle_format_combo, 1, 1)
        format_layout.addWidget(QLabel("文本导出格式"), 2, 0)
        format_layout.addWidget(self.task_text_output_combo, 2, 1)
        advanced_layout.addWidget(format_group)

        ai_group = QGroupBox("AI / 转写")
        ai_layout = QGridLayout(ai_group)
        ai_layout.addWidget(self._label_with_help("Whisper 模型", "用于语音转文字的模型；越大通常越准，但需要更长时间和更多电脑资源。"), 0, 0)
        ai_layout.addWidget(self.task_model_combo, 0, 1)
        ai_layout.addWidget(QLabel("转写语言"), 1, 0)
        ai_layout.addWidget(self.task_language_edit, 1, 1)
        advanced_layout.addWidget(ai_group)

        download_group = QGroupBox("下载参数")
        download_layout = QGridLayout(download_group)
        download_layout.addWidget(QLabel("下载画质"), 0, 0)
        download_layout.addWidget(self.task_quality_combo, 0, 1)
        download_layout.addWidget(self.task_prefer_compatible_chk, 1, 0)
        download_layout.addWidget(self.task_allow_separate_chk, 1, 1)
        advanced_layout.addWidget(download_group)

        def toggle_advanced(checked: bool) -> None:
            advanced_panel.setVisible(checked)
            advanced_button.setArrowType(Qt.DownArrow if checked else Qt.RightArrow)

        advanced_button.toggled.connect(toggle_advanced)
        root.addWidget(advanced_button, alignment=Qt.AlignLeft)
        root.addWidget(advanced_panel)
        self._update_task_strategy_hint()

        return panel

    def _build_processing_group(self) -> QGroupBox:
        card = QGroupBox("阶段 3：处理与输出")
        layout = QVBoxLayout(card)
        layout.setSpacing(12)

        overview = QGroupBox("总体进度")
        overview_layout = QGridLayout(overview)
        self.progress_label = QLabel("等待开始处理")
        self.progress_label.setObjectName("ProgressLabel")
        self.progress_label.setWordWrap(True)
        self.processing_total_label = QLabel("总任务数：0")
        self.processing_current_label = QLabel("当前项：0/0")
        self.processing_progress_bar = QProgressBar()
        self.processing_progress_bar.setRange(0, 100)
        self.processing_progress_bar.setValue(0)
        overview_layout.addWidget(self.progress_label, 0, 0, 1, 2)
        overview_layout.addWidget(self.processing_total_label, 1, 0)
        overview_layout.addWidget(self.processing_current_label, 1, 1)
        overview_layout.addWidget(self.processing_progress_bar, 2, 0, 1, 2)
        layout.addWidget(overview)

        step_group = QGroupBox("当前步骤")
        step_layout = QVBoxLayout(step_group)
        self.processing_step_label = QLabel("尚未开始")
        self.processing_step_label.setObjectName("ProgressLabel")
        self.processing_step_label.setWordWrap(True)
        step_layout.addWidget(self.processing_step_label)
        layout.addWidget(step_group)

        stats_group = QGroupBox("结果统计")
        stats_layout = QVBoxLayout(stats_group)
        self.processing_stats_label = QLabel("成功 0 | 失败 0 | 跳过 0 | 剩余 0")
        self.processing_stats_label.setWordWrap(True)
        stats_layout.addWidget(self.processing_stats_label)
        layout.addWidget(stats_group)

        log_group = QGroupBox("运行日志")
        log_layout = QVBoxLayout(log_group)
        self.log_edit = QPlainTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setMaximumHeight(180)
        self.log_edit.setPlaceholderText("执行详情会实时显示在这里。")
        log_layout.addWidget(self.log_edit)
        layout.addWidget(log_group)

        row = QHBoxLayout()
        self.stop_btn = QPushButton("停止")
        self.open_result_btn = QPushButton("打开本次结果文件")
        self.reveal_result_btn = QPushButton("打开本次结果所在位置")
        self.open_out_btn = QPushButton("打开输出目录")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._stop_processing)
        self.open_result_btn.clicked.connect(self._open_primary_result_file)
        self.reveal_result_btn.clicked.connect(self._reveal_primary_result_file)
        self.open_out_btn.clicked.connect(self._open_output_dir)
        row.addStretch(1)
        row.addWidget(self.stop_btn)
        row.addWidget(self.open_result_btn)
        row.addWidget(self.reveal_result_btn)
        row.addWidget(self.open_out_btn)
        layout.addLayout(row)
        return card

    def _build_log_card(self) -> QWidget:
        card = QGroupBox("运行日志（已折叠为辅助信息）")
        layout = QVBoxLayout(card)
        self.log_edit = QPlainTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setMaximumHeight(92)
        layout.addWidget(self.log_edit)
        return card

    def _apply_styles(self) -> None:
        themes = {
            "classic_blue": {
                "window": "#0f1726",
                "nav": "#0b1220",
                "nav_border": "#26344c",
                "panel": "#121d2f",
                "field": "#0f1a2b",
                "border": "#30405a",
                "soft_border": "#2f3c56",
                "text": "#e8eef6",
                "muted": "#89a3c0",
                "title": "#f0f6ff",
                "active": "#1d3656",
                "button": "#22344d",
                "button_hover": "#284164",
                "disabled": "#1a2739",
                "header": "#18253a",
                "alternate": "#132034",
            },
            "graphite": {
                "window": "#101010",
                "nav": "#151515",
                "nav_border": "#2b2b2b",
                "panel": "#1b1b1b",
                "field": "#121212",
                "border": "#3a3a3a",
                "soft_border": "#303030",
                "text": "#ededed",
                "muted": "#a8a8a8",
                "title": "#f7f7f7",
                "active": "#343434",
                "button": "#2a2a2a",
                "button_hover": "#383838",
                "disabled": "#202020",
                "header": "#242424",
                "alternate": "#181818",
            },
            "warm_sun": {
                "window": "#f4efe3",
                "nav": "#eee4d2",
                "nav_border": "#d6c7ad",
                "panel": "#fffaf0",
                "field": "#fbf3e4",
                "border": "#cbb997",
                "soft_border": "#d8c7a9",
                "text": "#2c261d",
                "muted": "#7a674b",
                "title": "#241f18",
                "active": "#d9bd84",
                "button": "#ead6ad",
                "button_hover": "#dfc58f",
                "disabled": "#e3d8c7",
                "header": "#ead8b5",
                "alternate": "#f6ead5",
            },
        }
        theme = themes.get(self.theme_key, themes["classic_blue"])
        self.setStyleSheet(
            f"""
            QMainWindow {{ background: {theme["window"]}; color: {theme["text"]}; }}
            QFrame#SideNav {{
                background: {theme["nav"]};
                border-right: 1px solid {theme["nav_border"]};
            }}
            QLabel#NavTitle {{
                font-size: 20px;
                font-weight: 700;
                color: {theme["title"]};
                padding: 4px 2px 10px 2px;
            }}
            QLabel#StepLabel {{
                color: {theme["muted"]};
                padding: 0 2px 14px 2px;
            }}
            QPushButton#NavButton {{
                text-align: left;
                padding: 9px 12px;
                border-radius: 6px;
                background: transparent;
                border: 1px solid transparent;
            }}
            QPushButton#NavButton[active="true"] {{
                background: {theme["active"]};
                border-color: {theme["border"]};
                color: {theme["title"]};
            }}
            QLabel#PageTitle {{
                font-size: 18px;
                font-weight: 700;
            }}
            QGroupBox {{
                border: 1px solid {theme["soft_border"]};
                border-radius: 10px;
                margin-top: 8px;
                padding: 12px;
                background: {theme["panel"]};
            }}
            QGroupBox::title {{
                color: {theme["muted"]};
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 4px;
            }}
            QLabel {{ color: {theme["text"]}; }}
            QLabel#TitleLabel {{ font-size: 24px; font-weight: 700; }}
            QLabel#HintLabel {{ color: {theme["muted"]}; }}
            QLabel#PreviewTitle {{ font-size: 16px; font-weight: 600; }}
            QLabel#PreviewImage {{
                border: 1px dashed {theme["border"]};
                border-radius: 8px;
                background: {theme["field"]};
                color: {theme["muted"]};
            }}
            QTableView {{
                gridline-color: {theme["soft_border"]};
                alternate-background-color: {theme["alternate"]};
                selection-background-color: {theme["active"]};
            }}
            QTableView::item {{
                padding: 8px;
            }}
            QTextEdit, QPlainTextEdit, QLineEdit, QComboBox, QTableView {{
                background: {theme["field"]};
                color: {theme["text"]};
                border: 1px solid {theme["border"]};
                border-radius: 8px;
                padding: 6px;
            }}
            QPushButton {{
                background: {theme["button"]};
                color: {theme["text"]};
                border: 1px solid {theme["border"]};
                border-radius: 8px;
                padding: 6px 14px;
            }}
            QPushButton#HelpButton {{
                min-width: 14px;
                max-width: 14px;
                min-height: 14px;
                max-height: 14px;
                border-radius: 7px;
                padding: 0;
                color: {theme["muted"]};
                background: transparent;
                border-color: {theme["soft_border"]};
                font-weight: 700;
            }}
            QCheckBox#CoreOption {{
                font-size: 14px;
                font-weight: 600;
                spacing: 8px;
                padding: 4px 2px;
            }}
            QPushButton:hover {{ background: {theme["button_hover"]}; }}
            QPushButton:disabled {{ background: {theme["disabled"]}; color: {theme["muted"]}; }}
            QHeaderView::section {{
                background: {theme["header"]};
                color: {theme["muted"]};
                border: 0;
                padding: 6px;
            }}
            """
        )

    def _bind_signals(self) -> None:
        self.candidate_scan_finished.connect(self._on_candidate_scan_finished)
        self.candidate_meta_ready.connect(self._apply_candidate_meta)
        if self.candidate_table is not None:
            self.candidate_table.selectionModel().selectionChanged.connect(self._on_candidate_selected)
            self.candidate_table.clicked.connect(self._on_candidate_clicked)

        copy_action = QAction("复制日志", self)
        copy_action.triggered.connect(self._copy_logs)
        self.addAction(copy_action)

    def _copy_logs(self) -> None:
        if not self.log_edit:
            return
        QApplication.clipboard().setText(self.log_edit.toPlainText())

    def _append_log(self, text: str) -> None:
        if not self.log_edit:
            return
        self.log_edit.appendPlainText(text)
        bar = self.log_edit.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _format_process_log_line(self, line: str) -> str | None:
        stripped = line.strip()
        if not stripped:
            return None
        normalized = stripped.lower()
        if self._parse_download_progress(stripped) is not None:
            return None
        if "[task" in normalized:
            return f"开始处理 {self._current_task_index_text()}：{self._current_task_name or self._short_display(stripped)}"
        if "downloading media" in normalized:
            return "开始下载媒体"
        if "media downloaded" in normalized:
            return "✔ 下载完成"
        if "exporting audio" in normalized:
            return "开始导出音频"
        if "audio export success" in normalized:
            return "✔ 音频导出成功"
        if "deleting temporary" in normalized:
            return "删除临时文件"
        if "removed temporary" in normalized:
            return "✔ 临时文件已删除"
        if "[done]" in normalized:
            return "✔ 任务完成"
        if "[skipped]" in normalized:
            return "跳过当前任务"
        if "[failed]" in normalized or "[error]" in normalized or "| error |" in normalized:
            return f"✖ {stripped}"
        if normalized.startswith("[download]"):
            return None
        if "ffmpeg" in normalized and "[error]" not in normalized and "failed" not in normalized:
            return None
        return stripped

    def _reset_processing_progress(self, total: int, output_root: Path) -> None:
        self._progress_total = max(0, total)
        self._progress_completed = 0
        self._progress_success = 0
        self._progress_failed = 0
        self._progress_skipped = 0
        self._progress_current_index = 0
        self._current_task_total = max(0, total)
        self._current_task_name = ""
        self._current_step_name = "正在启动处理进程"
        if self.log_edit:
            self.log_edit.clear()
        if self.progress_label:
            self.progress_label.setText(f"正在准备处理 {total} 个输入项。\n输出目录：{output_root}")
        self._set_processing_stage("正在启动处理进程")
        self._refresh_processing_progress()

    def _set_processing_stage(self, text: str) -> None:
        self._current_step_name = text
        if self.processing_step_label:
            self.processing_step_label.setText(self._compose_processing_stage_text(text))

    def _current_task_index_text(self) -> str:
        total = self._current_task_total or self._progress_total
        current = self._progress_current_index
        return f"任务 {current}/{total}" if current and total else "任务 0/0"

    def _compose_processing_stage_text(self, step_text: str) -> str:
        lines: list[str] = []
        if self._progress_current_index and (self._current_task_total or self._progress_total):
            name = self._current_task_name or "当前输入项"
            lines.append(f"{self._current_task_index_text()}：{name}")
        lines.append(f"步骤：{step_text}")
        return "\n".join(lines)

    def _set_preparation_progress(self, index: int, total: int, message: str) -> None:
        total = max(total, 0)
        index = max(0, min(index, total)) if total else 0
        if self.progress_label:
            self.progress_label.setText(f"准备阶段：{message}")
        if self.processing_total_label:
            self.processing_total_label.setText(f"总任务数：{total}")
        if self.processing_current_label:
            self.processing_current_label.setText(f"准备任务：{index}/{total}" if total else "准备任务：0/0")
        if self.processing_progress_bar:
            self.processing_progress_bar.setValue(int((index / total) * 100) if total else 0)
        self._set_processing_stage(message)

    def _short_display(self, value: str, max_len: int = 88) -> str:
        text = value.strip()
        if len(text) <= max_len:
            return text
        return text[: max_len - 1].rstrip() + "…"

    def _task_display_name(self, value: str) -> str:
        text = value.strip()
        if not text:
            return "当前输入项"
        if is_url(text):
            parsed = urlparse(text)
            tail = unquote(parsed.path.strip("/").split("/")[-1]) if parsed.path else ""
            if "." in tail:
                tail = tail.rsplit(".", 1)[0]
            tail = re.sub(r"[-_]+", " ", tail).strip()
            if tail and not self._is_invalid_candidate_title(tail):
                return self._short_display(tail, max_len=56)
            host = parsed.netloc.lower().replace("www.", "").strip()
            return self._short_display(host or text, max_len=56)
        return self._short_display(Path(text).stem or text, max_len=56)

    def _parse_download_progress(self, line: str) -> str | None:
        normalized = line.strip()
        if not normalized.lower().startswith("[download]"):
            return None
        if "%" not in normalized:
            return None
        percent_match = re.search(r"(\d+(?:\.\d+)?)%", normalized)
        if not percent_match:
            return None
        percent = f"{int(float(percent_match.group(1)))}%"
        speed = ""
        speed_match = re.search(r"\bat\s+([^\s]+/s)", normalized, re.IGNORECASE)
        if speed_match:
            speed = speed_match.group(1)
        eta = ""
        eta_match = re.search(r"\bETA\s+([0-9:]+)", normalized, re.IGNORECASE)
        if eta_match:
            eta = eta_match.group(1)
        parts = [percent]
        if speed:
            parts.append(speed)
        if eta:
            parts.append(f"剩余{eta}")
        return "｜".join(parts)

    def _refresh_processing_progress(self) -> None:
        total = max(0, self._progress_total)
        completed = min(self._progress_completed, total) if total else self._progress_completed
        remaining = max(total - completed, 0)
        percent = int((completed / total) * 100) if total else 0
        if self.processing_total_label:
            self.processing_total_label.setText(f"总任务数：{total}")
        if self.processing_current_label:
            current = self._progress_current_index if self._progress_current_index else min(completed + 1, total)
            self.processing_current_label.setText(f"当前项：{current}/{total}" if total else "当前项：0/0")
        if self.processing_progress_bar:
            self.processing_progress_bar.setValue(percent)
        if self.processing_stats_label:
            self.processing_stats_label.setText(
                f"成功 {self._progress_success} | 失败 {self._progress_failed} | "
                f"跳过 {self._progress_skipped} | 剩余 {remaining}"
            )

    def _mark_processing_result(self, status: str) -> None:
        if self._progress_total and self._progress_completed >= self._progress_total:
            return
        if status == "success":
            self._progress_success += 1
        elif status == "failed":
            self._progress_failed += 1
        elif status == "skipped":
            self._progress_skipped += 1
        self._progress_completed = self._progress_success + self._progress_failed + self._progress_skipped
        self._refresh_processing_progress()

    def _handle_process_line(self, line: str) -> bool:
        download_progress = self._parse_download_progress(line)
        if download_progress is not None:
            self._set_processing_stage(f"正在下载媒体（{download_progress}）")
            return True

        task_match = re.search(r"\[TASK\s+(\d+)/(\d+)\]\s+(.*)", line)
        if task_match:
            self._progress_current_index = int(task_match.group(1))
            self._progress_total = max(self._progress_total, int(task_match.group(2)))
            self._current_task_total = int(task_match.group(2))
            self._current_task_name = self._task_display_name(task_match.group(3))
            self._set_processing_stage("准备处理当前任务")
            self._refresh_processing_progress()
            return False

        expanded_match = re.search(r"Expanded to\s+(\d+)\s+task", line)
        if expanded_match:
            self._progress_total = int(expanded_match.group(1))
            self._current_task_total = self._progress_total
            self._refresh_processing_progress()
            return False

        summary_match = re.search(r"Done\. total=(\d+) success=(\d+) failed=(\d+)", line)
        if summary_match:
            self._progress_total = int(summary_match.group(1))
            self._progress_success = int(summary_match.group(2))
            self._progress_failed = int(summary_match.group(3))
            self._progress_completed = self._progress_success + self._progress_failed + self._progress_skipped
            self._set_processing_stage("处理完成，正在整理结果")
            self._refresh_processing_progress()
            return False

        normalized = line.lower()
        if "[done]" in normalized:
            self._mark_processing_result("success")
            self._set_processing_stage("当前项处理成功")
        elif "[failed]" in normalized or "| error |" in normalized or "[error]" in normalized:
            self._mark_processing_result("failed")
            self._set_processing_stage("当前项处理失败，请查看日志原因")
        elif "[skipped]" in normalized:
            self._mark_processing_result("skipped")
            self._set_processing_stage("当前项已跳过")
        elif "media downloaded" in normalized:
            self._set_processing_stage("下载完成")
        elif "downloading media" in normalized:
            self._set_processing_stage("正在下载媒体")
        elif "检测到直接媒体链接" in line or "direct media" in normalized:
            self._set_processing_stage("正在跳过网页扫描")
        elif "platform subtitle" in normalized or "subtitle" in normalized:
            self._set_processing_stage("正在获取或写出字幕")
        elif "audio export success" in normalized:
            self._set_processing_stage("音频导出成功")
        elif "extracting audio" in normalized or "audio export" in normalized:
            self._set_processing_stage("正在提取音频")
        elif "whisper" in normalized or "transcrib" in normalized:
            self._set_processing_stage("正在 Whisper 转写")
        elif "writing output" in normalized or "artifact" in normalized:
            self._set_processing_stage("正在写出文件")
        elif "deleting temporary" in normalized or "removed temporary" in normalized:
            self._set_processing_stage("正在删除临时文件")
        return False

    def _set_step(self, step: UiStep) -> None:
        self.current_step = step
        index_by_step = {
            UiStep.INPUT: 0,
            UiStep.SELECTING: 1,
            UiStep.PROCESSING: 2,
            UiStep.DONE: 3,
        }
        if self.stack:
            self.stack.setCurrentIndex(index_by_step[step])

        labels = {
            UiStep.INPUT: "新建任务",
            UiStep.SELECTING: "候选选择",
            UiStep.PROCESSING: "处理进度",
            UiStep.DONE: "结果输出",
        }
        if self.step_label:
            self.step_label.setText(labels[step])
        self._refresh_nav(step)
        self._set_running_state(self._process_running)

    def _candidate_item_at(self, row: int) -> CandidateItem | None:
        if row < 0 or row >= len(self._candidate_order):
            return None
        return self._candidate_items.get(self._candidate_order[row])

    def _split_csv(self, raw: str, default: list[str]) -> list[str]:
        if not raw.strip():
            return list(default)
        values = [x.strip() for x in raw.split(",") if x.strip()]
        return values or list(default)

    def _is_valid_cli_input(self, value: str) -> bool:
        item = value.strip()
        if not item:
            return False
        if is_url(item):
            return True
        return Path(item).expanduser().exists()

    def _safe_int(self, raw: str, default: int, min_value: int | None = None) -> int:
        try:
            value = int(raw)
        except ValueError:
            return default
        if min_value is not None and value < min_value:
            return default
        return value

    def _on_advanced_toggled(self, checked: bool) -> None:
        if self.advanced_content:
            self.advanced_content.setVisible(bool(checked))

    def _on_theme_changed(self) -> None:
        if not self.theme_combo:
            return
        value = self.theme_combo.currentData()
        self.theme_key = str(value or "classic_blue")
        self._apply_styles()
        self._refresh_nav("settings" if self.stack and self.stack.currentIndex() == 4 else self.current_step)
        self._save_gui_preferences()

    def _append_input(self, value: str) -> None:
        if not self.input_edit:
            return
        current = self.input_edit.toPlainText().strip()
        if current:
            self.input_edit.append(value)
        else:
            self.input_edit.setPlainText(value)

    def _get_input_items(self) -> list[str]:
        if not self.input_edit:
            return []
        lines = [line.strip() for line in self.input_edit.toPlainText().splitlines() if line.strip()]
        html_blob = self.input_edit.toHtml() or ""

        href_pattern = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
        inline_url_pattern = re.compile(r"""https?://[^\s<>"')\]]+""", re.IGNORECASE)

        merged: list[str] = []
        merged.extend(lines)

        # Handle rich-text paste where visible text is title but href carries the real URL.
        for url in href_pattern.findall(html_blob):
            clean = url.strip()
            if clean and is_url(clean):
                merged.append(clean)

        # Handle lines that include inline URLs mixed with plain text.
        for line in lines:
            for url in inline_url_pattern.findall(line):
                clean = url.strip()
                if clean and is_url(clean):
                    merged.append(clean)

        out: list[str] = []
        seen: set[str] = set()
        for item in merged:
            key = item.strip()
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(key)
        return out

    def _set_running_state(self, running: bool) -> None:
        self._process_running = running
        if self.parse_btn:
            self.parse_btn.setEnabled(self.current_step == UiStep.INPUT and not running and not self._scan_in_progress)
        if self.run_btn:
            self.run_btn.setEnabled(self.current_step == UiStep.SELECTING and not running and not self._scan_in_progress)
        if self.retry_btn:
            self.retry_btn.setEnabled(self.current_step in {UiStep.INPUT, UiStep.SELECTING, UiStep.DONE} and not running and not self._scan_in_progress)
        if self.open_out_btn:
            self.open_out_btn.setEnabled(self.current_step in {UiStep.PROCESSING, UiStep.DONE} and not running)
        if self.open_result_btn:
            self.open_result_btn.setEnabled(self.current_step == UiStep.DONE and not running)
        if self.reveal_result_btn:
            self.reveal_result_btn.setEnabled(self.current_step == UiStep.DONE and not running)
        if self.stop_btn:
            self.stop_btn.setEnabled(self.current_step == UiStep.PROCESSING and running)

        if self.confirm_btn:
            self.confirm_btn.setEnabled(self.current_step == UiStep.SELECTING and not running and self._candidate_mode_active)
        if self.back_btn:
            self.back_btn.setEnabled(self.current_step == UiStep.SELECTING and not running and self._candidate_mode_active)
        if self.candidate_table:
            self.candidate_table.setEnabled(self.current_step == UiStep.SELECTING and not running)

    def _set_scan_state(self, scanning: bool) -> None:
        self._scan_in_progress = scanning
        if scanning:
            QApplication.setOverrideCursor(Qt.WaitCursor)
        else:
            QApplication.restoreOverrideCursor()
        self._set_running_state(self._process_running)

    def _add_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择音频/视频文件",
            "",
            "媒体文件 (*.mp3 *.wav *.m4a *.caf *.mp4 *.mkv *.mov *.webm);;所有文件 (*.*)",
        )
        if path:
            self._append_input(path)

    def _add_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择文件夹")
        if path:
            self._append_input(path)

    def _add_url(self) -> None:
        url, ok = QInputDialog.getText(self, "添加链接", "请输入 URL：")
        if ok and url.strip():
            self._append_input(url.strip())

    def _clear_inputs(self) -> None:
        if self.input_edit:
            self.input_edit.clear()

    def _choose_output(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择输出目录")
        if path:
            self.out_edit.setText(path)

    def _choose_task_output(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择本次任务输出目录")
        if path and self.task_out_edit:
            self.task_out_edit.setText(path)

    def _choose_config(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择配置文件", "", "JSON (*.json);;所有文件 (*.*)")
        if path:
            self.config_edit.setText(path)

    def _choose_failed_log(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择 failed_tasks.jsonl", "", "JSONL (*.jsonl);;所有文件 (*.*)")
        if path:
            self.failed_log_edit.setText(path)

    def _choose_cookies_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择 cookies.txt", "", "Cookies (*.txt);;所有文件 (*.*)")
        if path and self.cookies_file_edit:
            self.cookies_file_edit.setText(path)

    def _update_cookie_controls(self) -> None:
        mode = str(self.cookie_mode_combo.currentData() if self.cookie_mode_combo else "none")
        use_file = mode == "cookies_file"
        use_browser = mode == "browser"
        if self.cookies_file_edit:
            self.cookies_file_edit.setEnabled(use_file)
        if self.cookies_file_btn:
            self.cookies_file_btn.setEnabled(use_file)
        if self.cookies_browser_combo:
            self.cookies_browser_combo.setEnabled(use_browser)

    def _load_cookie_defaults(self) -> None:
        config_path = Path(self.config_edit.text().strip() or "config.json").expanduser()
        if not config_path.is_absolute():
            config_path = (Path.cwd() / config_path).resolve()
        cfg = load_config(config_path if config_path.exists() else None)
        if self.cookie_mode_combo:
            self._set_combo_by_data(self.cookie_mode_combo, getattr(cfg.download, "cookie_mode", "none"))
        if self.cookies_file_edit:
            self.cookies_file_edit.setText(getattr(cfg.download, "cookies_file", "") or "")
        if self.cookies_browser_combo:
            self._set_combo_by_data(self.cookies_browser_combo, getattr(cfg.download, "cookies_browser", "chrome"))

    def _update_task_strategy_hint(self) -> None:
        if not self.task_strategy_hint:
            return

        hints: list[str] = []
        wants_video = bool(self.task_keep_video_chk and self.task_keep_video_chk.isChecked())
        wants_subtitle = bool(self.task_download_subtitle_chk and self.task_download_subtitle_chk.isChecked())
        wants_pdf = bool(self.task_download_pdf_chk and self.task_download_pdf_chk.isChecked())
        wants_audio = bool(self.task_export_audio_chk and self.task_export_audio_chk.isChecked())
        wants_text = bool(self.task_export_text_chk and self.task_export_text_chk.isChecked())
        strategy = str(self.task_subtitle_strategy_combo.currentData() if self.task_subtitle_strategy_combo else "")
        retention = str(self.task_media_retention_combo.currentData() if self.task_media_retention_combo else "")

        if self.task_text_output_combo:
            self.task_text_output_combo.setEnabled(wants_text)

        if wants_subtitle and not wants_video and not wants_audio:
            hints.append("只选择字幕时，系统会优先获取平台字幕；通常不会保留完整视频。")
        if strategy == "whisper_only" or (strategy == "platform_then_whisper" and wants_subtitle):
            hints.append("使用 Whisper 时，系统可能会临时获取音频或视频用于转写。")
        if retention in {"final_only", "temporary_cache"} or not wants_video:
            hints.append("不保留视频时，输出目录只留下你选择的最终结果。")
        if not wants_text:
            hints.append("未勾选导出文本时，文本格式选择不会参与本次任务。")
        if self._has_document_candidates() and not wants_pdf:
            hints.append("未勾选下载PDF时，已选 PDF 候选会在开始处理时跳过。")

        self.task_strategy_hint.setText(" ".join(hints) if hints else "当前选择会按默认智能策略处理。")

    def _sync_media_retention_from_keep_video(self) -> None:
        if not self.task_keep_video_chk or not self.task_media_retention_combo:
            return
        target_index = 2 if self.task_keep_video_chk.isChecked() else 1
        if self.task_media_retention_combo.currentIndex() != target_index:
            self.task_media_retention_combo.setCurrentIndex(target_index)

    def _load_task_strategy_defaults(self, output_root: Path | None = None) -> None:
        if self.task_out_edit:
            self.task_out_edit.setText(str(output_root or self.out_edit.text().strip() or "outputs"))
        if self.task_keep_video_chk:
            self.task_keep_video_chk.setChecked(self.keep_video_chk.isChecked())
        if self.task_save_thumbnail_chk:
            self.task_save_thumbnail_chk.setChecked(False)
        if self.task_save_metadata_chk:
            self.task_save_metadata_chk.setChecked(False)
        if self.task_export_audio_chk:
            self.task_export_audio_chk.setChecked(self.export_audio_chk.isChecked())
        if self.task_download_subtitle_chk:
            self.task_download_subtitle_chk.setChecked(True)
        has_documents = self._has_document_candidates()
        if self.task_download_pdf_chk:
            self.task_download_pdf_chk.setChecked(True)
            self.task_download_pdf_chk.setEnabled(has_documents)
            self.task_download_pdf_chk.setToolTip("当前候选中包含 PDF 时启用；取消后不会下载/归档 PDF 候选。")
        if self.task_export_text_chk:
            self.task_export_text_chk.setChecked(True)
        if self.task_media_retention_combo:
            self.task_media_retention_combo.setCurrentIndex(2 if self.keep_video_chk.isChecked() else 1)
        if self.task_audio_format_combo:
            self.task_audio_format_combo.setCurrentText(self.audio_format_combo.currentText())
        if self.task_subtitle_strategy_combo:
            self.task_subtitle_strategy_combo.setCurrentIndex(1)
        if self.task_subtitle_format_combo:
            self.task_subtitle_format_combo.setCurrentText(self.subtitle_format_combo.currentText())
        if self.task_text_output_combo:
            self.task_text_output_combo.setCurrentText(self.text_output_combo.currentText())
        if self.task_model_combo:
            self.task_model_combo.setCurrentText(self.model_combo.currentText())
        if self.task_language_edit:
            self.task_language_edit.setText(self.language_edit.text())
        if self.task_quality_combo:
            self.task_quality_combo.setCurrentText(self.quality_combo.currentText())
        if self.task_prefer_compatible_chk:
            self.task_prefer_compatible_chk.setChecked(self.prefer_compatible_chk.isChecked())
        if self.task_allow_separate_chk:
            self.task_allow_separate_chk.setChecked(self.allow_separate_chk.isChecked())
        self._update_task_strategy_hint()

    def _build_runtime_config(self, use_task_overrides: bool = False) -> tuple[AppConfig, Path, Path]:
        config_path = Path(self.config_edit.text().strip() or "config.json").expanduser()
        if not config_path.is_absolute():
            config_path = (Path.cwd() / config_path).resolve()
        cfg = load_config(config_path if config_path.exists() else None)

        model_value = self.model_combo.currentText().strip()
        language_value = self.language_edit.text().strip()
        quality_value = self.quality_combo.currentText().strip()
        keep_original = self.keep_video_chk.isChecked()
        save_metadata = bool(getattr(cfg.download, "save_metadata", False))
        export_audio = self.export_audio_chk.isChecked()
        audio_format = self.audio_format_combo.currentText().strip() or "mp3"
        subtitle_format = self.subtitle_format_combo.currentText().strip() or "srt"
        text_output = self.text_output_combo.currentText().strip().lower()
        prefer_compatible = self.prefer_compatible_chk.isChecked()
        allow_separate = self.allow_separate_chk.isChecked()
        output_value = self.out_edit.text().strip() or cfg.output_root

        if use_task_overrides:
            if self.task_model_combo:
                model_value = self.task_model_combo.currentText().strip() or model_value
            if self.task_language_edit:
                language_value = self.task_language_edit.text().strip() or language_value
            if self.task_quality_combo:
                quality_value = self.task_quality_combo.currentText().strip() or quality_value
            if self.task_keep_video_chk:
                keep_original = self.task_keep_video_chk.isChecked()
            if self.task_media_retention_combo:
                retention = str(self.task_media_retention_combo.currentData() or "")
                if self.task_keep_video_chk and not self.task_keep_video_chk.isChecked():
                    keep_original = False
                elif retention == "keep_all":
                    keep_original = True
                elif retention in {"final_only", "temporary_cache"}:
                    keep_original = False
            if self.task_export_audio_chk:
                export_audio = self.task_export_audio_chk.isChecked()
            if self.task_save_metadata_chk:
                save_metadata = self.task_save_metadata_chk.isChecked()
            if self.task_audio_format_combo:
                audio_format = self.task_audio_format_combo.currentText().strip() or audio_format
            if self.task_subtitle_format_combo:
                subtitle_format = self.task_subtitle_format_combo.currentText().strip() or subtitle_format
            if self.task_text_output_combo:
                text_output = self.task_text_output_combo.currentText().strip().lower()
            if self.task_export_text_chk and not self.task_export_text_chk.isChecked():
                text_output = "none"
            if self.task_prefer_compatible_chk:
                prefer_compatible = self.task_prefer_compatible_chk.isChecked()
            if self.task_allow_separate_chk:
                allow_separate = self.task_allow_separate_chk.isChecked()
            if self.task_out_edit and self.task_out_edit.text().strip():
                output_value = self.task_out_edit.text().strip()
            if self.task_subtitle_strategy_combo:
                strategy = str(self.task_subtitle_strategy_combo.currentData() or "platform_then_whisper")
                if self.task_download_subtitle_chk and not self.task_download_subtitle_chk.isChecked():
                    strategy = "skip_text"
                if strategy == "whisper_only":
                    cfg.subtitle_priority = "whisper_only"
                elif strategy == "platform_only":
                    cfg.subtitle_priority = "platform_only"
                elif strategy == "skip_text":
                    cfg.subtitle_priority = "skip_text"
                else:
                    cfg.subtitle_priority = "subtitle_first_then_whisper"

        cfg.whisper.model = model_value or cfg.whisper.model
        cfg.whisper.language = language_value or cfg.whisper.language
        cfg.scraping.candidate_mode = self.candidate_mode_combo.currentText().strip() or "select"
        cfg.download.quality = quality_value or cfg.download.quality
        cfg.scraping.always_try_page_url = self.always_try_chk.isChecked()
        cfg.download.prefer_compatible_codecs = prefer_compatible
        cfg.download.allow_separate_streams = allow_separate
        cfg.download.js_runtimes = self._split_csv(self.js_runtime_edit.text().strip(), ["deno", "node"])
        cfg.download.remote_components = self._split_csv(self.remote_component_edit.text().strip(), ["ejs:github"])
        if self.cookie_mode_combo:
            cfg.download.cookie_mode = str(self.cookie_mode_combo.currentData() or "none")
        if self.cookies_file_edit:
            cfg.download.cookies_file = self.cookies_file_edit.text().strip()
        if self.cookies_browser_combo:
            cfg.download.cookies_browser = str(self.cookies_browser_combo.currentData() or "chrome")
        cfg.scraping.download_archive = self.archive_edit.text().strip() or cfg.scraping.download_archive
        cfg.scraping.user_agent = self.user_agent_edit.text().strip() or cfg.scraping.user_agent
        cfg.scraping.request_timeout_seconds = self._safe_int(self.timeout_edit.text().strip(), 20, min_value=1)
        cfg.download.keep_original = keep_original
        cfg.download.save_metadata = save_metadata
        cfg.download.export_audio = export_audio
        cfg.download.export_audio_format = audio_format
        cfg.download.subtitle_output_format = subtitle_format

        if text_output == "none":
            cfg.download.output_txt = False
            cfg.download.output_srt = False
        elif text_output == "txt":
            cfg.download.output_txt = True
            cfg.download.output_srt = False
        elif text_output == "srt":
            cfg.download.output_txt = False
            cfg.download.output_srt = True
        elif text_output == "md":
            cfg.download.output_txt = True
            cfg.download.output_srt = False
        else:
            cfg.download.output_txt = True
            cfg.download.output_srt = True

        base_dir = config_path.parent if config_path.exists() else Path.cwd()
        output_root = ensure_output_root(base_dir=base_dir, output_root=output_value)
        self.current_output_root = output_root
        return cfg, config_path, output_root

    def _run_processing(self) -> None:
        if self._process_running or self._scan_in_progress:
            return
        if self.current_step != UiStep.INPUT:
            QMessageBox.information(self, "提示", "当前不在输入阶段。")
            return

        input_items = self._get_input_items()
        if not input_items:
            QMessageBox.warning(self, "提示", "请输入至少一个输入项。")
            return

        mode = "auto"
        if self.input_mode_combo:
            idx = self.input_mode_combo.currentIndex()
            if idx == 1:
                mode = "link"
            elif idx == 2:
                mode = "file"
        self._input_mode = mode

        if mode == "link":
            invalid = [x for x in input_items if not is_url(x)]
            if invalid:
                QMessageBox.warning(self, "输入格式不匹配", "当前是“链接”模式，输入中包含非 URL 项，请改为“自动识别”或文件模式。")
                return
        elif mode == "file":
            invalid = [x for x in input_items if is_url(x)]
            if invalid:
                QMessageBox.warning(self, "输入格式不匹配", "当前是“文件/文件夹”模式，输入中包含 URL，请改为“自动识别”或链接模式。")
                return

        ffmpeg_ok, ffmpeg_message = check_ffmpeg_available("ffmpeg")
        if not ffmpeg_ok:
            QMessageBox.critical(self, "ffmpeg 不可用", ffmpeg_message)
            return

        cfg, config_path, output_root = self._build_runtime_config()
        self._append_log(f"[INFO] Using ffmpeg: {ffmpeg_message}")
        self._append_log(f"[INFO] 输出目录: {output_root}")
        self._append_log("[INFO] 解析完成后会切换到候选选择阶段。")

        self._clear_candidate_state(reset_context=True)
        self._parsed_input_items = input_items

        if mode == "link":
            self._prepare_candidate_selection(input_items, cfg, output_root, config_path)
            return

        if mode == "file":
            self._prepare_local_selection(input_items, cfg, output_root, config_path)
            return

        url_count = sum(1 for x in input_items if is_url(x))
        local_count = len(input_items) - url_count
        self._append_log(f"[INFO] 自动识别：链接 {url_count} 条，本地 {local_count} 条。")
        if url_count > 0:
            self._prepare_candidate_selection(input_items, cfg, output_root, config_path)
            return
        self._prepare_local_selection(input_items, cfg, output_root, config_path)

    def _run_retry(self) -> None:
        if self._process_running or self._scan_in_progress or self._candidate_mode_active:
            return

        failed_raw = self.failed_log_edit.text().strip()
        if not failed_raw:
            QMessageBox.warning(self, "提示", "请先选择 failed_tasks.jsonl。")
            return

        failed_path = Path(failed_raw).expanduser().resolve()
        if not failed_path.exists():
            QMessageBox.critical(self, "错误", f"文件不存在：{failed_path}")
            return

        cfg, config_path, output_root = self._build_runtime_config()
        run_id = uuid.uuid4().hex[:12]
        self.current_run_id = run_id
        self.current_output_root = output_root

        cmd = [
            _python_console_executable(),
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
        self._launch_process(cmd)

    def _prepare_candidate_selection(
        self,
        input_items: list[str],
        cfg: AppConfig,
        output_root: Path,
        config_path: Path,
    ) -> None:
        self._clear_candidate_state(reset_context=True)
        self._candidate_scan_token += 1
        scan_token = self._candidate_scan_token
        self._append_log("[INFO] 开始扫描候选链接...")
        self._set_step(UiStep.PROCESSING)
        self._set_preparation_progress(0, len(input_items), "正在准备任务")
        self._set_scan_state(True)

        def worker() -> None:
            archive_path = Path(cfg.scraping.download_archive).expanduser()
            if not archive_path.is_absolute():
                archive_path = (output_root / "后台数据" / archive_path).resolve()
            seen_keys = load_seen_archive(archive_path)

            direct_inputs: list[str] = []
            candidate_urls: list[tuple[str, str]] = []
            seen_candidate_urls: set[str] = set()

            total_inputs = len(input_items)
            for idx, raw in enumerate(input_items, start=1):
                self.candidate_scan_finished.emit(
                    {
                        "progress": True,
                        "index": idx,
                        "total": total_inputs,
                        "stage": "正在识别直接媒体链接",
                        "token": scan_token,
                    }
                )
                if not is_url(raw):
                    if self._is_valid_cli_input(raw):
                        candidate_urls.append((raw, raw))
                    else:
                        self.candidate_scan_finished.emit(
                            {
                                "warn": f"[WARN] 已忽略非链接/非本地路径的显示文本，不会传给 --input: {raw}",
                                "done": False,
                                "token": scan_token,
                            }
                        )
                    continue
                if is_direct_media_url(raw) or is_direct_document_url(raw):
                    candidate_urls.append((raw, raw))
                    self.candidate_scan_finished.emit(
                        {
                            "warn": f"[INFO] 检测到直接资源链接，跳过网页扫描: {raw}",
                            "progress": True,
                            "index": idx,
                            "total": total_inputs,
                            "stage": "正在跳过网页扫描",
                            "done": False,
                            "token": scan_token,
                        }
                    )
                    continue
                try:
                    self.candidate_scan_finished.emit(
                        {
                            "progress": True,
                            "index": idx,
                            "total": total_inputs,
                            "stage": "正在扫描网页候选",
                            "done": False,
                            "token": scan_token,
                        }
                    )
                    found = discover_targets(
                        pages=[raw],
                        timeout=int(cfg.scraping.request_timeout_seconds),
                        user_agent=cfg.scraping.user_agent,
                        always_try_page_url=bool(cfg.scraping.always_try_page_url),
                    )
                    targets = sorted(found) if found else [raw]
                except Exception as exc:  # noqa: BLE001
                    targets = [raw]
                    self.candidate_scan_finished.emit(
                        {
                            "warn": f"[WARN] 候选扫描失败，回退直接链接: {raw} ({exc})",
                            "done": False,
                            "token": scan_token,
                        }
                    )

                for target in targets:
                    if target in seen_candidate_urls:
                        continue
                    seen_candidate_urls.add(target)
                    candidate_urls.append((raw, target))

            payload = {
                "cfg": cfg,
                "output_root": output_root,
                "archive_path": archive_path,
                "seen_keys": seen_keys,
                "direct_inputs": direct_inputs,
                "candidate_urls": candidate_urls,
                "config_path": config_path,
                "done": True,
                "token": scan_token,
            }
            self.candidate_scan_finished.emit(payload)

        threading.Thread(target=worker, daemon=True).start()

    def _prepare_local_selection(
        self,
        input_items: list[str],
        cfg: AppConfig,
        output_root: Path,
        config_path: Path,
    ) -> None:
        self._clear_candidate_state(reset_context=True)
        self._candidate_scan_token += 1
        archive_path = Path(cfg.scraping.download_archive).expanduser()
        if not archive_path.is_absolute():
            archive_path = (output_root / "后台数据" / archive_path).resolve()

        for idx, item in enumerate(input_items, start=1):
            if not self._is_valid_cli_input(item):
                self._append_log(f"[WARN] 已忽略非本地路径，不会加入候选: {item}")
                continue
            item_id = f"local-{idx}"
            title = Path(item).stem or Path(item).name or item
            source_kind = "document" if Path(item).suffix.lower() == ".pdf" else "local"
            self._candidate_items[item_id] = CandidateItem(
                item_id=item_id,
                source_url="",
                url=item,
                is_seen=False,
                checked=True,
                title=title,
                source_kind=source_kind,
            )
            self._candidate_order.append(item_id)

        self._pending_run_context = (cfg, output_root, archive_path, [], config_path)
        if not self._candidate_order:
            self._append_log("[WARN] 未发现候选，请检查输入。")
        self._show_candidates()
        self._append_log(f"[INFO] 本地输入解析完成：{len(self._candidate_order)} 条。")

    def _on_candidate_scan_finished(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        if int(payload.get("token") or 0) != self._candidate_scan_token:
            return
        if payload.get("progress"):
            self._set_preparation_progress(
                int(payload.get("index") or 0),
                int(payload.get("total") or 0),
                str(payload.get("stage") or "正在准备任务"),
            )
        warn = payload.get("warn")
        if warn:
            self._append_log(str(warn))
            if not payload.get("done"):
                return
        if payload.get("progress") and not payload.get("done"):
            return

        self._set_scan_state(False)

        cfg = payload["cfg"]
        output_root = payload["output_root"]
        archive_path = payload["archive_path"]
        seen_keys = payload["seen_keys"]
        direct_inputs = payload["direct_inputs"]
        candidate_urls = payload["candidate_urls"]
        config_path = payload["config_path"]

        if not candidate_urls:
            self._clear_candidate_state(reset_context=False)
            self._pending_run_context = (cfg, output_root, archive_path, direct_inputs, config_path)
            self._append_log("[WARN] 未发现候选，请检查输入。")
            self._show_candidates()
            return

        self._clear_candidate_state(reset_context=False)
        for idx, (source_url, target_url) in enumerate(candidate_urls, start=1):
            is_local = not is_url(target_url)
            is_document = (is_local and Path(target_url).suffix.lower() == ".pdf") or is_direct_document_url(target_url)
            key = dedup_key(target_url) if not is_local else target_url
            is_seen = (not is_local) and key in seen_keys
            item_id = f"{'doc' if is_document else 'local' if is_local else 'cand'}-{idx}"
            title = Path(target_url).stem if is_local else self._build_candidate_fallback_title(
                source_url=source_url,
                target_url=target_url,
                index=idx,
            )
            self._candidate_items[item_id] = CandidateItem(
                item_id=item_id,
                source_url="" if is_local else source_url,
                url=target_url,
                is_seen=is_seen,
                checked=not is_seen,
                title=title,
                source_kind="document" if is_document else "local" if is_local else "url",
            )
            self._candidate_order.append(item_id)

        self._pending_run_context = (cfg, output_root, archive_path, direct_inputs, config_path)
        if not self._validate_candidate_scope():
            self._clear_candidate_state(reset_context=True)
            self._append_log("[WARN] 候选来源与当前输入不一致，已阻止显示旧候选，请重新解析。")
            QMessageBox.warning(self, "候选已过期", "候选来源与当前输入不一致，已清空候选。请重新点击“开始解析”。")
            self._set_step(UiStep.INPUT)
            self._set_running_state(False)
            return
        self._show_candidates()
        self._append_log(f"[INFO] 候选发现完成：{len(self._candidate_order)} 条。")

    def _show_candidates(self) -> None:
        if not self.candidate_group or not self.candidate_table:
            return
        if not self._validate_candidate_scope():
            self._clear_candidate_state(reset_context=True)
            self._append_log("[WARN] 候选来源与当前输入不一致，已阻止进入候选页。")
            QMessageBox.warning(self, "候选已过期", "候选来源与当前输入不一致，已清空候选。请重新点击“开始解析”。")
            self._set_step(UiStep.INPUT)
            self._set_running_state(False)
            return
        self._candidate_mode_active = True
        self._refresh_candidate_table()
        if self._pending_run_context is not None:
            _cfg, output_root, _archive_path, _direct_inputs, _config_path = self._pending_run_context
            self._load_task_strategy_defaults(output_root)
        self._set_step(UiStep.SELECTING)

        if self._candidate_order:
            first_id = self._candidate_order[0]
            first_row = 0
            self.candidate_table.selectRow(first_row)
            self._update_preview(first_id)
            self._prefetch_meta(first_id)
            for item_id in self._candidate_order[1:4]:
                self._prefetch_meta(item_id)
        else:
            self._reset_preview("未发现候选，请检查输入。")

        self._set_running_state(False)

    def _clear_candidate_state(self, reset_context: bool) -> None:
        self._candidate_mode_active = False
        if reset_context:
            self._pending_run_context = None
        self._candidate_items.clear()
        self._candidate_order.clear()
        self._candidate_rows.clear()
        if self.candidate_model:
            self.candidate_model.reset_rows()
        if self.candidate_table:
            self.candidate_table.clearSelection()
        self._update_summary()
        self._reset_preview()

    def _hide_candidates(self) -> None:
        self._clear_candidate_state(reset_context=True)
        self._set_step(UiStep.INPUT)
        self._set_running_state(False)

    def _refresh_candidate_table(self) -> None:
        if self.candidate_model is None:
            return
        self._table_updating = True
        self._candidate_rows = list(self._candidate_order)
        self.candidate_model.reset_rows()
        if self.candidate_table:
            for row, item_id in enumerate(self._candidate_rows):
                item = self._candidate_items[item_id]
                self.candidate_table.setRowHeight(row, 56 if item.thumb_bytes else 34)
        self._table_updating = False
        self._update_summary()

    def _candidate_status_text(self, item: CandidateItem) -> str:
        if item.source_kind == "document":
            return "未处理"
        if item.source_kind == "local":
            return "未处理"
        return "已下载" if item.is_seen else "未下载"

    def _candidate_source_text(self, item: CandidateItem) -> str:
        if item.source_kind == "document":
            return "PDF文档"
        if item.source_kind == "local":
            return "本地文件"
        host = urlparse(item.url).netloc.lower().replace("www.", "").strip()
        return host or "web"

    def _has_document_candidates(self) -> bool:
        return any(
            (item := self._candidate_items.get(item_id)) is not None and item.source_kind == "document"
            for item_id in self._candidate_order
        )

    def _build_thumb_icon(self, item: CandidateItem) -> QIcon | None:
        if not item.thumb_bytes:
            return None
        pix = QPixmap()
        if not pix.loadFromData(item.thumb_bytes):
            return None
        return QIcon(pix.scaled(96, 54, Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def _update_summary(self) -> None:
        if not self.candidate_summary:
            return
        total = len(self._candidate_order)
        selected = sum(1 for item_id in self._candidate_order if self._candidate_items[item_id].checked)
        self.candidate_summary.setText(f"共 {total} 条，已选择 {selected} 条")

    def _reset_preview(self, title: str = "未选择候选视频") -> None:
        if self.preview_image:
            self.preview_image.setPixmap(QPixmap())
            self.preview_image.setText("暂无封面")
        if self.preview_title:
            self.preview_title.setText(title)
        if self.preview_status:
            self.preview_status.setText("")
        if self.preview_url:
            self.preview_url.setText("")

    def _on_candidate_selected(self) -> None:
        if not self.candidate_table:
            return
        row = self.candidate_table.currentIndex().row()
        if row < 0 or row >= len(self._candidate_rows):
            return
        item_id = self._candidate_rows[row]
        self._update_preview(item_id)
        self._prefetch_meta(item_id)

    def _on_candidate_clicked(self, index: QModelIndex) -> None:
        if self._table_updating:
            return
        row = index.row()
        if row < 0 or row >= len(self._candidate_rows):
            return
        item_id = self._candidate_rows[row]
        item = self._candidate_items[item_id]
        if index.column() == 0:
            item.checked = not item.checked
            if self.candidate_model:
                self.candidate_model.refresh_row(row)
            self._update_summary()
        if self.candidate_table:
            self.candidate_table.selectRow(row)
        self._update_preview(item_id)
        self._prefetch_meta(item_id)

    def _update_preview(self, item_id: str) -> None:
        item = self._candidate_items.get(item_id)
        if not item:
            return
        if self.preview_title:
            self.preview_title.setText(item.title)
        if self.preview_status:
            if item.source_kind == "document":
                self.preview_status.setText("未处理 | PDF文档")
            elif item.source_kind == "local":
                self.preview_status.setText("未处理 | 本地文件")
            else:
                host = urlparse(item.source_url).netloc or "unknown"
                self.preview_status.setText(self._candidate_status_text(item) + f" | 来源页: {host}")
        if self.preview_url:
            self.preview_url.setText(item.url)
        self._render_preview_image(item)

    def _render_preview_image(self, item: CandidateItem) -> None:
        if not self.preview_image:
            return
        if not item.thumb_bytes:
            self.preview_image.setPixmap(QPixmap())
            self.preview_image.setText("暂无封面")
            return
        pix = QPixmap()
        if not pix.loadFromData(item.thumb_bytes):
            self.preview_image.setPixmap(QPixmap())
            self.preview_image.setText("封面加载失败")
            return
        scaled = pix.scaled(410, 230, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.preview_image.setText("")
        self.preview_image.setPixmap(scaled)

    def _candidate_select_unseen(self) -> None:
        for item_id in self._candidate_order:
            self._candidate_items[item_id].checked = not self._candidate_items[item_id].is_seen
        self._refresh_candidate_table()

    def _candidate_select_all(self) -> None:
        for item_id in self._candidate_order:
            self._candidate_items[item_id].checked = True
        self._refresh_candidate_table()

    def _candidate_clear(self) -> None:
        for item_id in self._candidate_order:
            self._candidate_items[item_id].checked = False
        self._refresh_candidate_table()

    def _candidate_invert(self) -> None:
        for item_id in self._candidate_order:
            self._candidate_items[item_id].checked = not self._candidate_items[item_id].checked
        self._refresh_candidate_table()

    def _confirm_candidates(self) -> None:
        context = self._pending_run_context
        if context is None:
            self._hide_candidates()
            return

        _cfg, _output_root, _archive_path, direct_inputs, _config_path = context
        picked: list[str] = []
        source_pages: dict[str, str] = {}
        force_keys: set[str] = set()
        for item_id in self._candidate_order:
            item = self._candidate_items[item_id]
            if not item.checked:
                continue
            if item.source_kind == "document" and self.task_download_pdf_chk and not self.task_download_pdf_chk.isChecked():
                continue
            picked.append(item.url)
            if item.source_url and item.source_url != item.url:
                source_pages[item.url] = item.source_url
            if item.is_seen and self.force_seen_chk.isChecked():
                force_keys.add(dedup_key(item.url))

        if not picked and not direct_inputs:
            QMessageBox.warning(self, "提示", "没有勾选任何候选。")
            return

        run_items = [*direct_inputs, *picked]
        self._candidate_mode_active = False
        cfg, config_path, output_root = self._build_runtime_config(use_task_overrides=True)
        self._start_run_process(cfg, config_path, output_root, run_items, force_keys, source_pages=source_pages)

    def _validate_candidate_scope(self) -> bool:
        if not self._candidate_order:
            return True
        parsed_inputs = {item.strip() for item in self._parsed_input_items if item.strip()}
        if not parsed_inputs:
            return True
        local_inputs = {item for item in parsed_inputs if not is_url(item) and self._is_valid_cli_input(item)}
        if local_inputs and len(local_inputs) == len(parsed_inputs):
            for item_id in self._candidate_order:
                item = self._candidate_items.get(item_id)
                if not item or item.source_kind not in {"local", "document"} or item.url not in local_inputs or item.source_url:
                    return False
        return True

    def _back_from_candidates(self) -> None:
        self._append_log("[INFO] 已返回输入编辑。")
        self._hide_candidates()

    def _prefetch_meta(self, item_id: str) -> None:
        item = self._candidate_items.get(item_id)
        if not item or item.source_kind in {"local", "document"} or item.meta_loaded or item.meta_loading:
            return
        item.meta_loading = True

        def worker() -> None:
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
                    resp = requests.get(thumb_url, timeout=8, headers={"User-Agent": self.user_agent_edit.text().strip() or "Mozilla/5.0"})
                    resp.raise_for_status()
                    thumb_bytes = resp.content or None
                except Exception:
                    thumb_bytes = None

            self.candidate_meta_ready.emit(item_id, title, thumb_url, thumb_bytes)

        threading.Thread(target=worker, daemon=True).start()

    def _apply_candidate_meta(self, item_id: str, title: str, thumb_url: str, thumb_payload: object) -> None:
        item = self._candidate_items.get(item_id)
        if not item:
            return
        if title and not self._is_invalid_candidate_title(title):
            item.title = title.strip()
        item.thumbnail_url = thumb_url or item.thumbnail_url
        item.thumb_bytes = thumb_payload if isinstance(thumb_payload, bytes) else None
        item.meta_loading = False
        item.meta_loaded = True

        row = self._candidate_rows.index(item_id) if item_id in self._candidate_rows else -1
        if row >= 0:
            if self.candidate_model:
                self.candidate_model.refresh_row(row)
            if self.candidate_table and item.thumb_bytes:
                self.candidate_table.setRowHeight(row, 56)

        current_row = self.candidate_table.currentIndex().row() if self.candidate_table else -1
        if current_row >= 0 and current_row < len(self._candidate_rows) and self._candidate_rows[current_row] == item_id:
            self._update_preview(item_id)

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
        return {"title": info.get("title"), "thumbnail": info.get("thumbnail")}

    def _guess_title(self, url: str) -> str:
        parsed = urlparse(url)
        host = parsed.netloc.lower().replace("www.", "")
        tail = parsed.path.strip("/").split("/")[-1] if parsed.path else ""
        if tail:
            return f"{host}/{tail}"
        return host or url

    def _candidate_naming_mode(self) -> str:
        if self.candidate_naming_combo:
            value = self.candidate_naming_combo.currentData()
            if isinstance(value, str):
                return value
        return "page_title"

    def _is_invalid_candidate_title(self, title: str) -> bool:
        cleaned = unquote(title).strip().strip(".-_ ")
        if not cleaned:
            return True
        tail = cleaned.rsplit("/", 1)[-1].strip().strip(".-_ ").lower()
        stem = tail.rsplit(".", 1)[0].strip() if "." in tail else tail
        return tail in INVALID_CANDIDATE_TITLES or stem in INVALID_CANDIDATE_TITLES

    def _path_tail_title(self, url: str) -> str:
        parsed = urlparse(url)
        tail = parsed.path.strip("/").split("/")[-1] if parsed.path else ""
        tail = unquote(tail).strip().strip(".-_ ")
        if "." in tail:
            tail = tail.rsplit(".", 1)[0].strip()
        tail = re.sub(r"[-_]+", " ", tail).strip()
        if self._is_invalid_candidate_title(tail):
            return ""
        return tail

    def _source_domain_title(self, source_url: str, target_url: str) -> str:
        host = urlparse(source_url).netloc.lower().replace("www.", "").strip()
        if not host:
            host = urlparse(target_url).netloc.lower().replace("www.", "").strip()
        return host

    def _build_candidate_fallback_title(self, source_url: str, target_url: str, index: int) -> str:
        mode = self._candidate_naming_mode()
        unnamed = f"未命名候选-{index:03d}"
        host = self._source_domain_title(source_url, target_url)

        if mode == "sequence":
            return unnamed
        if mode == "domain_index":
            return f"{host} - 候选{index:03d}" if host else unnamed

        candidate = self._guess_title(target_url).strip()
        if source_url == target_url and candidate and not self._is_invalid_candidate_title(candidate):
            return candidate

        page_title = self._path_tail_title(source_url)
        if page_title:
            return f"{page_title} - 候选{index:03d}"

        target_tail = self._path_tail_title(target_url)
        if target_tail:
            return f"{target_tail} - 候选{index:03d}"

        if host:
            return f"{host} - 候选{index:03d}"

        return unnamed

    def _start_run_process(
        self,
        cfg: AppConfig,
        config_path: Path,
        output_root: Path,
        input_items: list[str],
        force_keys: set[str],
        source_pages: dict[str, str] | None = None,
    ) -> None:
        if not input_items:
            QMessageBox.warning(self, "提示", "没有可执行输入项。")
            return

        run_id = uuid.uuid4().hex[:12]
        self.current_run_id = run_id
        self.current_output_root = output_root
        self.stop_requested = False
        self._reset_processing_progress(len(input_items), output_root)
        self._set_step(UiStep.PROCESSING)

        cmd = [
            _python_console_executable(),
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
            "--subtitle-format",
            cfg.download.subtitle_output_format,
            "--subtitle-priority",
            cfg.subtitle_priority,
            "--audio-format",
            cfg.download.export_audio_format,
            "--request-timeout",
            str(cfg.scraping.request_timeout_seconds),
            "--run-id",
            run_id,
        ]
        cmd.append("--always-try-page" if cfg.scraping.always_try_page_url else "--no-always-try-page")
        cmd.append("--keep-original" if cfg.download.keep_original else "--no-keep-original")
        cmd.append("--save-metadata" if cfg.download.save_metadata else "--no-save-metadata")
        cmd.append("--export-audio" if cfg.download.export_audio else "--no-export-audio")
        cmd.append("--prefer-compatible-codecs" if cfg.download.prefer_compatible_codecs else "--no-prefer-compatible-codecs")
        cmd.append("--allow-separate-streams" if cfg.download.allow_separate_streams else "--no-allow-separate-streams")
        if cfg.download.output_txt and cfg.download.output_srt:
            cmd.extend(["--text-output", "txt+srt"])
        elif cfg.download.output_txt:
            cmd.extend(["--text-output", "txt"])
        elif cfg.download.output_srt:
            cmd.extend(["--text-output", "srt"])
        else:
            cmd.extend(["--text-output", "none"])

        if cfg.scraping.user_agent:
            cmd.extend(["--user-agent", cfg.scraping.user_agent])
        for runtime in cfg.download.js_runtimes:
            cmd.extend(["--js-runtime", runtime])
        for component in cfg.download.remote_components:
            cmd.extend(["--remote-component", component])
        for key in sorted(force_keys):
            cmd.extend(["--force-key", key])
        if cfg.download.cookie_mode == "cookies_file" and cfg.download.cookies_file:
            cmd.extend(["--cookies-file", cfg.download.cookies_file])
        elif cfg.download.cookie_mode == "browser" and cfg.download.cookies_browser:
            cmd.extend(["--cookies-from-browser", cfg.download.cookies_browser])
        for media_url, page_url in sorted((source_pages or {}).items()):
            cmd.extend(["--source-page", f"{media_url}||{page_url}"])
        valid_inputs: list[str] = []
        for item in input_items:
            if not self._is_valid_cli_input(item):
                self._append_log(f"[WARN] 已跳过无效输入，不传给 --input: {item}")
                continue
            valid_inputs.append(item)
        if not valid_inputs:
            QMessageBox.warning(self, "提示", "没有有效的 URL 或本地路径可执行。")
            return
        for item in valid_inputs:
            cmd.extend(["--input", item])

        self._launch_process(cmd)

    def _launch_process(self, cmd: list[str]) -> None:
        if self.proc is not None and self.proc.state() != QProcess.NotRunning:
            return
        self._append_log(f"[INFO] 启动命令: {' '.join(self._redact_command_for_log(cmd))}")
        self._set_processing_stage("进程已启动，等待后端返回任务列表")

        proc = QProcess(self)
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONIOENCODING", "utf-8")
        env.insert("PYTHONUTF8", "1")
        proc.setProcessEnvironment(env)
        proc.setProcessChannelMode(QProcess.MergedChannels)
        proc.readyReadStandardOutput.connect(self._on_process_output)
        proc.finished.connect(self._on_process_finished)
        proc.errorOccurred.connect(self._on_process_error)

        self.proc = proc
        self._set_running_state(True)
        proc.start(cmd[0], cmd[1:])

    @staticmethod
    def _redact_command_for_log(cmd: list[str]) -> list[str]:
        redacted: list[str] = []
        hide_next = False
        for part in cmd:
            if hide_next:
                redacted.append("<hidden>")
                hide_next = False
                continue
            redacted.append(part)
            if part in {"--cookies-file", "--source-page"}:
                hide_next = True
        return redacted

    def _on_process_output(self) -> None:
        if self.proc is None:
            return
        raw = bytes(self.proc.readAllStandardOutput()).decode("utf-8", errors="replace")
        if not raw:
            return
        for line in raw.splitlines():
            suppress_log = self._handle_process_line(line)
            if suppress_log:
                continue
            display_line = self._format_process_log_line(line)
            if display_line:
                self._append_log(display_line)

    def _on_process_error(self, _error: QProcess.ProcessError) -> None:
        if self.proc is None:
            return
        self._append_log(f"[ERROR] 进程错误: {self.proc.errorString()}")
        self._set_processing_stage("进程启动或运行异常")

    def _on_process_finished(self, exit_code: int, _status) -> None:
        self._set_running_state(False)
        self.proc = None
        return_to_candidates = False

        if self.stop_requested:
            self._append_log("[INFO] 已按用户请求停止。")
            self._append_stopped_record()
            return_to_candidates = self._ask_rollback_after_stop()
            if self.progress_label:
                self.progress_label.setText("已停止。可打开输出目录检查保留文件。")
            self._set_processing_stage("已停止")
        elif exit_code == 0:
            self._append_log("[DONE] 任务完成。")
            if self.progress_label:
                self.progress_label.setText(f"处理完成。\n输出目录：{self.current_output_root or self.out_edit.text().strip()}")
            self._progress_completed = self._progress_total
            self._set_processing_stage("全部任务处理完成")
            self._refresh_processing_progress()
            self._load_result_text()
        else:
            self._append_log(f"[ERROR] 进程退出码: {exit_code}")
            if self.progress_label:
                self.progress_label.setText(f"处理失败，退出码：{exit_code}。请查看下方日志。")
            self._set_processing_stage("处理失败，请查看运行日志")
            self._load_result_text()

        self.stop_requested = False
        if return_to_candidates:
            self._restore_candidate_selection_after_stop()
        else:
            self._set_step(UiStep.DONE)

    def _stop_processing(self) -> None:
        if self.proc is None or self.proc.state() == QProcess.NotRunning:
            return
        self.stop_requested = True
        self._append_log("[INFO] 收到停止请求，正在终止当前运行...")
        self.proc.terminate()
        QTimer.singleShot(2200, self._force_kill_if_needed)

    def _force_kill_if_needed(self) -> None:
        if self.proc is None or self.proc.state() == QProcess.NotRunning:
            return
        self._append_log("[WARN] 进程未退出，执行强制停止。")
        self.proc.kill()

    def _append_stopped_record(self) -> None:
        if not self.current_output_root:
            return
        failed_path = self.current_output_root / "后台数据" / "failed_tasks.jsonl"
        failed_raw = self.failed_log_edit.text().strip()
        if failed_raw:
            failed_path = Path(failed_raw).expanduser().resolve()
        append_jsonl(
            failed_path,
            {
                "run_id": self.current_run_id or "",
                "status": "failed",
                "stage": "stopped_by_user",
                "error": "Stopped by user in GUI",
            },
        )

    def _ask_rollback_after_stop(self) -> bool:
        if not self.current_output_root or not self.current_run_id:
            return False
        msg = QMessageBox(self)
        msg.setWindowTitle("停止后回退")
        msg.setText("选择回退范围：")
        msg.setInformativeText("仅回退当前任务 / 回退本次运行 / 保留已生成内容")
        btn_task = msg.addButton("仅回退当前任务", QMessageBox.YesRole)
        btn_run = msg.addButton("回退本次运行", QMessageBox.NoRole)
        btn_keep = msg.addButton("保留已生成内容", QMessageBox.RejectRole)
        msg.setDefaultButton(btn_keep)
        msg.exec()

        clicked = msg.clickedButton()
        if clicked is None or clicked == btn_keep:
            self._append_log("[INFO] 保留当前已生成内容。")
            return False

        scope = "task" if clicked == btn_task else "run"
        result = rollback_from_ledger(self.current_output_root, self.current_run_id, scope=scope)
        self._append_log(f"[INFO] 回退完成（scope={scope}）：删除 {result.get('deleted', 0)}，跳过 {result.get('skipped', 0)}。")
        return True

    def _restore_candidate_selection_after_stop(self) -> None:
        self.stop_requested = False
        self.proc = None
        self._process_running = False
        self.current_run_id = None
        if self._candidate_order and self._pending_run_context is not None:
            self._candidate_mode_active = True
            self._refresh_candidate_table()
            self._set_step(UiStep.SELECTING)
            if self.candidate_table and self._candidate_rows:
                current_row = self.candidate_table.currentIndex().row()
                if current_row < 0 or current_row >= len(self._candidate_rows):
                    current_row = 0
                    self.candidate_table.selectRow(current_row)
                self._update_preview(self._candidate_rows[current_row])
            self._append_log("[INFO] 已回到候选选择，可重新勾选并再次开始处理。")
        else:
            self._candidate_mode_active = False
            self._set_step(UiStep.INPUT)
            self._append_log("[WARN] 没有可恢复的候选上下文，已返回输入页。")
        self._set_running_state(False)

    def _open_output_dir(self) -> None:
        output_root = self.current_output_root
        if output_root is None:
            _, _, output_root = self._build_runtime_config()
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(output_root)))

    def _current_run_rows(self) -> list[dict]:
        if not self.current_output_root or not self.current_run_id:
            return []
        manifest_path = self.current_output_root / "后台数据" / "manifest.jsonl"
        if not manifest_path.exists():
            legacy_system_path = self.current_output_root / "_system" / "manifest.jsonl"
            manifest_path = legacy_system_path if legacy_system_path.exists() else self.current_output_root / "manifest.jsonl"
        return [row for row in read_jsonl(manifest_path) if row.get("run_id") == self.current_run_id]

    @staticmethod
    def _friendly_failure_reason(error: str, retry_suggestion: str = "") -> str:
        text = (error or "").strip()
        low = text.lower()
        if any(token in low for token in ("403", "401", "forbidden", "access denied", "url expired")):
            message = "服务器拒绝访问，可能需要来源页 Referer、Cookie，或视频链接已经过期。"
            if "已尝试来源页 referer" in low or "source-page referer" in low:
                message += " 已尝试来源页回退但仍失败。"
            if retry_suggestion:
                message += f" 建议：{retry_suggestion}"
            return message
        return text or retry_suggestion or "未记录错误原因"

    def _primary_result_file(self) -> Path | None:
        priority = ("document", "audio", "txt", "srt", "media")
        for row in self._current_run_rows():
            if str(row.get("status") or "").lower() != "success":
                continue
            artifacts = row.get("artifacts")
            if not isinstance(artifacts, dict):
                continue
            for key in priority:
                value = str(artifacts.get(key) or "").strip()
                if not value:
                    continue
                path = Path(value)
                if path.exists() and path.is_file():
                    return path
        return None

    def _open_primary_result_file(self) -> None:
        path = self._primary_result_file()
        if not path:
            QMessageBox.information(self, "提示", "没有找到可直接打开的本次结果文件。")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _reveal_primary_result_file(self) -> None:
        path = self._primary_result_file()
        if not path:
            QMessageBox.information(self, "提示", "没有找到可定位的本次结果文件。")
            return
        if sys.platform.startswith("win"):
            QProcess.startDetached("explorer.exe", ["/select,", str(path)])
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))

    def _load_result_text(self) -> None:
        if not self.result_text:
            return
        if not self.current_output_root or not self.current_run_id:
            self.result_text.setPlainText("暂无可展示的结果。")
            return

        rows = self._current_run_rows()
        if not rows:
            self.result_text.setPlainText(
                f"没有找到本次运行的结果记录。\n输出目录：{self.current_output_root}"
            )
            return

        failures = [row for row in rows if str(row.get("status") or "").lower() == "failed"]
        successes = [row for row in rows if str(row.get("status") or "").lower() == "success"]
        skipped = [row for row in rows if str(row.get("status") or "").lower() == "skipped"]

        sections: list[str] = []
        if failures:
            lines = ["# 任务失败", ""]
            for index, row in enumerate(failures, start=1):
                source = str(row.get("retry_input") or row.get("resolved_input") or row.get("source") or "unknown")
                stage = str(row.get("stage") or "unknown")
                error = self._friendly_failure_reason(
                    str(row.get("error") or ""),
                    str(row.get("retry_suggestion") or ""),
                )
                lines.append(f"{index}. {source}")
                lines.append(f"   阶段：{stage}")
                lines.append(f"   原因：{error}")
            sections.append("\n".join(lines))

        if successes:
            lines = ["# 成功结果", ""]
            for index, row in enumerate(successes, start=1):
                title = str(row.get("title") or row.get("resolved_input") or f"结果 {index}")
                artifacts = row.get("artifacts")
                lines.append(f"## {index}. {title}")
                if not isinstance(artifacts, dict):
                    lines.append("- 未记录输出文件")
                    continue
                artifact_labels = {
                    "document": "文档",
                    "audio": "音频",
                    "srt": "字幕",
                    "txt": "文本",
                    "json": "元数据",
                    "subtitle_raw": "原始字幕",
                }
                media_value = str(artifacts.get("media") or "").strip()
                if media_value:
                    artifact_labels["media"] = "视频"
                wrote_any = False
                for key, label in artifact_labels.items():
                    value = str(artifacts.get(key) or "").strip()
                    if value:
                        lines.append(f"- {label}: {value}")
                        wrote_any = True
                if not wrote_any:
                    lines.append("- 本项成功，但没有保留可展示的输出文件")
            sections.append("\n".join(lines))

        if skipped:
            lines = ["# 已跳过", ""]
            for index, row in enumerate(skipped, start=1):
                source = str(row.get("resolved_input") or row.get("source") or "unknown")
                reason = str(row.get("error") or "已跳过")
                lines.append(f"{index}. {source} - {reason}")
            sections.append("\n".join(lines))

        snippets: list[str] = []
        for row in rows:
            artifacts = row.get("artifacts")
            if not isinstance(artifacts, dict):
                continue
            txt_path = Path(str(artifacts.get("txt") or ""))
            if not txt_path.exists() or not txt_path.is_file():
                continue
            title = str(row.get("title") or txt_path.stem)
            try:
                body = txt_path.read_text(encoding="utf-8", errors="replace").strip()
            except Exception:
                continue
            if body:
                snippets.append(f"# {title}\n\n{body}")

        if snippets:
            sections.append("# 文本预览\n\n" + "\n\n---\n\n".join(snippets))

        if sections:
            self.result_text.setPlainText("\n\n".join(sections))
        else:
            self.result_text.setPlainText(
                f"处理已结束，但没有找到可展示的输出记录。\n输出目录：{self.current_output_root}"
            )

    def _export_result_text(self, fmt: str) -> None:
        if not self.result_text:
            return
        text = self.result_text.toPlainText().strip()
        if not text:
            QMessageBox.information(self, "提示", "当前没有可导出的文本结果。")
            return

        suffix = "md" if fmt == "md" else "txt"
        default_dir = str(self.current_output_root or Path.cwd())
        path, _ = QFileDialog.getSaveFileName(
            self,
            "导出结果",
            str(Path(default_dir) / f"media2text_result.{suffix}"),
            f"{suffix.upper()} (*.{suffix});;所有文件 (*.*)",
        )
        if not path:
            return
        Path(path).write_text(text, encoding="utf-8")


def _apply_fluent_theme() -> None:
    if setTheme and Theme:
        try:
            setTheme(Theme.AUTO)
        except Exception:
            pass


def _load_app_icon() -> QIcon | None:
    assets_dir = Path(__file__).resolve().parents[1] / "assets"
    candidates = [
        assets_dir / "logo.png",
        assets_dir / "logo.jpg",
        assets_dir / "logo.jpeg",
        assets_dir / "logo.ico",
        assets_dir / "logo.svg",
    ]
    for icon_path in candidates:
        if not icon_path.exists() or not icon_path.is_file():
            continue
        try:
            data = icon_path.read_bytes()
        except Exception:
            continue

        pixmap = QPixmap()
        if pixmap.loadFromData(data):
            return QIcon(pixmap)

        if icon_path.suffix.lower() == ".svg":
            head = data[:512].lstrip()
            if head.startswith(b"<svg") or b"<svg" in head:
                icon = QIcon(str(icon_path))
                if not icon.isNull():
                    return icon
    return None


def _python_console_executable() -> str:
    executable = Path(sys.executable)
    if executable.name.lower() == "pythonw.exe":
        python_exe = executable.with_name("python.exe")
        if python_exe.exists():
            return str(python_exe)
    return sys.executable


def main() -> None:
    app = QApplication.instance()
    owns_app = False
    if app is None:
        app = QApplication(sys.argv)
        owns_app = True

    _apply_fluent_theme()

    window = FluentMedia2TextWindow()
    icon = _load_app_icon()
    if icon is not None:
        app.setWindowIcon(icon)
        window.setWindowIcon(icon)
    window.show()

    if owns_app:
        app.exec()


if __name__ == "__main__":
    main()
