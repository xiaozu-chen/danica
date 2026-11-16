"""
screenshot.py - The screenshot tool: look at a page you built (see shot.py for how it works).

Pair it with view_image / vision: the picture is attached to the model's next step automatically
when the server has vision. Without vision the tool still reports console errors and failed loads.
"""

from urllib.parse import urlparse

from danica import shot, vision
from danica.tools.files import _write_guard
from danica.tools.registry import tool
from danica.workspace import Workspace


@tool(
    "screenshot",
    "Take a screenshot of a web page you built and see it (and any console errors). target is ONE of: a folder "
    "(for example dist, a built app), an .html file (for example site/index.html), or a dev-server address "
    "(http://localhost:5173). out is the picture to save, for example shots/home.png. width 320-1920 (390 = phone, "
    "1280 = desktop). Take one after you change the page, never two in a row without a change in between.",
    {
        "target": {"type": "string", "description": "A folder, an .html file, or http://localhost:PORT"},
        "out": {"type": "string", "description": "Where to save the picture, for example shots/home.png"},
        "width": {"type": "integer", "description": "Window width in pixels", "default": 1280},
        "full_page": {"type": "boolean", "description": "true = the whole scrolling page, not just the top", "default": False},
    },
)
def screenshot(ws: Workspace, target: str, out: str, width: int = 1280, full_page: bool = False) -> str:
    target = (target or "").strip()
    if target.startswith(("http://", "https://")):
        host = urlparse(target).hostname or ""
        if host not in ("localhost", "127.0.0.1", "::1"):
            return "ERROR: only local pages can be opened (localhost / 127.0.0.1). For a built app, give its folder."
        where = target
    else:
        p = ws.resolve(target)
        if not p.exists():
            return f"ERROR: '{target}' does not exist. Give a folder, an .html file, or http://localhost:PORT"
        if p.is_file() and p.suffix.lower() not in (".html", ".htm"):
            return f"ERROR: '{target}' is not an .html file"
        if p.is_dir() and not (p / "index.html").is_file():
            return f"ERROR: '{target}' has no index.html (did the build finish? run the build first)"
        where = str(p)
    path = ws.resolve(out)
    if path.suffix.lower() != ".png":
        return "ERROR: out must end in .png, for example shots/home.png"
    guarded = _write_guard(ws, path)
    if guarded:
        return guarded
    path.parent.mkdir(parents=True, exist_ok=True)
    width = max(320, min(int(width), 1920))
    result = shot.capture(where, path, width=width, full_page=bool(full_page))
    if not result.get("ok"):
        return f"ERROR: {result.get('error', 'screenshot failed')}"
    shown = ws.display(path)
    if vision.available():
        queue = getattr(ws, "pending_images", None)
        if queue is None:
            queue = ws.pending_images = []
        if shown not in queue:
            queue.append(shown)
        seen = f"The picture is attached to your next step: look at it, list what is wrong, and fix the biggest problems. "
    else:
        seen = "This model cannot see pictures, so use the messages below to judge the page. "
    logs = result.get("logs") or []
    issues = ("\nProblems found in the page:\n- " + "\n- ".join(logs)) if logs else "\nNo console errors or failed loads."
    return (f"OUTPUT: saved {shown} ({result['width']} px wide). {seen}Do not take another screenshot until you "
            f"have changed the page.{issues}")
