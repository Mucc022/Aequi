from __future__ import annotations

import sys
from pathlib import Path


def _bootstrap_local_venv() -> None:
    root = Path(__file__).resolve().parent
    site_packages_root = root / ".venv" / "Lib" / "site-packages"
    scripts_dir = root / ".venv" / "Scripts"
    if site_packages_root.exists():
        sys.path.insert(0, str(site_packages_root))
    if scripts_dir.exists():
        import os

        os.environ["PATH"] = str(scripts_dir) + os.pathsep + os.environ.get("PATH", "")


def _redirect_stdio_to_log() -> None:
    log_dir = Path(__file__).resolve().parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "gui.log"
    log_file = log_path.open("a", encoding="utf-8", buffering=1)
    sys.stdout = log_file
    sys.stderr = log_file


def main() -> int:
    _bootstrap_local_venv()
    _redirect_stdio_to_log()
    from media2text.gui_fluent import main as gui_main

    gui_main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
