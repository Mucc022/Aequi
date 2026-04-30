from __future__ import annotations

from pathlib import Path

from .io_utils import append_jsonl, read_jsonl


def make_ledger_path(output_root: Path, run_id: str) -> Path:
    return output_root / "后台数据" / "runs" / run_id / "session_ledger.jsonl"


def log_ledger_event(ledger_path: Path, event: str, **payload) -> None:
    record = {"event": event, **payload}
    append_jsonl(ledger_path, record)


def _collect_artifacts(events: list[dict], scope: str) -> list[Path]:
    started_order: list[str] = []
    ended: set[str] = set()
    artifacts_by_task: dict[str, list[Path]] = {}
    all_artifacts: list[Path] = []

    for event in events:
        event_name = event.get("event")
        task_id = str(event.get("task_id") or "")

        if event_name == "task_start" and task_id:
            started_order.append(task_id)
        elif event_name == "task_end" and task_id:
            ended.add(task_id)
        elif event_name == "artifact" and task_id:
            file_path = str(event.get("path") or "").strip()
            if not file_path:
                continue
            p = Path(file_path)
            artifacts_by_task.setdefault(task_id, []).append(p)
            all_artifacts.append(p)

    if scope == "run":
        return all_artifacts

    active_task: str | None = None
    for tid in reversed(started_order):
        if tid not in ended:
            active_task = tid
            break

    if active_task is None and started_order:
        active_task = started_order[-1]

    if not active_task:
        return []

    return artifacts_by_task.get(active_task, [])


def rollback_from_ledger(output_root: Path, run_id: str, scope: str) -> dict[str, int]:
    ledger_path = make_ledger_path(output_root, run_id)
    if not ledger_path.exists():
        legacy_path = output_root / "_system" / ".runs" / run_id / "session_ledger.jsonl"
        if legacy_path.exists():
            ledger_path = legacy_path
    events = read_jsonl(ledger_path)
    artifacts = _collect_artifacts(events, scope=scope)

    output_root_resolved = output_root.resolve()

    deleted = 0
    skipped = 0
    for artifact in artifacts:
        try:
            resolved = artifact.resolve()
        except Exception:
            skipped += 1
            continue

        try:
            inside = resolved.is_relative_to(output_root_resolved)
        except AttributeError:
            inside = str(resolved).startswith(str(output_root_resolved))

        if not inside:
            skipped += 1
            continue

        if not resolved.exists() or not resolved.is_file():
            skipped += 1
            continue

        try:
            resolved.unlink()
            deleted += 1
            _cleanup_empty_parents(resolved.parent, stop_dir=output_root_resolved)
        except Exception:
            skipped += 1

    return {"deleted": deleted, "skipped": skipped}


def _cleanup_empty_parents(path: Path, stop_dir: Path) -> None:
    current = path
    while True:
        try:
            current_resolved = current.resolve()
        except Exception:
            return

        if current_resolved == stop_dir:
            return

        try:
            if any(current.iterdir()):
                return
            current.rmdir()
        except Exception:
            return

        current = current.parent
