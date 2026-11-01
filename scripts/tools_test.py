"""
tools_test.py - Checks the file tools and, more importantly, that the path
jail actually holds. Run from the project root:  python -m scripts.tools_test
"""

import tempfile
from pathlib import Path

from danica.tools import run_tool, tool_schemas
from danica.workspace import Workspace


def check(label: str, condition: bool) -> None:
    print(("PASS  " if condition else "FAIL  ") + label)
    assert condition, label


with tempfile.TemporaryDirectory() as tmp:
    ws = Workspace(Path(tmp) / "ws")

    # --- happy paths ---
    check("write creates file",
          run_tool("write_file", {"path": "a/hello.py", "content": "print('hi')\nx = 1\n"}, ws).startswith("OK"))
    check("read shows numbered lines",
          "1\tprint('hi')" in run_tool("read_file", {"path": "a/hello.py"}, ws))
    check("edit replaces text",
          run_tool("edit_file", {"path": "a/hello.py", "old_string": "x = 1", "new_string": "x = 2"}, ws).startswith("OK"))
    check("edit really changed the file",
          "x = 2" in run_tool("read_file", {"path": "a/hello.py"}, ws))
    check("list shows folder", "a/" in run_tool("list_dir", {}, ws))

    # --- edit safety ---
    check("edit: missing text is an error",
          run_tool("edit_file", {"path": "a/hello.py", "old_string": "nope", "new_string": "y"}, ws).startswith("ERROR"))
    run_tool("write_file", {"path": "dup.txt", "content": "aa\naa\n"}, ws)
    check("edit: ambiguous match is refused",
          "matches 2" in run_tool("edit_file", {"path": "dup.txt", "old_string": "aa", "new_string": "b"}, ws))

    # --- the jail ---
    check("jail: ../ escape blocked",
          "outside the workspace" in run_tool("read_file", {"path": "..\\..\\secret.txt"}, ws))
    check("jail: absolute path blocked",
          "outside the workspace" in run_tool("read_file", {"path": "C:\\Windows\\win.ini"}, ws))
    check("jail: write escape blocked",
          "outside the workspace" in run_tool("write_file", {"path": "..\\evil.txt", "content": "x"}, ws))
    check("jail: evil file was not created", not (Path(tmp) / "evil.txt").exists())

    # --- robustness ---
    check("unknown tool is a string error", run_tool("nope", {}, ws).startswith("ERROR"))
    check("bad args are a string error", run_tool("read_file", {"wrong": 1}, ws).startswith("ERROR"))

print(f"\nAll good. {len(tool_schemas())} tools registered.")