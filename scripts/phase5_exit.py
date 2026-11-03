"""
phase5_exit.py - Phase 5 exit test: trust a code folder, edit real files, undo.

Offline: a scripted fake model makes the tool calls, while everything else is
real (project registration, permission policy, shadow git, audit log, shell).
Run from the project root:  python -m scripts.phase5_exit      (takes ~10 seconds)
"""

import json
import tempfile
from pathlib import Path

import danica.agent as agent_mod
from danica.agent import Agent
from danica.audit import AuditLog, verify
from danica.checkpoints import Checkpoints
from danica.export import export_session
from danica.permissions import PermissionPolicy
from danica.projects import register
from danica.web import state_for
from danica.workspace import Workspace


def check(label: str, condition: bool) -> None:
    print(("PASS  " if condition else "FAIL  ") + label)
    assert condition, label


script: list[dict] = []     # the fake model's next replies, in order
counter = {"n": 0}


def call(name: str, **args) -> None:
    """Queue one tool call for the fake model."""
    counter["n"] += 1
    script.append({"type": "tool_calls", "calls": [
        {"id": f"c{counter['n']}", "name": name, "arguments": json.dumps(args)}]})


def fake_stream(messages, **kwargs):
    if script:
        yield script.pop(0)
    else:
        yield {"type": "content", "text": "done"}      # script used up: finish the request


real_stream = agent_mod.stream_chat
agent_mod.stream_chat = fake_stream
try:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        tmp = Path(tmp)

        # --- a "real" code folder, with its own git repo that Danica must never touch ---
        proj = tmp / "proj"
        proj.mkdir()
        (proj / "hello.py").write_text("print('hi')\n", encoding="utf-8")
        (proj / ".git").mkdir()
        marker = proj / ".git" / "MARKER"
        marker.write_text("mine", encoding="utf-8")

        # --- trust it, then build the same pieces the CLI builds for project mode ---
        info = register(proj, tmp / "projects")
        session = tmp / "session"
        session.mkdir()
        ws = Workspace(proj, todo_path=session / "TODO.md", sources_path=session / "sources.json")

        asked: list[tuple[str, str]] = []

        def ask(tool, summary, tier, reason, can_remember) -> str:
            asked.append((tier.value, summary))
            return "deny"                                  # unattended: never allow

        policy = PermissionPolicy(ask=ask)
        policy.project_mode = True
        audit = AuditLog(session / "audit.jsonl", session="phase5")
        cp = Checkpoints(ws, Path(info["dir"]) / "git")
        agent = Agent(ws, transcript_path=session / "transcript.jsonl", policy=policy,
                      audit=audit, checkpoints=cp)

        # ============ request 1: edit a real file, run it, try two things that must not work ============
        call("edit_file", path="hello.py", old_string="print('hi')",
             new_string="def greet(name):\n    return 'hi ' + name\n\nprint(greet('x'))")
        call("run_shell", command="python hello.py")                  # everyday command: free
        call("run_shell", command="pip install requests")             # installs: must ask
        call("write_file", path=".git/hooks/pre-commit", content="echo pwned\n")   # must be blocked
        agent.run("add a greet function and run it")

        results = [m["content"] for m in agent.messages if m["role"] == "tool"]
        entries = [json.loads(x) for x in (session / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
        calls = [e["data"] for e in entries if e["event"] == "tool_call"]

        check("the real file was edited", "def greet" in (proj / "hello.py").read_text(encoding="utf-8"))
        check("the shell ran the edited file", any("hi x" in r for r in results))
        check("python script.py ran WITHOUT asking",
              any(c["tool"] == "run_shell" and "hello.py" in str(c["args"]) and c["how"] == "project-free"
                  for c in calls))
        check("pip install DID ask, and the denial held",
              len(asked) == 1 and "pip install" in asked[0][1]
              and any(c["tool"] == "run_shell" and not c["allowed"] for c in calls))
        check("a hook write was blocked", not (proj / ".git" / "hooks").exists()
              and any("run automatically" in r for r in results))
        check("a checkpoint was taken before the request", len(cp.undo_stack) == 1)

        # ============ undo ============
        summary, _ = cp.undo()
        check("undo restored the real file", (proj / "hello.py").read_text(encoding="utf-8") == "print('hi')\n")
        check("the user's own .git was never touched", marker.read_text(encoding="utf-8") == "mine")
        check("the shadow repo lives outside the project",
              (Path(info["dir"]) / "git" / "HEAD").exists() and not (proj / "HEAD").exists())

        # ============ request 2: untrusted web content is in context ============
        state_for(ws).mark_tainted("search: pelicans")
        call("write_file", path="notes.txt", content="a fact from a web page")
        agent.run("write down one fact")
        check("tainted: the write asked", len(asked) == 2 and asked[1][0] == "untrusted-context")
        check("tainted: the denied write did not happen", not (proj / "notes.txt").exists())

        # ============ the record ============
        ok, count, problem = verify(session / "audit.jsonl")
        check("the audit chain is intact", ok and count > 8)

        out = export_session(session, ws, info, out_root=tmp / "exports")
        check("the export names the tools that ran",
              "edit_file" in (out / "conversation.md").read_text(encoding="utf-8"))
        check("a project export has no workspace copy", not (out / "workspace").exists())
finally:
    agent_mod.stream_chat = real_stream

print("\nAll good. Phase 5 exit test passed.")