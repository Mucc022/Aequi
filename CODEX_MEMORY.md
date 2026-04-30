# Codex Memory Draft For Aequora

这个文件是给未来 Codex 或记忆系统使用的长期记忆草稿。当前会话不能直接写入系统 memory，所以把内容保存在仓库里。以后如果用户要“上传记忆”或“让新 Codex 接续”，可以直接引用这个文件。

## About The Project

- Project path: `C:\Mark\Softwares\Productivity\Coding\PracticeProject\Python\Aequora`.
- Repository remote: `https://github.com/Mucc022/Aequi.git`.
- Aequora is a Windows-first local desktop tool for organizing study media.
- It accepts local audio/video files, folders, and webpage links.
- It can produce audio exports, subtitles, text transcripts, manifests, failed-task logs, and run ledgers.
- The current product direction is GUI-first. CLI remains for development, debugging, and batch workflows.
- The current git history was intentionally reset. Treat `main` as the fresh Aequora history.

## Important Files

- `PROJECT_BLUEPRINT.md`: live project map, task board, and work log. Read before and update after every project task.
- `AGENTS.md`: operational handoff for Codex sessions.
- `README.md`: user-facing setup and usage guide.
- `start.bat`: double-click launcher for normal users.
- `run_gui.pyw`: no-console GUI entry.
- `run_gui.py`: GUI bootstrap, local venv bootstrap, and log redirection.
- `media_tool.py`: CLI and GUI dispatch entry.
- `media2text/gui_fluent.py`: current PySide6 GUI.
- `media2text/gui.py`: legacy Tk GUI fallback.
- `media2text/orchestrator.py`: core processing workflow.
- `config.json`: default runtime config.

## Rules For Future Codex Sessions

- Start every Aequora task by reading `AGENTS.md` and `PROJECT_BLUEPRINT.md`.
- End every Aequora task by updating `PROJECT_BLUEPRINT.md` when task status, project direction, or user decisions changed.
- Preserve the double-click Windows app experience.
- Do not allow silent GUI startup failures; keep logs and debug launch paths.
- Prefer practical implementation and verification over only describing plans.
- When the user gives vibe-coded natural language, translate it into concrete task entries before implementation.
- Keep documentation clear enough for a new Codex session to resume quickly.
- Be careful with Chinese text and UTF-8. In PowerShell, use `Get-Content -Encoding UTF8` for Markdown and Chinese source text.
- Do not revert unrelated user changes.
- Do not commit `.venv/`, `logs/`, `outputs/`, `*.lnk`, `_validation_work/out/`, `_validation_work/smoke/`, or `_cleanup_backup/`.

## User Preferences

- The user primarily works through vibe coding.
- The user wants direct, practical continuation, not just identification or advice.
- The user wants project state to be visible in plain natural language.
- The user wants files that help future Codex windows understand context quickly.
- For desktop utilities, the user values a polished launch path, useful diagnostics, and clear handoff instructions.
- The user prefers simple, clear Chinese explanations for project planning and status.

## Validation Commands

Run from the repo root:

```powershell
.\.venv\Scripts\python.exe -m compileall -q media2text run_gui.py media_tool.py create_shortcut.py
.\.venv\Scripts\python.exe media_tool.py --help
ffmpeg -version
```

Fast smoke test that avoids Whisper:

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

Clean `_validation_work\smoke` after the smoke test unless the user asks to keep it.

## Current Open Direction

The next likely work is product optimization. Good starting areas:

- Make startup and crash diagnostics clearer.
- Improve first-screen workflow clarity.
- Make results easier to open and understand.
- Improve failure explanations and retry guidance.
- Improve packaging/distribution when the app behavior is stable.
