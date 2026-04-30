# Codex Handoff: Aequora

This file is for future Codex sessions working in this repository. Read it before making changes.

## Project Snapshot

Aequora is a Windows-first local desktop tool for organizing study media. It processes local audio/video files, folders, or webpage links and can produce audio exports, subtitles, text transcripts, manifests, failed-task logs, and run ledgers.

The user primarily works through vibe coding and expects the agent to inspect the actual repo, make practical changes, verify them, and leave clear launch/debug paths.

## Current Repository State

- The git history was intentionally reset. The current `main` branch starts at `feat: initialize Aequora`.
- Old `Unified Media2Text` history, old migration branch, and old tags were removed from the remote.
- Treat this repo as a fresh Aequora project, not as a continuation of the old history.
- The remote is `origin` at `https://github.com/Mucc022/Aequi.git`.

## Main Entry Points

- `start.bat`: user-facing double-click launcher.
- `run_gui.pyw`: no-console GUI entry.
- `run_gui.py`: GUI bootstrap with local `.venv` path setup and log redirection to `logs/gui.log`.
- `media_tool.py`: CLI entry and GUI dispatch fallback.
- `media2text/gui_fluent.py`: current PySide6 GUI.
- `media2text/gui.py`: legacy Tk GUI fallback.
- `media2text/cli.py`: CLI parser and command routing.
- `media2text/orchestrator.py`: core task execution.
- `config.json`: default runtime configuration.
- `README.md`: user-facing setup, launch, CLI, output, and maintenance notes.

## User Preferences For This Project

- Prefer a polished double-click Windows app experience, not only CLI instructions.
- Silent GUI startup failure is unacceptable; preserve logging and debug launch paths.
- Keep README and handoff docs clear enough for a new Codex window to resume quickly.
- When adding launch or packaging behavior, include diagnostics and recovery paths.
- Be careful with Chinese text and UTF-8. Use `Get-Content -Encoding UTF8` when reading Markdown or source containing Chinese.
- Do not revert unrelated local changes. Inspect first, then work with the current tree.

## Validation Commands

Use the local venv when available:

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

Remove `_validation_work\smoke` after using it unless the user asks to keep outputs.

## Files And Directories To Treat Carefully

- `.venv/`, `logs/`, `outputs/`, `outputs-*`, `*.lnk`, `_validation_work/out/`, `_validation_work/smoke/`, and `_cleanup_backup/` are ignored and normally should not be committed.
- `_validation_work/site/` contains small validation fixtures and is intentionally committed.
- `assets/` contains app icon assets and should remain committed.
- `_cleanup_backup/` holds old scripts locally during consolidation but is not part of the clean initial repo.

## Practical Startup Debugging

If the user says the app does not open:

1. Check `logs/gui.log`.
2. Run `.\.venv\Scripts\python.exe run_gui.py` from the repo root.
3. Check Python availability and background processes if needed.
4. Prefer fixing silent failure paths over only documenting a command.

## Packaging Notes

The current README suggests:

```powershell
.\.venv\Scripts\pyinstaller.exe --noconfirm --clean --onefile --windowed --name Aequora run_gui.pyw
```

If packaging becomes active work, verify whether `run_gui.pyw` or a dedicated spec file is the better target before finalizing.
