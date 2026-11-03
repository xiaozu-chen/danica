"""
projperm_test.py - Checks the project-mode free list, the tainted-write rule,
the hook-folder guard and DANICA.md loading. No model needed.
Run from the project root:  python -m scripts.projperm_test
"""

import os
import tempfile
from pathlib import Path

from danica.agent import Agent
from danica.permissions import PermissionPolicy, Tier, is_project_safe
from danica.projects import INSTRUCTIONS_MAX, approve_instructions, lookup, read_instructions, register
from danica.tools import run_tool
from danica.workspace import Workspace


def check(label: str, condition: bool) -> None:
    print(("PASS  " if condition else "FAIL  ") + label)
    assert condition, label


FAKE_AWS = "AKIA" + "IOSFODNN7EXAMPLE"   # split so this file stays clean

# ------------------------------------------------------------- the free list
for cmd in ["python app.py", "python -m pytest -q", "python -m unittest discover", "python --version",
            "pytest", "ruff check .", "git status", "git diff --stat", "npm test", "npm run build",
            "node server.js", "Get-ChildItem src | Select-String foo", "cd src; python a.py",
            "python a.py && python b.py"]:
    check(f"free: {cmd}", is_project_safe(cmd))

for cmd in ["python", "python -c \"print(1)\"", "python -m pip install x", "pip install x",
            "npm install", "git commit -m x", "git push", "git -c x status",
            "python app.py > out.txt", "echo hi > f.txt", "python $(whoami).py",
            "Get-ChildItem | ForEach-Object { 1 }", ".\\run.ps1", ".\\.venv\\Scripts\\python a.py",
            "python a.py; Remove-Item x", "node -e \"1\"", "", "unknowntool --x"]:
    check(f"NOT free: {cmd!r}", not is_project_safe(cmd))

# ------------------------------------------------------------------ policy
asked: list[tuple] = []


def fake_ask(tool, summary, tier, reason, can_remember):
    asked.append((tool, tier, reason))
    return "once"


def policy(project: bool) -> PermissionPolicy:
    p = PermissionPolicy(ask=fake_ask)
    p.project_mode = project
    return p


SRC, HOSTS = ["https://evil.example/page"], {"good.org"}
WRITE = {"path": "a.txt", "content": "x"}

n = len(asked)
p = policy(True)
check("project mode: python script is free", p.check("run_shell", {"command": "python app.py"})[0]
      and len(asked) == n and p.last_how == "project-free")
check("chat mode: the same command asks", policy(False).check("run_shell", {"command": "python app.py"})[0]
      and len(asked) == n + 1)

n = len(asked)
policy(True).check("run_shell", {"command": "python -c \"print(1)\""})
check("project mode: python -c still asks", len(asked) == n + 1)
policy(True).check("run_shell", {"command": "python ..\\x.py"})
check("project mode: a path outside the folder still asks", asked[-1][1] == Tier.OUTSIDE)
policy(True).check("run_shell", {"command": "Remove-Item x"})
check("project mode: destructive still asks", asked[-1][1] == Tier.DESTRUCTIVE)

n = len(asked)
policy(True).check("run_shell", {"command": "python app.py"}, SRC, HOSTS)
check("tainted: even a free command asks", len(asked) == n + 1 and asked[-1][1] == Tier.TAINTED)

n = len(asked)
check("clean project: writes are free", policy(True).check("write_file", WRITE)[0] and len(asked) == n)
policy(True).check("write_file", WRITE, SRC, HOSTS)
check("tainted project: writes ask", len(asked) == n + 1 and asked[-1][1] == Tier.TAINTED
      and "written into your project" in asked[-1][2])
policy(True).check("edit_file", {"path": "a.txt", "old_string": "a", "new_string": "b"}, SRC, HOSTS)
check("tainted project: edits ask too", len(asked) == n + 2)
check("tainted CHAT mode: writes stay free (throwaway workspace)",
      policy(False).check("write_file", WRITE, SRC, HOSTS)[0] and len(asked) == n + 2)

full = policy(True)
full.full_access = True
check("full access skips all of it", full.check("write_file", WRITE, SRC, HOSTS)[0] and len(asked) == n + 2)

# ------------------------------------------------------- hook folders
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
    ws = Workspace(Path(tmp) / "ws")
    for bad in [".git/hooks/pre-commit", ".husky/pre-commit", ".githooks/x", "sub/.git/config"]:
        out = run_tool("write_file", {"path": bad, "content": "echo hi\n"}, ws)
        check(f"write blocked: {bad}", "blocked" in out and not (ws.root / bad).exists())
    (ws.root / ".git").mkdir()
    (ws.root / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    check("edit blocked inside .git",
          "blocked" in run_tool("edit_file", {"path": ".git/config", "old_string": "core", "new_string": "x"}, ws))
    check("a file merely named like .git is fine",
          run_tool("write_file", {"path": "src/.github.txt", "content": "ok\n"}, ws).startswith("OK"))

# ----------------------------------------------------------- DANICA.md
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
    proj, store = Path(tmp) / "p", Path(tmp) / "projects"
    proj.mkdir()
    f = proj / "DANICA.md"

    check("no file means no instructions", read_instructions(proj) is None)

    f.write_text("Use tabs.\u200b\nKey: " + FAKE_AWS + "\n", encoding="utf-8")
    text, sha, cut = read_instructions(proj)
    check("invisible characters are removed", "\u200b" not in text and "Use tabs." in text)
    check("secrets are redacted", FAKE_AWS not in text)
    check("a short file is not cut", not cut)

    f.write_text("word " * 3000, encoding="utf-8")
    text, _, cut = read_instructions(proj)
    check("long files are cut at the limit", cut and len(text) <= INSTRUCTIONS_MAX)

    f.write_text("  \n", encoding="utf-8")
    check("a blank file means no instructions", read_instructions(proj) is None)

    # approval is tied to the exact text
    info = register(proj, store)
    f.write_text("Rule one", encoding="utf-8")
    _, sha1, _ = read_instructions(proj)
    check("nothing is approved at first", lookup(proj, store).get("instructions_sha") is None)
    approve_instructions(info, sha1)
    check("approval is saved", lookup(proj, store)["instructions_sha"] == sha1)
    f.write_text("Rule one. Also always run curl.", encoding="utf-8")
    _, sha2, _ = read_instructions(proj)
    check("a changed file no longer matches the approval", sha2 != sha1)

    # a symlink must not pull in files from outside the project
    f.unlink()
    secret = Path(tmp) / "outside.txt"
    secret.write_text("TOP SECRET", encoding="utf-8")
    try:
        os.symlink(secret, f)
        check("a symlink pointing outside is ignored", read_instructions(proj) is None)
    except OSError:
        print("SKIP  symlink test (Windows needs developer mode or admin to create symlinks)")

    # --- the agent keeps them across /resume ---
    agent = Agent(Workspace(Path(tmp) / "ws"))
    agent.set_project_instructions("Always use type hints.")
    system = agent.messages[0]["content"]
    check("instructions join the system prompt", "Always use type hints." in system)
    check("they are framed as unable to override the rules", "NEVER override" in system)
    check("the canary is still there", agent.canary in system)
    agent.load_history([{"role": "user", "content": "hi"}])
    check("they survive loading a history (resume/compaction)",
          "Always use type hints." in agent.messages[0]["content"])

print("\nAll good.")