"""
devserver.py - Start / stop / watch a project's dev server (Vite: `npm run dev`) without hanging Danica.

WHY A SEPARATE TOOL
  run_shell waits for a command to finish and kills it at the timeout, but a dev server never finishes.
  This module starts it in the BACKGROUND, waits until it prints its address, remembers it, and can stop it.

SAFETY (same ideas as shell.py)
  * The command is FIXED: `npm run dev -- --host 127.0.0.1` or `npm run preview -- --host 127.0.0.1`.
    The model chooses only a project folder inside the workspace and the word dev / preview.
  * Same sandbox as run_shell: a Windows Job Object (memory + process limits, whole tree killed on stop)
    and the same minimal environment. If the sandbox cannot start, nothing runs.
  * At most MAX_SERVERS at once, and everything is stopped when Danica exits.
  * Output goes to a log file in the app folder (a pipe nobody reads would fill up and freeze the server).
"""

from __future__ import annotations

import atexit
import os
import re
import signal
import subprocess
import tempfile
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

SCRIPTS = ("dev", "preview")
MAX_SERVERS = 2
START_TIMEOUT = 40            # seconds to wait for the address to appear AND answer
LOG_TAIL_LINES = 40
URL_RX = re.compile(r"https?://(?:localhost|127\.0\.0\.1|\[::1\]):\d+[^\s\x1b]*")
ANSI_RX = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


class DevServerError(Exception):
    """A problem worded for the model."""


@dataclass
class Server:
    id: str
    folder: Path
    script: str
    log: Path
    proc: subprocess.Popen
    job: object | None
    url: str = ""
    started: float = field(default_factory=time.time)


SERVERS: dict[str, Server] = {}


# ------------------------------------------------------------------ how a process is started and stopped

def _argv(script: str) -> list[str]:
    """The ONE command that is ever run (script is checked against SCRIPTS first)."""
    return ["powershell", "-NoProfile", "-NonInteractive", "-Command",
            f"npm run {script} -- --host 127.0.0.1"]


def _env() -> dict[str, str]:
    from danica.tools.shell import _clean_env              # the same minimal environment run_shell uses
    env = _clean_env()
    env.update({"NO_COLOR": "1", "FORCE_COLOR": "0", "BROWSER": "none", "CI": "1"})   # no colour codes, never open a browser
    return env


def _spawn(argv: list[str], cwd: Path, log_file) -> tuple[subprocess.Popen, object | None]:
    """Start confined. Windows: inside a Job Object (fails closed). Elsewhere (tests / Linux): its own process group."""
    kwargs = dict(cwd=cwd, env=_env(), stdin=subprocess.DEVNULL, stdout=log_file, stderr=subprocess.STDOUT)
    if os.name == "nt":
        from danica.config import CONFIG
        from danica.sandbox import Job, SandboxError
        limits = CONFIG["sandbox"]
        try:
            job = Job(memory_mb=limits["memory_mb"], max_processes=limits["max_processes"])
            proc = job.popen(argv, creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0), **kwargs)
        except SandboxError as exc:
            raise DevServerError(f"the sandbox could not start, so nothing was run: {exc}") from None
        return proc, job
    return subprocess.Popen(argv, start_new_session=True, **kwargs), None


def _kill(server: Server) -> None:
    if server.job is not None:
        server.job.kill()
        server.job.close()
    elif server.proc.poll() is None:
        try:
            os.killpg(os.getpgid(server.proc.pid), signal.SIGTERM)
        except (OSError, ProcessLookupError):
            server.proc.terminate()
    try:
        server.proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        server.proc.kill()


def stop_all() -> None:
    for server in list(SERVERS.values()):
        try:
            _kill(server)
        except Exception:
            pass
    SERVERS.clear()


atexit.register(stop_all)                  # never leave a server running after Danica closes


# ------------------------------------------------------------------ helpers

def _read_log(server: Server) -> str:
    try:
        return ANSI_RX.sub("", server.log.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return ""


def _tail(text: str, lines: int = LOG_TAIL_LINES) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])[-3000:]


def _answers(url: str) -> bool:
    """True if something answers HTTP at url (any status): the server is really ready, not just printing."""
    try:
        urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url, timeout=2).close()
        return True
    except urllib.error.HTTPError:
        return True
    except Exception:
        return False


def _alive(server: Server) -> bool:
    return server.proc.poll() is None


# ------------------------------------------------------------------ the four actions

def start(folder: Path, script: str = "dev") -> Server:
    if script not in SCRIPTS:
        raise DevServerError(f"script must be one of: {', '.join(SCRIPTS)}")
    if not (folder / "package.json").is_file():
        raise DevServerError(f"'{folder.name}' has no package.json: give the project folder (the one with package.json)")
    for server in SERVERS.values():                          # already running for this project? reuse it
        if server.folder == folder and server.script == script and _alive(server):
            return server
    for sid in [k for k, v in SERVERS.items() if not _alive(v)]:
        del SERVERS[sid]                                     # forget servers that died on their own
    if len(SERVERS) >= MAX_SERVERS:
        raise DevServerError(f"{MAX_SERVERS} dev servers are already running ({', '.join(SERVERS)}): stop one first")

    run_dir = Path(os.environ.get("APPDATA") or tempfile.gettempdir()) / "Danica" / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    sid = next(f"dev{n}" for n in range(1, 10) if f"dev{n}" not in SERVERS)
    log = run_dir / f"{sid}.log"
    with open(log, "wb") as log_file:
        proc, job = _spawn(_argv(script), folder, log_file)
    server = Server(sid, folder, script, log, proc, job)
    SERVERS[sid] = server

    deadline = time.time() + START_TIMEOUT
    while time.time() < deadline:
        if not _alive(server):
            text = _read_log(server)
            del SERVERS[sid]
            raise DevServerError("the dev server stopped right after starting. Last output:\n" + (_tail(text) or "(none)"))
        match = URL_RX.search(_read_log(server))
        if match and _answers(match.group(0)):
            server.url = match.group(0).rstrip("/") + "/"
            return server
        time.sleep(0.3)
    text = _read_log(server)
    _kill(server)
    del SERVERS[sid]
    raise DevServerError(f"no address appeared within {START_TIMEOUT}s. Last output:\n" + (_tail(text) or "(none)"))


def stop(sid: str = "") -> list[str]:
    """Stop one server (by id) or all of them. Returns the ids stopped."""
    targets = [sid] if sid else list(SERVERS)
    stopped = []
    for key in targets:
        server = SERVERS.pop(key, None)
        if server is None:
            if sid:
                raise DevServerError(f"no dev server called '{sid}' (running: {', '.join(SERVERS) or 'none'})")
            continue
        _kill(server)
        stopped.append(key)
    return stopped


def describe() -> list[str]:
    lines = []
    for server in SERVERS.values():
        state = f"running at {server.url}" if _alive(server) else "STOPPED (it exited by itself)"
        lines.append(f"{server.id}: {server.folder.name} ({server.script}) {state}, up {int(time.time() - server.started)}s")
    return lines


def logs(sid: str) -> str:
    server = SERVERS.get(sid)
    if server is None:
        raise DevServerError(f"no dev server called '{sid}' (running: {', '.join(SERVERS) or 'none'})")
    return _tail(_read_log(server)) or "(no output yet)"
