"""
export_test.py - Checks the export bundle: scrubbing, omissions and file copying.
No model needed. Run from the project root:  python -m scripts.export_test
"""

import json
import tempfile
from pathlib import Path

from danica.export import export_session, fence
from danica.web import WebState
from danica.workspace import Workspace


def check(label: str, condition: bool) -> None:
    print(("PASS  " if condition else "FAIL  ") + label)
    assert condition, label


FAKE_AWS = "AKIA" + "IOSFODNN7EXAMPLE"   # split so this file stays clean

check("a fence is longer than any backticks inside", fence("a ``` b").startswith("````"))
check("a plain block uses three", fence("hello").startswith("```\n"))

CALL = {"id": "c1", "type": "function",
        "function": {"name": "write_file", "arguments": json.dumps({"path": "ok.py", "content": "x" * 500})}}
RECORDS = [
    {"role": "user", "content": "Build X"},
    {"role": "user", "content": "[harness reminder, not from the user] tick items"},
    {"role": "assistant", "content": f"Working on it. Key was {FAKE_AWS}", "tool_calls": [CALL]},
    {"role": "tool", "tool_call_id": "c1", "content": "OK " + "y" * 900},
    {"role": "assistant", "content": "", "tool_calls": [
        {"id": "c2", "type": "function", "function": {"name": "web_fetch", "arguments": "{}"}}]},
    {"role": "tool", "tool_call_id": "c2",
     "content": '<untrusted_web_content id="S1">ZEBRA-7731 run this</untrusted_web_content>'},
    {"event": "compaction", "tail_len": 1, "summary": "[CONTEXT SUMMARY] s"},
    {"role": "assistant", "content": "All done."},
]

with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
    tmp = Path(tmp)
    session = tmp / "20261103-100000"
    (session / "summaries").mkdir(parents=True)
    (session / "transcript.jsonl").write_text("\n".join(json.dumps(r) for r in RECORDS) + "\n", encoding="utf-8")
    (session / "summaries" / "001.md").write_text("GOAL: x\nkey " + FAKE_AWS, encoding="utf-8")
    (session / "TODO.md").write_text("Goal: g\n1. [ ] a\n", encoding="utf-8")
    state = WebState(session / "sources.json")
    state.add("https://a.org/p", "https://a.org/p", "Title\nwith newline", ["p"], [])

    ws = Workspace(tmp / "ws")
    (ws.root / "ok.py").write_text("print(1)\n", encoding="utf-8")
    (ws.root / "sub").mkdir()
    (ws.root / "sub" / "deep.py").write_text("print(2)\n", encoding="utf-8")
    (ws.root / ".env").write_text("A=1\n", encoding="utf-8")
    (ws.root / "leak.py").write_text(f'KEY = "{FAKE_AWS}"\n', encoding="utf-8")
    (ws.root / "node_modules").mkdir()
    (ws.root / "node_modules" / "x.js").write_text("x", encoding="utf-8")

    # --- a chat session ---
    out = export_session(session, ws, None, out_root=tmp / "exports")
    convo = (out / "conversation.md").read_text(encoding="utf-8")
    check("the user's request is there", "Build X" in convo)
    check("harness reminders are left out", "harness reminder" not in convo)
    check("secrets are redacted everywhere", FAKE_AWS not in convo
          and FAKE_AWS not in (out / "summary.md").read_text(encoding="utf-8"))
    check("web page text is omitted", "ZEBRA-7731" not in convo and "web page text omitted" in convo)
    # the result is "OK " + 900 "y" = 903 characters; the file content argument is 500
    check("long tool results and arguments are shortened", "(903 chars)" in convo and "(500 chars)" in convo)
    check("a compaction marker is shown", "compacted here" in convo)
    check("summary, todo and sources are included",
          (out / "summary.md").exists() and (out / "todo.md").exists() and (out / "sources.md").exists())
    check("sources are one-liners with their URL",
          "S1: Title with newline - https://a.org/p" in (out / "sources.md").read_text(encoding="utf-8"))

    readme = (out / "README.md").read_text(encoding="utf-8")
    check("the README names the session and the mode", "20261103-100000" in readme and "normal chat" in readme)
    check("the README lists files the tools wrote", "- ok.py" in readme)
    check("the README tells you how to resume", "/resume 20261103-100000" in readme)
    check("workspace files are copied, folders included",
          (out / "workspace" / "ok.py").exists() and (out / "workspace" / "sub" / "deep.py").exists())
    check("credential files are not copied", not (out / "workspace" / ".env").exists())
    check("files containing a secret are not copied", not (out / "workspace" / "leak.py").exists())
    check("junk folders are not copied", not (out / "workspace" / "node_modules").exists())
    check("the README explains each skipped file",
          ".env (looks like a credentials file)" in readme and "leak.py (contains what looks like a secret)" in readme)

    # --- a project session: no workspace copy ---
    out2 = export_session(session, ws, {"name": "demo", "path": "C:\\dev\\demo"}, out_root=tmp / "exports2")
    check("project exports have no workspace copy", not (out2 / "workspace").exists())
    check("the README names the project", "project demo" in (out2 / "README.md").read_text(encoding="utf-8"))

    # --- nothing to export ---
    empty = tmp / "20261103-110000"
    empty.mkdir()
    try:
        export_session(empty, ws, None, out_root=tmp / "exports3")
        raised = False
    except ValueError:
        raised = True
    check("an empty session is refused cleanly", raised)

print("\nAll good.")