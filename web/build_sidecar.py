"""Build the Python API executable using the Tauri target's external-bin suffix."""

from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BINARIES = ROOT / "src-tauri" / "binaries"


def main() -> None:
    python = os.environ.get("DANICA_PYTHON", sys.executable)
    target = os.environ.get("TAURI_ENV_TARGET_TRIPLE") or os.environ.get("HOST_TRIPLE")
    if not target:
        machine = platform.machine().lower()
        arch = {"amd64": "x86_64", "x64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}.get(machine, machine)
        if sys.platform == "win32":
            target = f"{arch}-pc-windows-msvc"
        elif sys.platform == "darwin":
            target = f"{arch}-apple-darwin"
        else:
            target = f"{arch}-unknown-linux-gnu"
    suffix = ".exe" if "windows" in target else ""
    output = BINARIES / f"danica-api-{target}{suffix}"
    BINARIES.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        python, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile",
        "--name", f"danica-api-{target}", "--distpath", str(BINARIES),
        "--workpath", str(ROOT / ".sidecar-build"), "--specpath", str(ROOT / ".sidecar-build"),
        "--paths", str(ROOT.parent), "--hidden-import", "web.api",
        "--hidden-import", "danica.config", "--collect-submodules", "danica",
        "--hidden-import", "fastapi",
        "--hidden-import", "uvicorn.logging", "--hidden-import", "uvicorn.loops.auto",
        "--hidden-import", "uvicorn.protocols.http.auto", "--hidden-import", "uvicorn.lifespan.on",
        "--add-data", f"{ROOT / 'dist'}{os.pathsep}web/dist",
        "--collect-all", "fastapi", "--collect-all", "uvicorn",
        str(ROOT / "sidecar.py"),
    ], cwd=ROOT.parent, check=True)
    if not output.exists():
        raise SystemExit(f"PyInstaller did not produce expected sidecar: {output}")


if __name__ == "__main__":
    main()
