"""
cite_test.py - Checks citation verification, the Sources notice, source
persistence (stable ids across /resume) and the agent hook. No model needed.
Run from the project root:  python -m scripts.cite_test
"""

import json
import tempfile
from pathlib import Path

import danica.agent as agent_mod
from danica.agent import Agent
from danica.citations import build_notice, verify
from danica.compaction import build_message
from danica.web import WebState, state_for
from danica.workspace import Workspace


def check(label: str, condition: bool) -> None:
    print(("PASS  " if condition else "FAIL  ") + label)
    assert condition, label


def make_state(path=None) -> WebState:
    st = WebState(path)
    st.add("https://a.org/pelican", "https://a.org/pelican", "Pelican facts", ["p one", "p two", "p three"], [])
    st.add("https://b.org/diet", "https://www.b.org/diet", "Diet page", ["only one"], [])
    return st


# ------------------------------------------------------------------ verify
st = make_state()
r = verify("Pelicans eat fish [S1.2]. They dive [S2.1].", st)
check("valid citations are accepted, in order", r.cited == ["S1", "S2"] and not r.invalid and r.count == 2)
r = verify("A [S1.1, S1.3] and B [S1.2-3]", st)
check("lists and ranges work", r.cited == ["S1"] and r.count == 3 and not r.invalid)
check("a whole-source citation works", verify("see [S1]", st).cited == ["S1"])
r = verify("x [S9.1] y [S1.99] z [S2.2] w [S1.3-5]", st)
check("unknown sources and out-of-range passages are flagged",
      r.invalid == ["S9.1", "S1.99", "S2.2", "S1.3-5"] and not r.cited)
check("ordinary brackets are ignored",
      not verify("array [1] and [note] and [Section 3]", st).invalid)
check("code is ignored", not verify("```\nx = grid[S9]\n```\nand `a[S8]`", st).invalid)
check("no state means every citation is invalid", verify("[S1.1]", None).invalid == ["S1.1"])
check("first-cited order, no duplicates", verify("[S2.1] [S1.1] [S2.1]", st).cited == ["S2", "S1"])

r = verify("See https://a.org/pelican, and https://www.b.org/diet. Also https://evil.test/x "
           "and https://typed.org/y.", st, ignore_text="please check https://typed.org/y")
check("only the unfetched, untyped URL is flagged", r.unfetched_urls == ["https://evil.test/x"])

# ------------------------------------------------------------------ notice
r = verify("[S1.1] [S9.1]", st)
text = build_notice(r, st, web_used=True)
check("the notice lists fetched sources with their URLs", "[S1] Pelican facts - https://a.org/pelican" in text)
check("the notice warns about invalid ids", "S9.1" in text and "WARNING" in text)
check("uncited sources are not listed", "[S2]" not in text)
check("a clean answer has no warning", "WARNING" not in build_notice(verify("[S1.1]", st), st, True))
check("web used but nothing cited warns",
      "cites no passage ids" in build_notice(verify("plain answer", st), st, True))
check("no web and no citations: silence", build_notice(verify("plain answer", st), st, False) == "")

# ------------------------------------------------------------- persistence
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
    path = Path(tmp) / "sources.json"
    make_state(path)
    check("sources are saved without leftovers", path.exists() and not (Path(tmp) / "sources.tmp").exists())

    again = WebState(path)
    check("ids and passages survive a reload",
          list(again.by_id) == ["S1", "S2"] and again.by_id["S1"].passages == ["p one", "p two", "p three"])
    check("hosts of loaded pages are known", {"a.org", "b.org"} <= again.known_hosts)
    check("numbering continues after a reload",
          again.add("https://c.org/", "https://c.org/", "C", ["x"], []).id == "S3")
    s1 = again.add("https://a.org/pelican", "https://a.org/pelican", "Pelican v2", ["new"], [])
    check("re-fetching a known URL keeps its id", s1.id == "S1" and again.counter == 3)
    check("a fresh load sees the update", WebState(path).by_id["S1"].passages == ["new"])

    path.write_text("{not json", encoding="utf-8")
    check("a corrupt file gives an empty state", WebState(path).by_id == {})
    path.write_text(json.dumps({"counter": 5, "sources": [
        {"id": "evil", "url": "u", "passages": []}, {"id": "S2", "url": "u2", "passages": [1]}]}), encoding="utf-8")
    check("entries that don't look right are skipped", WebState(path).by_id == {})
    path.write_text(json.dumps({"counter": 1, "sources": [{"bad": 1}]}), encoding="utf-8")
    check("unknown fields give an empty state", WebState(path).by_id == {})

    ws1 = Workspace(Path(tmp) / "ws1", sources_path=Path(tmp) / "src2.json")
    state_for(ws1).add("https://a.org/", "https://a.org/", "A", ["p"], [])
    ws2 = Workspace(Path(tmp) / "ws2", sources_path=Path(tmp) / "src2.json")
    check("a new workspace object (a resumed session) sees the same sources",
          state_for(ws2).by_id["S1"].url == "https://a.org/")
    check("without a sources path nothing is written", Workspace(Path(tmp) / "ws3").sources_path is None)

# -------------------------------------------------------------- compaction
msg = build_message("n" * 200, ["a request"], [], [], ["S1: https://a.org/pelican"])
check("the summary lists web sources (id and URL)", "S1: https://a.org/pelican" in msg["content"])
check("no sources, no section", "WEB SOURCES" not in build_message("n" * 200, ["r"], [], [])["content"])

# ------------------------------------------------------------- agent hook
real_stream, real_run_tool = agent_mod.stream_chat, agent_mod.run_tool
try:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        ws = Workspace(Path(tmp) / "agent_ws")
        state_for(ws).add("https://a.org/pelican", "https://a.org/pelican", "Pelican facts", ["p one", "p two"], [])
        events: list = []
        agent = Agent(ws, on_event=lambda kind, data: events.append((kind, data)))

        agent_mod.stream_chat = lambda messages, **kw: iter(
            [{"type": "content", "text": "Pelicans eat fish [S1.2] and [S7.1]."}])
        agent.run("what do pelicans eat?")
        notes = [d for k, d in events if k == "citations"]
        check("the agent shows a verified Sources list and flags the bad id",
              len(notes) == 1 and "[S1] Pelican facts" in notes[0] and "S7.1" in notes[0])
        check("the notice is not stored in the conversation",
              all(notes[0] not in str(m.get("content", "")) for m in agent.messages))

        turn = {"n": 0}

        def search_stream(messages, **kw):
            turn["n"] += 1
            if turn["n"] == 1:
                yield {"type": "tool_calls", "calls": [{"id": "c1", "name": "web_search",
                                                        "arguments": json.dumps({"query": "pelicans"})}]}
            else:
                yield {"type": "content", "text": "They eat fish."}

        agent_mod.stream_chat = search_stream
        agent_mod.run_tool = lambda name, args, ws: "Search results for pelicans ..."
        events.clear()
        agent.run("search for pelicans")
        check("web used without citations warns",
              any("cites no passage ids" in d for k, d in events if k == "citations"))

        agent_mod.stream_chat = lambda messages, **kw: iter([{"type": "content", "text": "fine"}])
        events.clear()
        agent.run("hello again")
        check("web_used resets for every request", not any(k == "citations" for k, _ in events))
finally:
    agent_mod.stream_chat, agent_mod.run_tool = real_stream, real_run_tool

print("\nAll good.")