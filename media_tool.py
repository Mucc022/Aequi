from __future__ import annotations

import sys
import traceback

from media2text.cli import main as cli_main


def _run_gui_tk() -> int:
    from media2text.gui import main as gui_main

    gui_main()
    return 0


def _run_gui_fluent() -> int:
    from media2text.gui_fluent import main as gui_fluent_main

    gui_fluent_main()
    return 0


def _run_gui_auto() -> int:
    try:
        return _run_gui_fluent()
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] Fluent GUI unavailable, fallback to Tk GUI: {exc}", file=sys.stderr)
        print("[WARN] Traceback for Fluent GUI failure:", file=sys.stderr)
        print(traceback.format_exc(), file=sys.stderr)
        return _run_gui_tk()


def main() -> int:
    if len(sys.argv) <= 1:
        return _run_gui_auto()

    mode = sys.argv[1].strip().lower()
    if mode in {"gui", "gui-auto"}:
        return _run_gui_auto()
    if mode == "gui-fluent":
        return _run_gui_fluent()
    if mode in {"gui-tk", "gui-legacy"}:
        return _run_gui_tk()

    return cli_main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
