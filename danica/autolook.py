"""
autolook.py - After a successful UI build, take a screenshot WITHOUT being asked and attach it for the model.

A small model forgets the "now look at it" step, so the harness does it: when run_shell has just finished a
`npm run build` (or vite build) with exit code 0 and the project has a built dist/index.html, the screenshot
tool is run once at desktop width. Its picture reaches the model on its next step (view_image's mechanism),
and its text (console errors, 404s, overflow) is appended to the build output.

Safety valves: needs vision on; at most MAX_PER_REQUEST per request (the agent resets the counter);
turn it off with  CONFIG["vision"]["auto_screenshot"] = false.
"""

from __future__ import annotations

import re

from danica import vision
from danica.config import CONFIG
from danica.tools.registry import run_tool
from danica.workspace import Workspace

MAX_PER_REQUEST = 3
OUT_DIRS = ("dist", "build")
BUILD_RX = re.compile(r"\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?build\b|\bvite\s+build\b", re.IGNORECASE)
# `cd app; npm run build`  /  `Set-Location app && npm run build`: the project folder the build ran in
CD_RX = re.compile(r"(?:^|[;&|]\s*)(?:cd|sl|set-location|push-location)\s+(?:-path\s+)?(['\"]?)([^'\";&|\n]+?)\1\s*(?=[;&|]|$)",
                   re.IGNORECASE)


def enabled() -> bool:
    """Return whether automatic screenshots are enabled and available."""
    return bool(CONFIG.get("vision", {}).get("auto_screenshot", True)) and vision.available()


def maybe(ws: Workspace, command: str, result: str, used: int) -> tuple[str, bool]:
    """(text to append to the shell result, whether a screenshot was taken). Never raises."""
    try:
        if used >= MAX_PER_REQUEST or not enabled():
            return "", False
        if not result.startswith("[exit code 0]") or not BUILD_RX.search(command):
            return "", False
        folder = ws.root
        match = CD_RX.search(command)
        if match:
            folder = ws.resolve(match.group(2).strip())          # stays inside the workspace (resolve enforces it)
        built = next((folder / name for name in OUT_DIRS if (folder / name / "index.html").is_file()), None)
        if built is None:
            return "", False
        text = run_tool("screenshot", {"target": ws.display(built), "out": f"shots/auto{used + 1}.png"}, ws)
        return "\n\n[auto screenshot after the build] " + text, not text.startswith("ERROR")
    except Exception:                                            # a convenience must never break the build result
        return "", False
