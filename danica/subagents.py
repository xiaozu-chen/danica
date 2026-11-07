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
from typing import TYPE_CHECKING

from danica.audit import AuditLog
from danica.workspace import Workspace

if TYPE_CHECKING:
    from danica.permissions import PermissionPolicy

MAX_CHILDREN = 5
MAX_ACTIVE = 1
MAX_DEPTH = 1
MAX_TASK_CHARS = 12_000
MAX_RESULT_BYTES = 32_000
MAX_SUMMARY_CHARS = 8_000


@dataclass
class Child:
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

    def _log(self, event: str, **data) -> None:
        if self.audit:
            self.audit.append(event, **data)

    def run(self, task: str, depth: int = 0, *,
            _ready: threading.Event | None = None,
            _ready_child: list[str] | None = None) -> str:
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
                sys.executable, "-m", "danica.subagent_worker",
                "--host", "127.0.0.1", "--port", str(listener.getsockname()[1]),
                "--token", token,
            ]
            flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0) if (
                self.new_console and os.name == "nt") else 0
            process = subprocess.Popen(
                command,
                cwd=str(self.ws.root),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags,
            )
            child.process = process
            child.status = "running"
            if _ready:
                if _ready_child is not None:
                    _ready_child.append(child_id)
                _ready.set()
            self._log("subagent_start", child=child_id, depth=depth,
                      task_chars=len(task), workspace=str(self.ws.root))

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
                        "full_access": self.policy.full_access,
                        "session_allowed": sorted(self.policy.session_allowed),
                    },
                    "taint_sources": list(getattr(self.ws.web, "taint_sources", []))[:20],
                    "known_hosts": sorted(getattr(self.ws.web, "known_hosts", set()))[:100],
                    "audit_path": str(self.audit.path) if self.audit else "",
                }
                conn.sendall(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")
                response = _read_line(conn, MAX_RESULT_BYTES)
            if not response:
                raise SubagentError("subagent closed IPC without returning a result")
            result = json.loads(response.decode("utf-8"))
            if not isinstance(result, dict) or result.get("token") != token:
                raise SubagentError("subagent returned an invalid protocol response")
            if result.get("status") != "completed":
                raise SubagentError(str(result.get("error") or "subagent failed"))
            summary = result.get("summary")
            if not isinstance(summary, str) or not summary.strip():
                raise SubagentError("subagent returned an empty summary")
            if len(summary) > MAX_SUMMARY_CHARS:
                summary = summary[:MAX_SUMMARY_CHARS] + "\n[summary truncated]"
            files = result.get("changed_files", [])
            if not isinstance(files, list) or not all(isinstance(item, str) for item in files):
                raise SubagentError("subagent returned invalid changed-file metadata")
            child.status = "completed"
            child.result = summary
            self._log("subagent_complete", child=child_id, summary_chars=len(summary),
                      changed_files=files[:50])
            return json.dumps({"summary": summary, "changed_files": files[:50]},
                              ensure_ascii=False)
        except (OSError, socket.timeout, json.JSONDecodeError, UnicodeDecodeError) as exc:
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
        thread = self._threads.get(child_id)
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def close(self) -> None:
        for child in list(self.children):
            if child.status in ("starting", "running"):
                self.kill(child.id)
        for thread in list(self._threads.values()):
            thread.join(timeout=5)
            if thread.is_alive():
                raise SubagentError(f"subagent worker {thread.name} did not stop")

    def list(self) -> list[dict]:
        for child in self.children:
            if child.process and child.status == "running" and child.process.poll() is not None:
                child.status = "failed"
        return [{"id": c.id, "status": c.status,
                 "elapsed_seconds": round(time.monotonic() - c.started_at, 1),
                 "task": c.task[:200], "summary": c.result[:500]}
                for c in self.children]

    def kill(self, child_id: str) -> bool:
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


def run_worker_connection(host: str, port: int, token: str) -> None:
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
        policy.full_access = bool(permission.get("full_access"))
        allowed = permission.get("session_allowed", [])
        if not isinstance(allowed, list) or not all(isinstance(item, str) for item in allowed):
            raise SubagentError("invalid inherited session permissions")
        policy.session_allowed = set(allowed)
        audit_path = Path(payload["audit_path"]) if payload.get("audit_path") else None
        audit = AuditLog(audit_path, session=str(payload.get("session_id", ""))) if audit_path else None
        ws = Workspace(Path(payload["workspace"]))
        from danica.web import state_for
        web_state = state_for(ws)
        sources = payload.get("taint_sources", [])
        hosts = payload.get("known_hosts", [])
        if (not isinstance(sources, list) or not all(isinstance(item, str) for item in sources)
                or not isinstance(hosts, list) or not all(isinstance(item, str) for item in hosts)):
            raise SubagentError("invalid inherited web trust state")
        for source in sources:
            web_state.mark_tainted(source)
        web_state.known_hosts.update(hosts)
        agent = Agent(ws, max_steps=min(int(payload.get("max_turns", 1)), 8),
                      policy=policy, audit=audit)
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
        conn.sendall(json.dumps(response, ensure_ascii=False).encode("utf-8") + b"\n")
