# Aequora

Aequora 是一个面向学习资料整理的本地工具。它可以从本地音频/视频/PDF 文件、文件夹或网页链接中提取资料内容，并按一次任务生成音频、字幕、文本、PDF 文档和后台记录。

当前版本以 Windows 桌面 GUI 为主，CLI 仍保留给开发调试和批量处理使用。

## 功能范围

- 本地音频/视频文件处理。
- 本地 PDF 文件归档。
- 文件夹批量扫描。
- 网页链接候选媒体 / PDF 发现。
- PDF 直链下载。
- 通过平台字幕或 Whisper 生成文本和字幕。
- 可选导出处理后的音频文件。
- 任务 manifest、失败日志、运行 ledger 记录。
- GUI 停止任务后可选择回退当前任务或本次运行产物。

## 环境准备

建议在项目目录内使用本地虚拟环境：

```powershell
cd "C:\Mark\Softwares\Productivity\Coding\PracticeProject\Python\Aequora"
python -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install -r requirements.txt
```

安装 FFmpeg：

```powershell
winget install Gyan.FFmpeg
```

确认 FFmpeg 可用：

```powershell
ffmpeg -version
```

## 启动 GUI

普通使用直接双击：

```text
start.bat
```

该入口会执行 `pythonw run_gui.pyw`，只显示 GUI，不显示控制台窗口。GUI 启动日志写入：

```text
logs/gui.log
```

如果双击没有反应，先用调试方式启动：

```powershell
.\.venv\Scripts\python.exe run_gui.py
```

也可以直接指定 GUI：

```powershell
.\.venv\Scripts\python.exe media_tool.py gui-fluent
.\.venv\Scripts\python.exe media_tool.py gui-tk
```

## 创建桌面快捷方式

项目内提供快捷方式生成脚本：

```powershell
.\.venv\Scripts\python.exe create_shortcut.py
```

脚本会在桌面创建 `Aequora.lnk`，目标指向本项目的 `run_gui.pyw`，图标使用 `assets/logo.ico`。

## CLI 用法

CLI 入口由 `media_tool.py` 提供。

处理本地文件或文件夹：

```powershell
.\.venv\Scripts\python.exe media_tool.py run --input "C:\Videos" --out "outputs"
```

处理本地 PDF 或 PDF 链接：

```powershell
.\.venv\Scripts\python.exe media_tool.py run --input "C:\Docs\paper.pdf" --out "outputs" --subtitle-priority skip_text --text-output none --no-export-audio
.\.venv\Scripts\python.exe media_tool.py run --input "https://example.com/paper.pdf" --out "outputs" --subtitle-priority skip_text --text-output none --no-export-audio
```

处理网页链接：

```powershell
.\.venv\Scripts\python.exe media_tool.py run --input "https://example.com/video-page" --out "outputs"
```

失败任务重试：

```powershell
.\.venv\Scripts\python.exe media_tool.py retry-failed --failed-log "outputs\后台数据\failed_tasks.jsonl" --out "outputs"
```

生成关键截图提示词：

```powershell
.\.venv\Scripts\python.exe media_tool.py snapshot-make-prompt --video "C:\Videos\demo.mp4" --srt "C:\Videos\demo.srt" --out "C:\Videos\demo_prompt.txt"
```

按 AI 输出截图：

```powershell
.\.venv\Scripts\python.exe media_tool.py snapshot-capture --video "C:\Videos\demo.mp4" --ai-output "C:\Videos\demo_ai_output.txt"
```

## 配置

默认配置文件是：

```text
config.json
```

常用字段：

- `output_root`: 默认输出目录。
- `subtitle_priority`: 字幕/转写策略，支持 `subtitle_first_then_whisper`、`platform_only`、`whisper_only`、`skip_text`。
- `whisper.model`: Whisper 模型，例如 `medium`、`large-v3`。
- `whisper.language`: 识别语言，例如 `zh`、`en`、`auto`。
- `download.keep_original`: 是否保留原始媒体。
- `download.export_audio`: 是否额外导出音频。
- `scraping.candidate_mode`: 网页候选选择模式，支持 `select`、`auto`。

## 输出结构

默认输出目录：

```text
outputs/
├─ 结果/
│  ├─ *.mp3 / *.wav
│  ├─ *.txt
│  ├─ *.srt
│  └─ *.pdf
└─ 后台数据/
   ├─ manifest.jsonl
   ├─ failed_tasks.jsonl
   ├─ downloaded.txt
   ├─ metadata/
   ├─ cache/
   └─ runs/
      └─ <run_id>/
         └─ session_ledger.jsonl
```

文件名通常包含序号、日期、标题和短哈希，便于避免重名。

## 项目结构

```text
media_tool.py              CLI 和旧 GUI 自动入口
run_gui.py                 当前 GUI 启动入口，带日志重定向
run_gui.pyw                无控制台 GUI 入口
start.bat                  双击启动脚本
create_shortcut.py         桌面快捷方式生成脚本
config.json                默认配置
media2text/
  cli.py                   CLI 参数和任务分派
  gui_fluent.py            PySide6 GUI
  gui.py                   旧 Tk GUI fallback
  orchestrator.py          核心任务执行器
  ytdlp_pipeline.py        yt-dlp 下载和元数据处理
  transcriber.py           faster-whisper 转写
  ffmpeg_utils.py          FFmpeg 检测和音频处理
  run_ledger.py            运行回退记录
  scraper_engine.py        网页候选发现
assets/                    图标资源
logs/                      GUI 启动日志
outputs/                   默认运行输出
_cleanup_backup/           旧脚本和打包脚本备份
_validation_work/          验证素材和临时验证工作区
```

## 开发检查

语法和导入级检查：

```powershell
.\.venv\Scripts\python.exe -m compileall -q media2text run_gui.py media_tool.py create_shortcut.py
```

查看 CLI 帮助：

```powershell
.\.venv\Scripts\python.exe media_tool.py --help
```

快速绕过 Whisper 的本地音频导出测试：

```powershell
.\.venv\Scripts\python.exe media_tool.py run `
  --input "_validation_work\site\sample.wav" `
  --out "_validation_work\smoke" `
  --config config.json `
  --subtitle-priority skip_text `
  --text-output none `
  --export-audio `
  --audio-format mp3
```

## 打包

旧打包脚本已移入 `_cleanup_backup/`。需要重新发布 EXE 时，可以先恢复或重写打包脚本，也可以直接从当前入口打包：

```powershell
.\.venv\Scripts\pyinstaller.exe --noconfirm --clean --onefile --windowed --name Aequora run_gui.pyw
```

如果希望保留 CLI，则另行打包 `media_tool.py`。

## Git 维护建议

当前仓库历史仍包含旧的 `Unified Media2Text` 阶段。如果这个项目已经确定改名为 Aequora，并且不需要保留旧项目历史，可以把当前工作区作为新的初始版本。

建议流程是先确认当前文件状态，再重建历史：

```powershell
git status --short --branch
git checkout --orphan main-fresh
git add .
git commit -m "feat: initialize Aequora"
git branch -D main
git branch -m main
```

如果远端也要完全替换，需要再执行强推：

```powershell
git push --force-with-lease origin main
```

注意：这会重写仓库历史。只有在确认旧提交不再需要、并且没有其他人依赖远端历史时再执行。
