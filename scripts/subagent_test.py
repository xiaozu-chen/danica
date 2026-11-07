"""Subagent IPC, inherited permissions, and supervisor limits; no model required."""

import json
import socket
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

from danica.permissions import PermissionPolicy, Tier, classify
from danica.subagents import Child, SubagentError, SubagentSupervisor, run_worker_connection
from danica.tools.registry import tool_schemas
from danica.workspace import Workspace


def check(label: str, condition: bool) -> None:
    print(("PASS  " if condition else "FAIL  ") + label)
    assert condition, label


class FakeProcess:
    def __init__(self, command, **kwargs):
        args = dict(zip(command[3::2], command[4::2]))
        self.code = None
        self.thread = threading.Thread(target=self._serve, args=(args,), daemon=True)
        self.thread.start()

    def _serve(self, args):
        with socket.create_connection((args["--host"], int(args["--port"])), timeout=3) as conn:
            request = json.loads(conn.makefile("rb").readline())
            self.received = request
            result = {"token": request["token"], "status": "completed",
                      "summary": "Research summary", "changed_files": []}
            conn.sendall(json.dumps(result).encode() + b"\n")
        self.code = 0

    def poll(self):
        return self.code

    def wait(self, timeout=None):
        self.thread.join(timeout)
        if self.thread.is_alive():
            raise TimeoutError("fake child did not finish")
        return self.code

    def kill(self):
        self.code = -9


class IdleProcess:
    def __init__(self, command, **kwargs):
        self.code = None

    def poll(self):
        return self.code

    def wait(self, timeout=None):
        return self.code

    def kill(self):
        self.code = -9


with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
    ws = Workspace(Path(tmp) / "workspace")
    policy = PermissionPolicy()
    policy.project_mode = True
    policy.session_allowed.add("tool:web_search")
    supervisor = SubagentSupervisor(ws, policy, timeout_seconds=3, new_console=False)
    with patch("danica.subagents.subprocess.Popen", FakeProcess):
        result = json.loads(supervisor.run("Research this question"))
    check("delegate is registered and execute-gated",
          "delegate" in {s["function"]["name"] for s in tool_schemas()}
          and classify("delegate", {"task": "research"})[0] == Tier.EXECUTE)
    check("parent receives only the summary and changed-file metadata",
          result == {"summary": "Research summary", "changed_files": []})
    received = FakeProcess  # keep the assertions below independent of process internals
    check("session history records a completed child", supervisor.list()[0]["status"] == "completed")

    try:
        supervisor.run("", depth=0)
        empty_rejected = False
    except SubagentError:
        empty_rejected = True
    try:
        supervisor.run("cannot recurse", depth=1)
        depth_rejected = False
    except SubagentError:
        depth_rejected = True
    check("empty tasks and nested delegation are refused", empty_rejected and depth_rejected)

    supervisor.children.extend(
        Child(str(i), "prior task", None, "completed", time.monotonic()) for i in range(2, 6)
    )
    try:
        supervisor.run("sixth task")
        cap_rejected = False
    except SubagentError:
        cap_rejected = True
    check("session cap is five total children", cap_rejected)

with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
    ws = Workspace(Path(tmp) / "workspace")
    supervisor = SubagentSupervisor(ws, PermissionPolicy(), timeout_seconds=30, new_console=False)
    with patch("danica.subagents.subprocess.Popen", IdleProcess):
        child_id = supervisor.start("background research")
        check("background subagents appear in the dashboard",
              supervisor.list()[0]["id"] == child_id
              and supervisor.list()[0]["status"] == "running")
        check("dashboard kill stops a running subagent",
              supervisor.kill(child_id) and supervisor.wait(child_id, timeout=3)
              and supervisor.list()[0]["status"] == "killed")
    supervisor.close()


with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    token = "local-test-token"
    payload = {
        "token": token,
        "task": "Summarize a source",
        "workspace": str(Path(tmp) / "child"),
        "max_turns": 3,
        "permission": {"project_mode": True, "full_access": False,
                       "session_allowed": ["tool:web_search"]},
        "taint_sources": ["https://example.test/"],
        "known_hosts": ["example.test"],
    }

    def send_task():
        conn, _ = listener.accept()
        with conn:
            conn.sendall(json.dumps(payload).encode() + b"\n")
            result = json.loads(conn.makefile("rb").readline())
            return result

    received = {}

    def fake_agent(ws, **kwargs):
        received["ws"] = ws
        received["max_steps"] = kwargs["max_steps"]
        received["policy"] = kwargs["policy"]

        class Agent:
            touched = ["report.md"]

            def run(self, prompt):
                received["prompt"] = prompt
                return "Findings: one relevant passage."

        return Agent()

    sender = threading.Thread(target=lambda: received.update(response=send_task()), daemon=True)
    sender.start()
    with patch("danica.agent.Agent", side_effect=fake_agent):
        run_worker_connection("127.0.0.1", listener.getsockname()[1], token)
    sender.join(timeout=3)
    listener.close()
    response = received["response"]
    check("worker validates token and returns a structured concise result",
          response["status"] == "completed" and response["summary"].startswith("Findings:")
          and response["changed_files"] == ["report.md"])
    check("worker inherits permissions and taint ceiling",
          received["max_steps"] == 3 and received["policy"].project_mode
          and not received["policy"].full_access
          and received["policy"].session_allowed == {"tool:web_search"}
          and received["ws"].web.tainted
          and "example.test" in received["ws"].web.known_hosts)

print("\nAll good.")
