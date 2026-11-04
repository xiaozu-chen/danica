"""
phase6_exit.py - Phase 6 exit test: a preference stated in one session is used in a later
one, and forget leaves no trace.

Offline: a scripted fake model and a fake embedding server. Everything else is real
(extraction pipeline, proposal queue, memory store, FTS5 + vector index, recall, forget).
Run from the project root:  python -m scripts.phase6_exit
"""

import tempfile
from pathlib import Path

import danica.agent as agent_mod
import danica.capture as capture
from danica.agent import Agent
from danica.capture import ProposalQueue
from danica.forget import forget
from danica.memory import MemoryStore
from danica.memory_index import MemoryIndex
from danica.tools import run_tool
from danica.workspace import Workspace
from scripts.fakes import FakeEmbedder


def check(label: str, condition: bool) -> None:
    print(("PASS  " if condition else "FAIL  ") + label)
    assert condition, label


SENTENCE = "From now on I always use tabs for indentation, never spaces, in my kumquat project."
NOTE = "I always use tabs for indentation, never spaces, in my kumquat project."

real_chat, real_stream = capture.chat, agent_mod.stream_chat
capture.chat = lambda messages, **kw: {"thinking": "", "content": "global: " + NOTE}
agent_mod.stream_chat = lambda messages, **kw: iter([{"type": "content", "text": "Noted."}])
try:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        tmp = Path(tmp)
        db = tmp / "facts.db"
        embedder = FakeEmbedder()

        def open_session(sid: str):
            """A brand-new session: new agent, new store objects, same folders and database."""
            (tmp / "sessions" / sid).mkdir(parents=True, exist_ok=True)
            store = MemoryStore(tmp / "g", None, tmp / "sessions" / sid / "memory")
            store.index = MemoryIndex(db, embedder=embedder, min_similarity=0.5)
            ws = Workspace(tmp / "ws" / sid)
            ws.memory = store
            agent = Agent(ws, transcript_path=tmp / "sessions" / sid / "transcript.jsonl")
            agent.memory = store
            agent.proposals = ProposalQueue(tmp / "sessions" / sid / "proposals.json")
            return ws, store, agent

        # ============ session A: the user states a preference ============
        ws_a, store_a, agent_a = open_session("sA")
        agent_a.run(SENTENCE)
        check("the preference was PROPOSED", agent_a.proposals.count() == 1)
        check("nothing is saved without the user's approval", store_a.list_pages() == [])

        item = agent_a.proposals.items()[0]                      # the user reviews and says "y"
        page = store_a.add(item["scope"], item["text"], source="captured", confidence="0.8")
        agent_a.proposals.remove(item["id"])
        check("approving it saves a captured global note",
              store_a.list_pages()[0].source == "captured" and page.scope == "global")

        # ============ session B: a later session uses it ============
        ws_b, store_b, agent_b = open_session("sB")
        hits = agent_b._auto_recall("which whitespace style do I like")
        check("a paraphrase finds the note by meaning in the new session", len(hits) == 1 and hits[0][0] == page.id)
        agent_b.recalled = hits
        view = agent_b._messages_for_model()[-1]["content"]
        check("the note reaches the model's context", "kumquat" in view and "tabs" in view)
        check("the recall tool finds it too",
              "kumquat" in run_tool("recall", {"query": "which whitespace style do I like"}, ws_b))
        check("the database file holds the note before forgetting", b"kumquat" in db.read_bytes())

        # ============ forget it ============
        report = forget(store_b, store_b.find(page.id), roots=[tmp / "sessions"])
        problems = "; ".join(f"{n}: {d}" for n, ok, d in report.checks if not ok)
        check("forget verified clean" + (f" (PROBLEMS: {problems})" if problems else ""), report.ok)
        check("the note file is gone", not page.path.exists())
        check("the database file holds no trace of it",
              b"kumquat" not in db.read_bytes() and b"tabs for indentation" not in db.read_bytes())
        check("forget names the transcript that still quotes it", any("sA" in m for m in report.mentions))

        # ============ session C: it is really gone ============
        ws_c, store_c, agent_c = open_session("sC")
        check("auto-recall finds nothing", agent_c._auto_recall("which whitespace style do I like") == [])
        check("the recall tool finds nothing",
              "No saved notes" in run_tool("recall", {"query": "which whitespace style do I like"}, ws_c))
        check("no memory block is sent to the model", agent_c._messages_for_model() is agent_c.messages)
finally:
    capture.chat, agent_mod.stream_chat = real_chat, real_stream

print("\nAll good. Phase 6 exit test passed.")