"""
artifact_test.py - Checks the artifact list (against a real shadow git), the safe /open rules,
/save, and how the agent tracks written files. No model needed.
Run from the project root:  python -m scripts.artifact_test
"""

import json
import re
import tempfile
from pathlib import Path

from docx import Document
from PIL import Image

import danica.agent as agent_mod
import danica.artifacts as art
from danica.agent import Agent
from danica.artifacts import Artifact, ArtifactError, collect, is_openable, open_artifact, pick, save_copy
from danica.checkpoints import Checkpoints
from danica.docgen import build_docx
from danica.markdown_doc import parse
from danica.workspace import Workspace


def check(label: str, condition: bool) -> None:
    print(("PASS  " if condition else "FAIL  ") + label)
    assert condition, label


def error_of(fn, *args) -> str:
    try:
        fn(*args)
        return ""
    except ArtifactError as e:
        return str(e)


with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
    tmp = Path(tmp)

    # --- the list, from a real shadow git ---
    ws = Workspace(tmp / "ws")
    (ws.root / "keep.txt").write_text("a", encoding="utf-8")
    (ws.root / "gone.txt").write_text("b", encoding="utf-8")
    (ws.root / ".gitignore").write_text("ign.txt\n", encoding="utf-8")
    cp = Checkpoints(ws, tmp / "git")
    snap = cp.begin_request("go")

    (ws.root / "keep.txt").write_text("changed", encoding="utf-8")
    (ws.root / "gone.txt").unlink()
    (ws.root / "new.txt").write_text("n", encoding="utf-8")
    (ws.root / "out.csv").write_text("a,b\n", encoding="utf-8")                # as if a script wrote it
    (ws.root / "ign.txt").write_text("hidden from git", encoding="utf-8")
    (ws.root / ".env").write_text("A=1\n", encoding="utf-8")
    (ws.root / "charts").mkdir()
    Image.new("RGB", (20, 10), "red").save(ws.root / "charts" / "a.png")
    (ws.root / "charts" / "a.json").write_text("{}", encoding="utf-8")
    (ws.root / "charts" / "solo.json").write_text("{}", encoding="utf-8")
    (ws.root / "__pycache__").mkdir()
    (ws.root / "__pycache__" / "x.pyc").write_bytes(b"x")

    found = collect(ws, cp, snap, [], {"charts/a.png"})
    got = {a.rel: a.status for a in found.items}
    check("changed, new and script-made files are listed",
          got == {"keep.txt": "changed", "new.txt": "new", "out.csv": "new", "charts/solo.json": "new"})
    check("a deleted file is counted, not listed", found.deleted == 1)
    check("sizes are real", next(a for a in found.items if a.rel == "new.txt").size == 1)
    check("credential files, caches, shown pictures and chart data are left out",
          not {".env", "__pycache__/x.pyc", "charts/a.png", "charts/a.json"} & set(got))

    found = collect(ws, cp, snap, ["ign.txt"], {"charts/a.png"})
    check("a file the project's .gitignore hides is still listed when a file tool wrote it",
          {a.rel: a.status for a in found.items}.get("ign.txt") == "written")

    for i in range(15):
        (ws.root / f"f{i}.txt").write_text("x", encoding="utf-8")
    found = collect(ws, cp, snap, [], set())
    check("the list is capped and says how many more", len(found.items) == 12 and found.more == 8)

    ws2 = Workspace(tmp / "ws2")
    (ws2.root / "new.txt").write_text("n", encoding="utf-8")
    found = collect(ws2, None, None, ["new.txt", "missing.txt", "..\\..\\x.txt"], set())
    check("without git, only the files the tools wrote are listed",
          [(a.rel, a.status) for a in found.items] == [("new.txt", "written")])

    # --- what /open may open ---
    for name in ("t.txt", "p.py", "c.csv", "s.bat"):
        (ws.root / name).write_text("x", encoding="utf-8")
    ours = ws.root / "ours.docx"
    build_docx(parse("# x"), ours, title="", meta_title="x", sources=[], page="A4", image_for=lambda r: None)
    theirs = ws.root / "theirs.docx"
    Document().save(str(theirs))
    check("plain text opens", is_openable(ws.root / "t.txt"))
    check("scripts and spreadsheets never open", not any(is_openable(ws.root / n) for n in ("p.py", "c.csv", "s.bat")))
    check("a Word file opens only if Danica made it", is_openable(ours) and not is_openable(theirs))

    calls: list[tuple[str, str]] = []
    real = art._startfile, art._explorer
    art._startfile = lambda p: calls.append(("open", p))
    art._explorer = lambda p: calls.append(("folder", p))
    try:
        open_artifact(Artifact("t.txt", ws.root / "t.txt", "new", 1))
        open_artifact(Artifact("p.py", ws.root / "p.py", "new", 1))
        check("a safe file is opened, anything else opens its folder instead",
              [c[0] for c in calls] == ["open", "folder"])
        check("a vanished file is an error", "no longer exists" in error_of(
            open_artifact, Artifact("z.txt", ws.root / "z.txt", "new", 1)))
    finally:
        art._startfile, art._explorer = real

    # --- /save ---
    dest = tmp / "dest"
    dest.mkdir()
    first = save_copy(ws.root / "t.txt", dest)
    second = save_copy(ws.root / "t.txt", dest)
    check("a copy never overwrites", first.name == "t.txt" and second.name == "t (1).txt"
          and first.read_text(encoding="utf-8") == "x")
    check("a missing folder is an error", "not a folder" in error_of(save_copy, ws.root / "t.txt", tmp / "nope"))

    # --- picking ---
    items = [Artifact("a", ws.root / "t.txt", "new", 1), Artifact("b", ws.root / "p.py", "new", 1)]
    check("pick by number", pick("2", items).rel == "b")
    check("a bad number is an error", "1 to 2" in error_of(pick, "3", items) and "1 to 2" in error_of(pick, "x", items))
    check("no number with several files asks which", "which one" in error_of(pick, "", items))
    check("no number with one file takes it", pick("", items[:1]).rel == "a")
    check("no files is a clear error", "no files" in error_of(pick, "1", []))

    # --- the agent tracks what was written ---
    real_stream, real_run_tool = agent_mod.stream_chat, agent_mod.run_tool
    turn = {"n": 0}

    def stream(messages, **kwargs):
        turn["n"] += 1
        if turn["n"] == 1:
            yield {"type": "tool_calls", "calls": [
                {"id": "c1", "name": "write_file", "arguments": json.dumps({"path": "notes.txt", "content": "x"})},
                {"id": "c2", "name": "write_file", "arguments": json.dumps({"path": "bad.txt", "content": "x"})},
                {"id": "c3", "name": "document", "arguments": json.dumps({"source": "r.md"})}]}
        else:
            yield {"type": "content", "text": "done"}

    def fake_tool(name, args, w):
        if name == "document":
            return "OK: saved reports/r.docx (3 KB, 2 sections, 0 image(s), 0 source(s))."
        return "ERROR: nope" if args.get("path") == "bad.txt" else "OK: created notes.txt (1 lines)"

    agent_mod.stream_chat, agent_mod.run_tool = stream, fake_tool
    try:
        agent = Agent(Workspace(tmp / "ws3"))
        agent.run("write things")
        check("the agent remembers written files, not failed ones", agent.touched == ["notes.txt", "reports/r.docx"])
        check("without checkpoints there is no snapshot id", agent.request_snap is None)
        turn["n"] = 99
        agent.run("thanks")
        check("the lists reset for every request", agent.touched == [])

        ws4 = Workspace(tmp / "ws4")
        agent2 = Agent(ws4, checkpoints=Checkpoints(ws4, tmp / "git4"))
        agent2.run("hello")
        check("with checkpoints the agent keeps the snapshot id",
              re.fullmatch(r"[0-9a-f]{8}", agent2.request_snap or "") is not None)
    finally:
        agent_mod.stream_chat, agent_mod.run_tool = real_stream, real_run_tool

print("\nAll good.")