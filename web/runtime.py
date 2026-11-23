"""Launch the packaged web frontend and its local FastAPI sidecar."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys


def launch() -> None:
    import os
    tauri_bin = os.environ.get("DANICA_TAURI_BIN")
    if not tauri_bin and getattr(sys, "frozen", False):
        tauri_bin = str(Path(sys.executable).with_name("danica-web.exe"))
    if not tauri_bin:
        candidates = [
            Path(__file__).resolve().parent / "src-tauri" / "target" / "release" / "danica-web.exe",
            Path(__file__).resolve().parent / "src-tauri" / "target" / "debug" / "danica-web.exe",
        ]
        tauri_bin = next((str(path) for path in candidates if path.is_file()), None)
    if not tauri_bin or not Path(tauri_bin).is_file():
        raise RuntimeError(
            "The Tauri desktop app is not built. Run `cd web && npm install && npm run tauri build`."
        )
    if tauri_bin:
        shell_env = os.environ.copy()
        shell_env["DANICA_API_URL"] = "http://127.0.0.1:8765"
        subprocess.run([tauri_bin], env=shell_env, check=False)
