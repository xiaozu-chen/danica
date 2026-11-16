"""
shot.py - Take a screenshot of a web page with headless Chromium (Playwright).

TWO HALVES in one file
  capture(...)   called by Danica. It starts THIS FILE as a separate Python process (so a browser crash
                 or hang can never take Danica down, and it is killed after a timeout).
  _child(...)    runs in that process. Self-contained: it imports nothing from danica.

WHAT IT CAN OPEN
  * a FOLDER or an .html FILE in the workspace: the folder is served on a private 127.0.0.1 port, so
    built apps (Vite's dist/, which uses absolute /assets/ paths and ES modules) work. file:// would not.
  * an http://localhost:PORT/... or http://127.0.0.1:PORT/... address (a dev server you started).
  Nothing else: any request to a non-local host is blocked and reported, so a page can't call out.

IT ALSO REPORTS what went wrong inside the page: console errors, uncaught exceptions, failed requests.
Those lines are often more useful to a small model than the picture.

SETUP (once):  pip install playwright   then   playwright install chromium
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}
MAX_LOGS = 8
MAX_PAGE_HEIGHT = 6000          # a full-page picture is cut here (huge pictures cost context)
SETUP_HINT = "run: pip install playwright   then: playwright install chromium"


# ------------------------------------------------------------------ the half Danica calls

def capture(target: str, out: Path, width: int = 1280, height: int = 800, full_page: bool = False,
            wait_ms: int = 400, timeout: int = 45) -> dict:
    """{"ok": True, "width", "height", "logs": [...]} or {"ok": False, "error": "..."}. Never raises."""
    args = {"target": target, "out": str(out), "width": width, "height": height,
            "full_page": full_page, "wait_ms": wait_ms}
    try:
        done = subprocess.run([sys.executable, "-I", str(Path(__file__).resolve()), json.dumps(args)],
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"the browser did not finish within {timeout}s (a page that never finishes loading?)"}
    except OSError as e:
        return {"ok": False, "error": f"could not start the browser helper: {e}"}
    try:
        return json.loads(done.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"ok": False, "error": "the browser helper failed: " + (done.stderr.strip().splitlines() or ["no output"])[-1][:200]}


# ------------------------------------------------------------------ the half in the other process

def _child(args: dict) -> dict:
    import functools
    import http.server
    import threading
    from urllib.parse import urlparse

    try:
        from playwright.sync_api import Error as PWError, sync_playwright
    except ImportError:
        return {"ok": False, "error": "Playwright is not installed. " + SETUP_HINT}

    target = args["target"]
    server = None
    if target.startswith(("http://", "https://")):
        url = target
    else:
        path = Path(target)
        folder, name = (path, "") if path.is_dir() else (path.parent, path.name)
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(folder))
        handler.log_message = lambda *a, **k: None                   # type: ignore[attr-defined]
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{server.server_port}/{name}"

    logs: list[str] = []
    blocked: set[str] = set()

    def note(line: str) -> None:
        line = " ".join(line.split())[:220]
        if line not in logs:
            logs.append(line)

    def gate(route):                                                    # only local requests may leave the page
        parts = urlparse(route.request.url)
        if parts.scheme in ("data", "blob", "about") or (parts.hostname or "") in {"localhost", "127.0.0.1", "::1"}:
            route.continue_()
        else:
            blocked.add(parts.hostname or parts.scheme)
            route.abort()

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": args["width"], "height": args["height"]})
            page.route("**/*", gate)
            page.on("console", lambda m: note(f"console {m.type}: {m.text}") if m.type in ("error", "warning") else None)
            page.on("pageerror", lambda e: note(f"uncaught error: {e}"))
            page.on("requestfailed", lambda r: note(f"failed to load {r.url.split('?')[0][-80:]}") if (urlparse(r.url).hostname or "") in {"localhost", "127.0.0.1", "::1"} else None)
            page.on("response", lambda r: note(f"HTTP {r.status} for {r.url.split('?')[0][-80:]}") if r.status >= 400 else None)
            page.goto(url, wait_until="load", timeout=20000)
            try:
                page.wait_for_load_state("networkidle", timeout=4000)   # scripts that fetch data: give them a moment
            except PWError:
                pass
            page.wait_for_timeout(int(args["wait_ms"]))
            shot = {"path": args["out"]}
            if args["full_page"]:
                shot["full_page"] = True
                if page.evaluate("document.documentElement.scrollHeight") > MAX_PAGE_HEIGHT:
                    shot["clip"] = {"x": 0, "y": 0, "width": args["width"], "height": MAX_PAGE_HEIGHT}
                    note(f"page is taller than {MAX_PAGE_HEIGHT}px: the picture is cut there")
            page.screenshot(**shot)
            size = page.evaluate("[document.documentElement.scrollWidth, document.documentElement.scrollHeight]")
            if size[0] > args["width"] + 2:
                note(f"the page is {size[0]}px wide in a {args['width']}px window: something overflows sideways")
            browser.close()
    except PWError as e:
        text = str(e)
        if "Executable doesn't exist" in text or "playwright install" in text:
            return {"ok": False, "error": "the Chromium browser is not installed. " + SETUP_HINT}
        return {"ok": False, "error": text.strip().splitlines()[0][:220]}
    finally:
        if server is not None:
            server.shutdown()
    if blocked:
        note("blocked requests to outside sites (not allowed): " + ", ".join(sorted(blocked)[:4]))
    return {"ok": True, "width": args["width"], "height": args["height"], "logs": logs[:MAX_LOGS]}


if __name__ == "__main__":
    try:
        result = _child(json.loads(sys.argv[1]))
    except Exception as exc:                                              # report, never traceback into the parent
        result = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:180]}"}
    print(json.dumps(result))
