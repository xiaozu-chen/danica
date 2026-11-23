"""Bounded subprocess supervisor for isolated, summary-only subagents."""

import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TYPE_CHECKING

from danica.audit import AuditLog
from danica.frozen import module_cmd
from danica.workspace import Workspace

if TYPE_CHECKING:
    from danica.permissions import PermissionPolicy

MAX_CHILDREN = 5
MAX_ACTIVE = 1
MAX_DEPTH = 1
MAX_TASK_CHARS = 12_000_000
MAX_RESULT_BYTES = 32_000_000
MAX_SUMMARY_CHARS = 8_000_000

# agent events worth showing to a human watching a subagent.
# tool_start/tool_delta are skipped: they fire for every streamed token.
WATCHED_EVENTS = ("content", "thinking", "tool_call", "tool_result", "notice", "citations") 


@dataclass
class Child:
    """Track one delegated worker and its current state."""

    id: str
    task: str
    process: subprocess.Popen | None
    status: str
    started_at: float
    result: str = ""
    listener: socket.socket | None = None
    thread: threading.Thread | None = None


class SubagentError(Exception):
    """A rejected task or an explicit child-process/IPC failure."""


def _one_line(value, limit: int) -> str:
    """Flatten any value to one line of a most `limit` characters."""
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[:limit - 1] + "..."

def wire_event(kind: str, data: Any) -> dict[str, Any] | None:
    """Agent event -> small JSON-safe dict for the socket (None = nobody sees it.)"""
    if kind not in WATCHED_EVENTS:
        return None
    if kind == "tool_call":
        name, args = data
        return {"kind": kind, "data": [str(name), _one_line(args, 300)]}
    if kind == "tool_result":
        return {"kind": kind, "data": _one_line(data, 400)}
    return {"kind": kind, "data": str(data)[:4000]}

def describe_event(kind: str, data: Any) -> tuple[str, str] | None:
    """Wire event -> (rich style, one line). content/thinking stream, so the caller handles them."""
    if kind == "tool_call":
        name, summary = data
        return "bold cyan", f"▶ {name}  {summary}"
    if kind == "tool_result":
        return "dim", f"↳ {data}"
    if kind == "notice":
        return "yellow", f"! {_one_line(data, 300)}"
    if kind == "citations":
        return "dim", _one_line(data, 300)
    return None
    

def _read_line(conn: socket.socket, limit: int) -> bytes:
    data = bytearray()
    while len(data) <= limit:
        part = conn.recv(min(4096, limit + 1 - len(data)))
        if not part:
            break
        newline = part.find(b"\n")
        if newline >= 0:
            data.extend(part[:newline])
            break
        data.extend(part)
    if len(data) > limit:
        raise SubagentError("subagent protocol message exceeded its size limit")
    return bytes(data)


class SubagentSupervisor:
    """Start, monitor, and stop delegated agent workers."""

    def __init__(
        self,
        ws: Workspace,
        policy: "PermissionPolicy",
        audit: AuditLog | None = None,
        session_id: str = "",
        *,
        timeout_seconds: int = 300,
        max_turns: int = 8,
        new_console: bool = True,
        on_event: Callable[[str, str, object], None] | None = None,
    ):
        self.ws = ws
        self.policy = policy
        self.audit = audit
        self.session_id = session_id
        self.timeout_seconds = timeout_seconds
        self.max_turns = max_turns
        self.new_console = new_console
        self.children: list[Child] = []
        self._lock = threading.Lock()
        self._threads: dict[str, threading.Thread] = {}
        # on_event(child_id, kind, data): set by a UI that subagents itself (the TUI panel).
        # While set, no separate console opens. Called from worker threads: only hand events over.
        self.on_event = on_event

    def _log(self, event: str, **data) -> None:
        if self.audit:
            self.audit.append(event, **data)

    def _emit(self, child_id: str, kind: str, data) -> None:
        """Forward an event to the UI; a broken callback must never break the subagent."""
        if self.on_event is None:
            return
        try:
            self.on_event(child_id, kind, data)
        except Exception:
            pass

    def run(self, task: str, depth: int = 0, *,
            _ready: threading.Event | None = None,
            _ready_child: list[str] | None = None) -> str:
        """Run a delegated task and return its final text result."""
        task = task.strip()
        if not task:
            raise SubagentError("task must not be empty")
        if len(task) > MAX_TASK_CHARS:
            raise SubagentError(f"task exceeds {MAX_TASK_CHARS} characters")
        if depth >= MAX_DEPTH:
            raise SubagentError("subagents cannot spawn subagents")
        with self._lock:
            if len(self.children) >= MAX_CHILDREN:
                raise SubagentError(f"session limit reached ({MAX_CHILDREN} total subagents)")
            if sum(c.status in ("starting", "running") for c in self.children) >= MAX_ACTIVE:
                raise SubagentError("another subagent is already running")
            child_id = f"{len(self.children) + 1}-{secrets.token_hex(4)}"
            child = Child(child_id, task, None, "starting", time.monotonic())
            self.children.append(child)

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            listener.settimeout(min(self.timeout_seconds, 15))
            child.listener = listener
            token = secrets.token_urlsafe(32)
            command = [
                *module_cmd("danica.subagent_worker"),      # packaged exe: Danica.exe --run-module ... (see frozen.py)
                "--host", "127.0.0.1", "--port", str(listener.getsockname()[1]),
                "--token", token,
            ]
            # A separate console is only the FALLBACK, when nothing here shows the subagent itself
            use_console = self.new_console and os.name == "nt" and self.on_event is None
            if use_console:
                command.append("--console")       # worker prints progress and pauses at the end
            # BUG FIX: stdout/stderr were DEVNULL even for the new console -> blank window.
            # None = inherit, i.e. write to the console that was just created.
            quiet = None if use_console else subprocess.DEVNULL
            process = subprocess.Popen(
                command,
                cwd=str(self.ws.root),
                stdin=subprocess.DEVNULL,
                stdout=quiet,
                stderr=quiet,
                creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0) if use_console else 0,
            )
            child.process = process
            child.status = "running"
            if _ready:
                if _ready_child is not None:
                    _ready_child.append(child_id)
                _ready.set()
            self._log("subagent_start", child=child_id, depth=depth,
                      task_chars=len(task), workspace=str(self.ws.root))
            self._emit(child_id, "start", task)

            try:
                conn, _ = listener.accept()
            except socket.timeout as exc:
                raise SubagentError("subagent did not connect before the startup timeout") from exc
            with conn:
                conn.settimeout(self.timeout_seconds)
                payload = {
                    "token": token,
                    "task": task,
                    "workspace": str(self.ws.root),
                    "child_id": child_id,
                    "session_id": self.session_id,
                    "max_turns": self.max_turns,
                    "permission": {
                        "project_mode": self.policy.project_mode,
                        # Open Mode is inherited, never widened: same start folder, same protected areas
                        "open_root": str(self.policy.open_root) if self.policy.open_root else "",
                        "full_access": self.policy.full_access,
                        "session_allowed": sorted(self.policy.session_allowed),
                    },
                    "taint_sources": list(getattr(self.ws.web, "taint_sources", []))[:20],
                    "known_hosts": sorted(getattr(self.ws.web, "known_hosts", set()))[:100],
                    "audit_path": str(self.audit.path) if self.audit else "",
                }
                conn.sendall(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")
                # The worker now sends MANY lines: progress events, then ONE final result line.
                reader = conn.makefile("rb")
                result = None
                while True:
                    line = reader.readline(MAX_RESULT_BYTES + 1)
                    if not line:
                        break                                  # worker closed without a result
                    if len(line) > MAX_RESULT_BYTES:
                        raise SubagentError("subagent protocol message exceeded its size limit")
                    message = json.loads(line.decode("utf-8"))
                    if not isinstance(message, dict) or message.get("token") != token:
                        raise SubagentError("subagent returned an invalid protocol response")
                    if message.get("type") == "event":         # progress: show it, keep reading
                        self._emit(child_id, str(message.get("kind", "")), message.get("data"))
                        continue
                    result = message                            # anything else is the final result
                    break
            if result is None:
                raise SubagentError("subagent closed IPC without returning a result")
            if result.get("status") != "completed":
                raise SubagentError(str(result.get("error") or "subagent failed"))
            summary = result.get("summary")
            if not isinstance(summary, str) or not summary.strip():
                raise SubagentError("subagent returned an empty summary")
            if len(summary) > MAX_SUMMARY_CHARS:
                summary = summary[:MAX_SUMMARY_CHARS] + "\n[summary truncated]"
            files = result.get("changed_files", [])
            if not isinstance(files, list) or not all(
                isinstance(item, str) for item in files
            ):
                raise SubagentError("subagent returned invalid changed-file metadata")
            child.status = "completed"
            child.result = summary
            self._log("subagent_complete", child=child_id, summary_chars=len(summary),
                      changed_files=files[:50])
            return json.dumps({"summary": summary, "changed_files": files[:50]},
                              ensure_ascii=False)
        except (
            OSError,
            socket.timeout,
            json.JSONDecodeError,
            UnicodeDecodeError,
        ) as exc:
            if child.status != "killed":
                child.status = "failed"
                child.result = f"{type(exc).__name__}: {exc}"[:500]
                self._log("subagent_failed", child=child_id, error=type(exc).__name__)
            raise SubagentError(f"subagent communication failed: {exc}") from exc
        except SubagentError as exc:
            if child.status != "killed":
                child.status = "failed"
                child.result = str(exc)[:500]
                self._log("subagent_failed", child=child_id, error=str(exc)[:200])
            raise
        except KeyboardInterrupt:
            if child.process and child.process.poll() is None:
                child.process.kill()
            child.status = "killed"
            self._log("subagent_kill", child=child_id, reason="keyboard_interrupt")
            raise
        finally:
            listener.close()
            child.listener = None
            process = child.process
            if process is not None:
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)

    def start(self, task: str) -> str:
        """Start a delegated task and return its worker identifier."""
        ready = threading.Event()
        finished = threading.Event()
        failure: list[SubagentError] = []
        ready_child: list[str] = []

        def execute() -> None:
            try:
                self.run(task, _ready=ready, _ready_child=ready_child)
            except SubagentError as exc:
                failure.append(exc)
            finally:
                finished.set()

        thread = threading.Thread(target=execute, name="danica-subagent", daemon=True)
        thread.start()
        if not ready.wait(timeout=20):
            if finished.is_set() and failure:
                raise failure[0]
            raise SubagentError("subagent did not start before the startup timeout")
        with self._lock:
            child = next((item for item in self.children
                          if item.id == ready_child[0] and item.thread is None), None)
            if child is None:
                raise SubagentError("subagent started but could not be added to the dashboard")
            child.thread = thread
            self._threads[child.id] = thread
            return child.id

    def wait(self, child_id: str, timeout: float | None = None) -> bool:
        """Wait for a worker to finish, returning whether it completed."""
        thread = self._threads.get(child_id)
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def close(self) -> None:
        """Stop all running delegated workers."""
        for child in list(self.children):
            if child.status in ("starting", "running"):
                self.kill(child.id)
        for thread in list(self._threads.values()):
            thread.join(timeout=5)
            if thread.is_alive():
                raise SubagentError(f"subagent worker {thread.name} did not stop")

    def list(self) -> list[dict]:
        """Return each child worker's state and a short result summary."""
        for child in self.children:
            if child.process and child.status == "running" and child.process.poll() is not None:
                child.status = "failed"
        return [{"id": c.id, "status": c.status,
                 "elapsed_seconds": round(time.monotonic() - c.started_at, 1),
                 "task": c.task[:200], "summary": c.result[:500]}
                for c in self.children]

    def kill(self, child_id: str) -> bool:
        """Stop a worker by identifier and report whether it was found."""
        with self._lock:
            for child in self.children:
                if child.id == child_id and child.status == "running" and child.process:
                    child.process.kill()
                    child.status = "killed"
                    if child.listener:
                        try:
                            child.listener.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass
                        child.listener.close()
                        child.listener = None
                    self._log("subagent_kill", child=child_id)
                    return True
        return False

def _print_event(live: dict, kind: str, data) -> None:
    """Write one event to a subagent's own console. Streamed text is joined into lines."""
    if kind in ("content", "thinking"):
        if kind == "thinking" and not live["open"]:
            return                                    # hide private reasoning
        print(data, end="", flush=True)
        live["open"] = not str(data).endswith("\n")
        return
    if live["open"]:                                  # finish a half-written text line first
        print(flush=True)
        live["open"] = False
    described = describe_event(kind, data)
    if described:
        print(described[1], flush=True)


def run_worker_connection(
    host: str, port: int, token: str, console: bool = False
) -> None:
    """Connect to the supervisor and execute one bounded task."""
    with socket.create_connection((host, port), timeout=15) as conn:
        conn.settimeout(360)
        request = _read_line(conn, MAX_RESULT_BYTES)
        if not request:
            raise SubagentError("supervisor closed IPC before sending a task")
        payload = json.loads(request.decode("utf-8"))
        if not isinstance(payload, dict) or payload.get("token") != token:
            raise SubagentError("supervisor authentication failed")
        from danica.agent import Agent
        from danica.audit import AuditLog
        from danica.permissions import PermissionPolicy

        permission = payload.get("permission", {})
        if not isinstance(permission, dict):
            raise SubagentError("invalid inherited permission policy")
        policy = PermissionPolicy()
        policy.project_mode = bool(permission.get("project_mode"))
        open_root = permission.get("open_root") or ""
        if not isinstance(open_root, str):
            raise SubagentError("invalid inherited Open Mode folder")
        policy.open_root = Path(open_root) if open_root else None
        policy.full_access = bool(permission.get("full_access"))
        allowed = permission.get("session_allowed", [])
        if not isinstance(allowed, list) or not all(
            isinstance(item, str) for item in allowed
        ):
            raise SubagentError("invalid inherited session permissions")
        policy.session_allowed = set(allowed)
        audit_path = Path(payload["audit_path"]) if payload.get("audit_path") else None
        # auto_repair=False: this process shares the parent's log; only the parent may repair (move or cut) it
        audit = AuditLog(audit_path, session=str(payload.get("session_id", "")), auto_repair=False) if audit_path else None
        ws = Workspace(Path(payload["workspace"]), open_mode=policy.open_root is not None)
        from danica.web import state_for
        web_state = state_for(ws)
        sources = payload.get("taint_sources", [])
        hosts = payload.get("known_hosts", [])
        if (
            not isinstance(sources, list)
            or not all(isinstance(item, str) for item in sources)
            or not isinstance(hosts, list)
            or not all(isinstance(item, str) for item in hosts)
        ):
            raise SubagentError("invalid inherited web trust state")
        for source in sources:
            web_state.mark_tainted(source)
        web_state.known_hosts.update(hosts)
        # Agent may call on_event from several threads (parallel reads): one lock covers both outputs
        send_lock = threading.Lock()
        live = {"open": False}                    # True while a streamed text line is unfinished

        def report(kind: str, data) -> None:
            wire = wire_event(kind, data)
            if wire is None:
                return
            with send_lock:
                try:                                         # 1) to the supervisor (TUI pane)
                    line = json.dumps({"token": token, "type": "event", **wire}, ensure_ascii=False)
                    conn.sendall(line.encode("utf-8") + b"\n")
                except OSError:
                    pass                                     # supervisor gone: keep working
                if console:                                  # 2) to this window (fallback)
                    _print_event(live, wire["kind"], wire["data"])

        agent = Agent(ws, max_steps=min(int(payload.get("max_turns", 1)), 8),
                      policy=policy, audit=audit, on_event=report)
        if policy.open_root is not None:          # the child knows the same Open Mode rules
            from danica.openmode import open_mode_note
            agent.set_mode_note(open_mode_note(policy.open_root, snapshots_off=False))
        if console:
            print(f"Danica subagent {payload.get('child_id', '')}\n"
                  f"Task: {str(payload.get('task', ''))[:300]}\n" + "-" * 60, flush=True)
        child_prompt = (
            "You are a depth-1 research subagent. Work only in the provided workspace and within "
            "the inherited permissions. Do not attempt to spawn another agent. Return a concise, "
            "structured summary with findings, evidence/paths, and limitations; do not return a "
            "transcript.\n\nTask:\n" + str(payload.get("task", ""))
        )
        try:
            summary = agent.run(child_prompt)
            response = {"token": token, "status": "completed",
                        "summary": summary[:MAX_SUMMARY_CHARS],
                        "changed_files": agent.touched[:50]}
        except Exception as exc:
            response = {"token": token, "status": "failed",
                        "error": f"{type(exc).__name__}: {exc}"[:500]}
        with send_lock:
            conn.sendall(json.dumps(response, ensure_ascii=False).encode("utf-8") + b"\n")
        if console:                                  # keep the window readable before it closes
            print(f"\n{'-' * 60}\n{response['status'].upper()}: "
                  f"{response.get('error') or 'summary sent to the main Danica window'}", flush=True)
            time.sleep(8 if response["status"] == "completed" else 30)
