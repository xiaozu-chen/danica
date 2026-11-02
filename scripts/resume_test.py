"""
resume_test.py - Checks transcript replay, repair, the session list and a full
live-vs-resumed round trip. No model needed.
Run from the project root:  python -m scripts.resume_test
"""

import json
import tempfile
from pathlib import Path

import danica.compaction as comp
from danica.agent import Agent
from danica.context import ContextTracker
from danica.sessions import list_sessions, load_records, rebuild, repair, resolve_session
from danica.workspace import Workspace


def check(label: str, condition: bool) -> None:
    print(("PASS  " if condition else "FAIL  ") + label)
    assert condition, label


SYS = {"role": "system", "content": "sys"}
GOOD = ("GOAL: build the calc package\n"
        "PROGRESS: wrote calc/m0.py and calc/m1.py, then ran the tests and all of them passed\n"
        "DECISIONS: use unittest from the standard library\n"
        "PROBLEMS: none so far\n"
        "NEXT: summarize the result for the user")


def call(cid: str) -> dict:
    return {"id": cid, "type": "function", "function": {"name": "write_file", "arguments": "{}"}}


def pair(i: int, path: str, result: str) -> list[dict]:
    cid = f"c{i}"
    return [{"role": "assistant", "content": "",
             "tool_calls": [{"id": cid, "type": "function",
                             "function": {"name": "write_file",
                                          "arguments": json.dumps({"path": path, "content": "x"})}}]},
            {"role": "tool", "tool_call_id": cid, "content": result}]


def conversation(start: int, count: int, request: str, result_chars: int) -> list[dict]:
    msgs = [{"role": "user", "content": request}]
    for k in range(start, start + count):
        msgs += pair(k, f"calc/m{k}.py", "OK " + "y" * result_chars)
    return msgs


def write_transcript(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


# ------------------------------------------------------------ load_records
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
    p = Path(tmp) / "t.jsonl"
    p.write_text('{"role": "user", "content": "a"}\n{"role": "user", "cont\n{"role": "user", "content": "b"}\n',
                 encoding="utf-8")
    records, bad = load_records(p)
    check("a corrupt line is skipped, the rest is kept", len(records) == 2 and bad == 1)
    check("a missing file is an empty transcript", load_records(Path(tmp) / "nope.jsonl") == ([], 0))

# ------------------------------------------------------------------ repair
broken = [SYS, {"role": "user", "content": "go"},
          {"role": "assistant", "content": "", "tool_calls": [call("a"), call("b")]},
          {"role": "tool", "tool_call_id": "a", "content": "ok"},      # "b" never got a result
          {"role": "tool", "tool_call_id": "zzz", "content": "stray"}]  # matches no call
fixed_msgs, n = repair(broken)
check("a dangling call gets a placeholder result",
      [m.get("tool_call_id") for m in fixed_msgs if m["role"] == "tool"] == ["a", "b"])
check("the placeholder says the call never finished", "never finished" in fixed_msgs[-1]["content"])
check("two problems were counted (one missing, one stray)", n == 2)
orphan, n = repair([SYS, {"role": "tool", "tool_call_id": "q", "content": "?"}, {"role": "user", "content": "hi"}])
check("an orphan tool result is dropped", [m["role"] for m in orphan] == ["system", "user"] and n == 1)
clean = [SYS, {"role": "user", "content": "go"}] + pair(0, "a.py", "ok")
check("a valid history is left alone", repair(clean) == (clean, 0))

# ----------------------------------------------------------------- rebuild
recs = [{"role": "user", "content": f"m{i}"} for i in range(6)]
recs += [{"event": "compaction", "tail_len": 2, "summary": "[CONTEXT SUMMARY] S", "narrative": "NARR"},
         {"event": "something_unknown"},
         {"role": "user", "content": "after"}]
msgs, narrative, compactions, _ = rebuild(recs, SYS)
check("compaction marker replaces everything but the tail",
      msgs == [SYS, {"role": "user", "content": "[CONTEXT SUMMARY] S"},
               {"role": "user", "content": "m4"}, {"role": "user", "content": "m5"},
               {"role": "user", "content": "after"}])
check("narrative and compaction count are restored", narrative == "NARR" and compactions == 1)
check("unknown event records are ignored", all("event" not in m for m in msgs))

# ------------------------------------- round trip: live session vs resumed one
calls: list[list[dict]] = []


def fake_chat(messages, **kwargs):
    calls.append(messages)
    return {"thinking": "", "content": GOOD}


real_chat = comp.chat
comp.chat = fake_chat
try:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        transcript = Path(tmp) / "transcript.jsonl"
        live = Agent(Workspace(Path(tmp) / "ws"), transcript_path=transcript)
        live.tracker = ContextTracker(4000)

        for m in conversation(0, 10, "Build the calc package please", 1000):
            live._add(m)                                   # _add also writes the transcript
        live.tracker.record(3600, len(live.messages))
        check("live compaction ran", live.compact())
        for m in conversation(10, 2, "Now add a mult function", 100):
            live._add(m)

        records, bad = load_records(transcript)
        resumed = Agent(Workspace(Path(tmp) / "ws2"))      # a brand-new agent, nothing shared
        fixed = resumed.load_history(records)

        check("nothing needed repair", bad == 0 and fixed == 0)
        # messages[0] is the system prompt, which holds a per-agent random canary
        check("resumed messages are IDENTICAL to the live ones", resumed.messages[1:] == live.messages[1:])
        check("compaction count restored", resumed.compactions == 1)
        check("narrative restored for roll-forward", resumed.summary == live.summary)
        check("first request remembered", "Build the calc package please" in resumed.user_requests)
        check("later request remembered", "Now add a mult function" in resumed.user_requests)
        check("written files remembered", "calc/m0.py" in resumed.files_changed)

        # a resumed session can compact again and still knows the old summary
        for m in conversation(12, 10, "Finally add a div function", 1000):
            resumed._add(m)
        resumed.tracker = ContextTracker(4000)
        resumed.tracker.record(3600, len(resumed.messages))
        calls.clear()
        check("a resumed session can compact again", resumed.compact())
        check("the old narrative was rolled into the new summary", "PREVIOUS SUMMARY" in calls[-1][1]["content"])
        content = resumed.messages[1]["content"]
        check("all three requests survive", all(r in content for r in (
            "Build the calc package please", "Now add a mult function", "Finally add a div function")))
finally:
    comp.chat = real_chat

# ------------------------------------------------------ list and resolve
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
    sessions = Path(tmp) / "sessions"
    write_transcript(sessions / "20261101-100000" / "transcript.jsonl", [
        {"role": "user", "content": "[harness reminder, not from the user] tick items"},
        {"role": "user", "content": "First real request"}, {"role": "assistant", "content": "ok"}])
    (sessions / "20261101-110000").mkdir(parents=True)     # empty session: no transcript
    write_transcript(sessions / "20261102-090000" / "transcript.jsonl", [
        {"role": "user", "content": "Second session request"},
        {"event": "compaction", "tail_len": 1, "summary": "[CONTEXT SUMMARY] x"}])
    (sessions / "not-a-session").mkdir()

    infos = list_sessions(sessions)
    check("empty and foreign folders are skipped", [i["id"] for i in infos] == ["20261102-090000", "20261101-100000"])
    check("preview skips harness notes", infos[1]["preview"] == "First real request")
    check("compactions are counted", infos[0]["compactions"] == 1 and infos[1]["compactions"] == 0)

    check("no argument = newest other session",
          resolve_session(sessions, "", current="20261102-090000") == "20261101-100000")
    check("a list number works", resolve_session(sessions, "2", current="x") == "20261101-100000")
    check("a unique prefix works", resolve_session(sessions, "20261101", current="x") == "20261101-100000")

    def refused(arg: str, current: str = "x") -> bool:
        try:
            resolve_session(sessions, arg, current)
            return False
        except ValueError:
            return True

    check("unknown number is refused", refused("9"))
    check("unknown prefix is refused", refused("20300101"))
    check("path tricks are refused", refused("..\\..") and refused("../x") and refused("C:\\"))
    check("the current session is refused", refused("1", current="20261102-090000"))
    check("an empty list is a clean error", refused("", current="20261102-090000") is False)

print("\nAll good.")