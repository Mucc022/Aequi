from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


APP_NAME = "Aequora"


def _project_root() -> Path:
    return Path(__file__).resolve().parent


def _desktop_dir() -> Path:
    user_profile = os.environ.get("USERPROFILE")
    if not user_profile:
        raise RuntimeError("USERPROFILE is not set; cannot locate Desktop.")

    desktop = Path(user_profile) / "Desktop"
    if desktop.exists():
        return desktop

    onedrive = os.environ.get("OneDrive") or os.environ.get("OneDriveConsumer")
    if onedrive:
        onedrive_desktop = Path(onedrive) / "Desktop"
        if onedrive_desktop.exists():
            return onedrive_desktop

    desktop.mkdir(parents=True, exist_ok=True)
    return desktop


def _make_logo_ico(png_path: Path, ico_path: Path) -> None:
    if ico_path.exists():
        return
    if not png_path.exists():
        raise RuntimeError(f"Missing logo source: {png_path}")

    try:
        from PIL import Image

        image = Image.open(png_path)
        image.save(ico_path, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
        return
    except Exception:
        pass

    try:
        from PySide6.QtGui import QImage

        image = QImage(str(png_path))
        if not image.isNull() and image.save(str(ico_path), "ICO"):
            return
    except Exception:
        pass

    raise RuntimeError(
        "Could not generate assets/logo.ico automatically. "
        "Install Pillow or convert assets/logo.png to assets/logo.ico manually."
    )


def _powershell_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _create_shortcut(shortcut_path: Path, target_path: Path, work_dir: Path, icon_path: Path) -> None:
    script = "\n".join(
        [
            "$shell = New-Object -ComObject WScript.Shell",
            f"$shortcut = $shell.CreateShortcut({_powershell_quote(str(shortcut_path))})",
            f"$shortcut.TargetPath = {_powershell_quote(str(target_path))}",
            f"$shortcut.WorkingDirectory = {_powershell_quote(str(work_dir))}",
            f"$shortcut.IconLocation = {_powershell_quote(str(icon_path))}",
            "$shortcut.Save()",
        ]
    )

    completed = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "Failed to create shortcut.")


def main() -> int:
    root = _project_root()
    target_path = root / "run_gui.pyw"
    if not target_path.exists():
        raise RuntimeError(f"Missing GUI entry: {target_path}")

    assets_dir = root / "assets"
    png_path = assets_dir / "logo.png"
    ico_path = assets_dir / "logo.ico"
    _make_logo_ico(png_path=png_path, ico_path=ico_path)

    shortcut_path = _desktop_dir() / f"{APP_NAME}.lnk"
    _create_shortcut(
        shortcut_path=shortcut_path,
        target_path=target_path,
        work_dir=root,
        icon_path=ico_path,
    )

    print(f"Shortcut created: {shortcut_path}")
    print(f"Target: {target_path}")
    print(f"Working directory: {root}")
    print(f"Icon: {ico_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
