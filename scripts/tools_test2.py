"""
tools_test2.py - Checks glob, grep and run_shell.
Run from the project root:  python -m scripts.tools_test2
"""

import os
import tempfile
from pathlib import Path

from danica.tools import run_tool, tool_schemas
from danica.workspace import Workspace


def check(label: str, condition: bool) -> None:
    print(("PASS  " if condition else "FAIL  ") + label)
    assert condition, label


# Plant a fake secret in OUR environment; the shell child must not see it
os.environ["DANICA_FAKE_SECRET"] = "hunter2"

with tempfile.TemporaryDirectory() as tmp:
    ws = Workspace(Path(tmp) / "ws")
    run_tool("write_file", {"path": "src/app.py", "content": "def main():\n    print('Hello')\n"}, ws)
    run_tool("write_file", {"path": "src/util.py", "content": "# helper\nHELLO = 1\n"}, ws)
    run_tool("write_file", {"path": ".venv/junk.py", "content": "def main(): pass\n"}, ws)

    # --- glob ---
    out = run_tool("glob", {"pattern": "**/*.py"}, ws)
    check("glob finds python files", "src/app.py" in out and "src/util.py" in out)
    check("glob skips .venv", ".venv" not in out)
    check("glob blocks ..", run_tool("glob", {"pattern": "../*"}, ws).startswith("ERROR"))
    check("glob blocks absolute", run_tool("glob", {"pattern": "C:\\*"}, ws).startswith("ERROR"))

    # --- grep ---
    out = run_tool("grep", {"pattern": "def main"}, ws)
    check("grep finds match with line number", "src/app.py:1:" in out)
    check("grep skips .venv", ".venv" not in out)
    check("grep ignore_case works",
          "util.py:2" in run_tool("grep", {"pattern": "hello", "ignore_case": True}, ws))
    check("grep include filter works",
          "No matches" in run_tool("grep", {"pattern": "def main", "include": "*.md"}, ws))
    check("grep bad regex is an error string",
          run_tool("grep", {"pattern": "(unclosed"}, ws).startswith("ERROR"))
    check("grep jail holds", "outside the workspace" in run_tool("grep", {"pattern": "x", "path": "..\\.."}, ws))

    # --- run_shell ---
    out = run_tool("run_shell", {"command": "echo hello-from-shell"}, ws)
    check("shell runs and reports exit code", "[exit code 0]" in out and "hello-from-shell" in out)
    check("shell reports failures",
          "[exit code 3]" in run_tool("run_shell", {"command": "exit 3"}, ws))

    run_tool("run_shell", {"command": "Set-Content probe.txt hi"}, ws)
    check("shell cwd is the workspace", (ws.root / "probe.txt").exists())

    out = run_tool("run_shell", {"command": "echo \"[$env:DANICA_FAKE_SECRET]\""}, ws)
    check("shell does not leak env secrets", "hunter2" not in out)

    out = run_tool("run_shell", {"command": "Start-Sleep 20", "timeout": 2}, ws)
    check("shell timeout kills the process", "TIMED OUT" in out)

    out = run_tool("run_shell", {"command": "1..5000 | ForEach-Object { 'line number ' + $_ }"}, ws)
    check("shell output is capped", "truncated" in out and len(out) < 9000)

print(f"\nAll good. {len(tool_schemas())} tools registered.")