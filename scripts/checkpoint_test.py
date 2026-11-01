"""
checkpoint_test.py - Checks snapshots, /undo, /rewind and safety copies.
Needs git installed. Run from the project root:  python -m scripts.checkpoint_test
"""

import tempfile
from pathlib import Path

from danica.checkpoints import CheckpointError, Checkpoints
from danica.workspace import Workspace


def check(label: str, condition: bool) -> None:
    print(("PASS  " if condition else "FAIL  ") + label)
    assert condition, label


# git leaves read-only files behind, so tell the temp dir to ignore cleanup errors
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
    ws = Workspace(Path(tmp) / "ws")
    cp = Checkpoints(ws, Path(tmp) / "cp" / "git")
    check("git is available", cp.available)

    a, b = ws.root / "a.txt", ws.root / "b.txt"

    # --- request 1 starts on an EMPTY workspace and creates a file ---
    cp.begin_request("make a")
    a.write_text("one", encoding="utf-8")

    # --- request 2 edits it, adds another file, and writes into an excluded folder ---
    cp.begin_request("change things")
    a.write_text("two", encoding="utf-8")
    b.write_text("new", encoding="utf-8")
    (ws.root / ".venv").mkdir()
    (ws.root / ".venv" / "keep.txt").write_text("x", encoding="utf-8")

    summary, safety_1 = cp.undo()
    check("undo restores the edited file", a.read_text(encoding="utf-8") == "one")
    check("undo removes the file the request created", not b.exists())
    check("excluded folders are left alone", (ws.root / ".venv" / "keep.txt").exists())
    check("summary names the changed files", "b.txt" in summary and "removed" in summary)

    # --- second undo reverts request 1 (the workspace was empty before it) ---
    cp.undo()
    check("second undo reaches the empty workspace", not a.exists())
    check("empty stack says so", "Nothing to undo" in cp.undo()[0])

    # --- an undo can itself be undone through its safety copy ---
    summary, _ = cp.rewind(safety_1)
    check("rewind to a safety copy brings changes back",
          a.read_text(encoding="utf-8") == "two" and b.exists())

    # --- deleted files come back ---
    cp.begin_request("delete a")
    a.unlink()
    summary, _ = cp.undo()
    check("undo restores a deleted file", a.exists() and a.read_text(encoding="utf-8") == "two")
    check("summary says restored", "restored" in summary)

    # --- a request that changed nothing ---
    cp.begin_request("just chatting")
    summary, _ = cp.undo()
    check("no-change requests are reported", "No net file changes" in summary)

    # --- listing ---
    labels = [label for _, _, label in cp.list_checkpoints()]
    check("list shows request labels", any(l.startswith("before: delete a") for l in labels))
    check("list shows safety copies", any(l.startswith("safety copy") for l in labels))

    # --- bad ids ---
    for bad in ["zzzz", "--help", "", "deadbeef"]:
        try:
            cp.rewind(bad)
            raised = False
        except CheckpointError:
            raised = True
        check(f"rewind rejects {bad!r}", raised)

print("\nAll good.")