"""Launch the packaged web frontend and its local FastAPI sidecar."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys


def launch() -> None:
    import os
    tauri_bin = os.environ.get("DANICA_TAURI_BIN")
    if tauri_bin:
        shell_env = os.environ.copy()
        shell_env["DANICA_API_URL"] = "http://127.0.0.1:8765"
        subprocess.run([tauri_bin], env=shell_env, check=False)
    elif getattr(sys, "frozen", False):
        shell = Path(sys.executable).with_name("danica-web.exe")
        if not shell.is_file():
            raise RuntimeError(f"Tauri desktop shell was not found: {shell}")
        shell_env = os.environ.copy()
        shell_env["DANICA_API_URL"] = "http://127.0.0.1:8765"
        subprocess.run([str(shell)], env=shell_env, check=False)
    else:
        # Source installs launch Tauri's dev shell, which also starts Vite and the API sidecar.
        web_dir = Path(__file__).resolve().parent
        shell_env = os.environ.copy()
        shell_env.setdefault("DANICA_PYTHON", sys.executable)
        shell_env["DANICA_API_URL"] = "http://127.0.0.1:8765"
        npm = "npm.cmd" if sys.platform == "win32" else "npm"
        subprocess.run([npm, "run", "tauri", "dev"], cwd=web_dir, env=shell_env, check=False)
