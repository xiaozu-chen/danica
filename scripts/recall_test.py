"""
recall_test.py - Checks query cleaning, the index (sync, search, scope isolation,
deletes), the recall tool and auto-recall in the agent. No model needed.
Run from the project root:  python -m scripts.recall_test
"""

import sqlite3
import tempfile
from pathlib import Path

import danica.agent as agent_mod
from danica.agent import Agent
from danica.memory import MemoryStore, Page, render_page
from danica.memory_index import MemoryIndex, query_terms
from danica.permissions import Tier, classify
from danica.pruning import make_stub
from danica.tools import run_tool
from danica.workspace import Workspace


def check(label: str, condition: bool) -> None:
    print(("PASS  " if condition else "FAIL  ") + label)
    assert condition, label


# --- this Python's SQLite must have FTS5 with the porter stemmer ---
try:
    sqlite3.connect(":memory:").execute("CREATE VIRTUAL TABLE t USING fts5(x, tokenize='porter unicode61')")
    fts5 = True
except sqlite3.OperationalError:
    fts5 = False
check("this Python's SQLite has FTS5", fts5)

# --- query cleaning ---
check("stop words and short words are dropped",
      query_terms("What do you know about my coding preferences?") == ["know", "coding", "preferences"])
check("FTS5 syntax characters never survive",
      query_terms('x" OR 1=1 -- NEAR(a b) title:secret * ^') == ["near", "title", "secret"])
check("only stop words means no terms", query_terms("the and of") == [])
check("duplicates collapse", query_terms("pelican pelican") == ["pelican"])
check("the number of terms is capped", len(query_terms(" ".join(f"word{i}" for i in range(30)), 5)) == 5)

with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
    tmp = Path(tmp)
    db_path = tmp / "facts.db"
    index = MemoryIndex(db_path)

    def make_store(project: str, session: str) -> MemoryStore:
        """A store laid out like the real one: projects/<id>/memory, sessions/<id>/memory."""
        s = MemoryStore(tmp / "g", tmp / "projects" / project / "memory", tmp / "sessions" / session / "memory")
        s.index = index
        return s

    a = make_store("A", "s1")
    a.add("global", "I prefer 4-space indentation and type hints")
    a.add("project", "This project uses unittest, not pytest")
    a.add("chat", "Today I am only fixing bugs")

    check("owners follow the folder convention",
          a.owner("global") == "" and a.owner("project") == "A" and a.owner("chat") == "s1")

    # --- sync ---
    check("the first sync adds every note", index.sync(a) == {"added": 3, "updated": 0, "removed": 0})
    check("a second sync changes nothing", index.sync(a) == {"added": 0, "updated": 0, "removed": 0})

    # --- search ---
    hits = index.recall(a, "which unittest or pytest do we use")
    check("keyword search finds the right note first", bool(hits) and hits[0].scope == "project")
    hits = index.recall(a, "indentation preferences")
    check("stemming: 'preferences' finds 'prefer'", bool(hits) and hits[0].scope == "global")
    check("a query with no searchable words gives no hits", index.recall(a, "the and of") == [])
    check("a query with no match gives no hits", index.recall(a, "xylophone quasar") == [])
    check("the count is clamped to 1..8",
          len(index.recall(a, "project indentation bugs", 100)) <= 8
          and len(index.recall(a, "project indentation bugs", 0)) == 1)

    # --- scope isolation: the same database serves other projects and sessions ---
    a.add("project", "pelican alpha launch code name")
    a.add("chat", "session one only zebra")
    b = make_store("B", "s2")
    check("another project cannot see this project's notes", index.recall(b, "pelican alpha") == [])
    check("another session cannot see this session's chat notes", index.recall(b, "zebra") == [])
    check("global notes are shared by every store", index.recall(b, "indentation")[0].scope == "global")
    check("syncing one store never deletes another's rows", index.recall(a, "pelican alpha")[0].scope == "project")

    # --- edits and deletes ---
    note = a.add("project", "temporary kiwi note")
    index.sync(a)
    text = note.path.read_text(encoding="utf-8")
    note.path.write_text(text.replace("kiwi", "mango"), encoding="utf-8")
    counts = index.sync(a)
    check("a hand edit is picked up", counts["updated"] == 1 and bool(index.recall(a, "mango"))
          and not index.recall(a, "kiwi"))

    a.delete(a.find(note.id))
    counts = index.sync(a)
    check("a deleted note leaves the index", counts["removed"] == 1 and not index.recall(a, "mango"))
    raw = sqlite3.connect(db_path)
    left = raw.execute("SELECT count(*) FROM facts_fts WHERE facts_fts MATCH 'mango'").fetchone()[0]
    left += raw.execute("SELECT count(*) FROM facts WHERE body LIKE '%mango%'").fetchone()[0]
    raw.close()
    check("nothing about it is left in the database tables", left == 0)

    # --- a copied file (same uuid in two scopes) must not make the index flip-flop ---
    global_page = a.list_pages("global")[0]
    copy = tmp / "projects" / "A" / "memory" / "copy.md"
    copy.write_text(global_page.path.read_text(encoding="utf-8"), encoding="utf-8")
    index.sync(a)
    check("a copied file does not cause endless updates", index.sync(a) == {"added": 0, "updated": 0, "removed": 0})
    copy.unlink()
    index.sync(a)

    # --- the recall tool ---
    check("a new workspace has no memory attached", Workspace(tmp / "w0").memory is None)
    ws = Workspace(tmp / "ws")
    check("recall without memory is an error", run_tool("recall", {"query": "x"}, ws).startswith("ERROR"))
    ws.memory = a
    out = run_tool("recall", {"query": "which unittest do we use"}, ws)
    check("the tool returns scope, id and the note text", "[project, id " in out and "unittest" in out)
    check("a query with no searchable words explains itself", "searchable words" in run_tool("recall", {"query": "the of"}, ws))
    check("no match says so", "No saved notes" in run_tool("recall", {"query": "xylophone quasar"}, ws))
    check("a bad count is an error", run_tool("recall", {"query": "unittest", "count": "many"}, ws).startswith("ERROR"))
    check("the raw query is never echoed back",
          'NEAR' not in run_tool("recall", {"query": 'xylophone" NEAR(a b)'}, ws))
    check("recall is a free READ-tier tool", classify("recall", {})[0] == Tier.READ)
    check("pruned recall results keep the query",
          make_stub("recall", {"query": "indent"}, "line\n" * 50).startswith("[pruned] recall 'indent'"))

    # --- auto-recall in the agent ---
    agent = Agent(Workspace(tmp / "ws2"))
    agent.memory = a
    lines = agent._auto_recall("which unittest or pytest do we use")
    check("auto-recall returns relevant notes", bool(lines) and "[project, id " in lines[0][1])
    agent.recalled = lines
    block = agent._messages_for_model()[-1]["content"]
    check("notes the digest already shows are not repeated", "Also relevant" not in block)

    # 40 newer notes push an old, relevant one out of the digest
    big = MemoryStore(tmp / "gbig")
    big.index = index
    for i in range(40):
        big.add("global", f"filler note {i} " + "padding " * 18)
    old = Page("dddddddd-0000-4000-8000-000000000001", "zebra crossing rules", "zebra crossing rules",
               "global", "2000-01-01", "2000-01-01", "zebra crossing rules", tmp / "gbig" / "old.md")
    old.path.write_text(render_page(old), encoding="utf-8")

    agent2 = Agent(Workspace(tmp / "ws3"))
    agent2.memory = big
    agent2.recalled = agent2._auto_recall("tell me about zebra crossing")
    block = agent2._messages_for_model()[-1]["content"]
    check("a relevant note the digest left out is added",
          "Also relevant" in block and "dddddddd" in block and "more notes" in block)

    # --- memory trouble must never stop a request ---
    (tmp / "blocker").write_text("x", encoding="utf-8")        # a FILE where the DB's folder should be
    broken = MemoryStore(tmp / "gb2")
    broken.add("global", "a note")
    broken.index = MemoryIndex(tmp / "blocker" / "x.db")
    agent3 = Agent(Workspace(tmp / "ws4"))
    agent3.memory = broken
    check("a broken index gives no results instead of crashing", agent3._auto_recall("a note") == [])

    real_stream = agent_mod.stream_chat
    agent_mod.stream_chat = lambda messages, **kw: iter([{"type": "content", "text": "ok"}])
    try:
        check("a request still completes when the memory index is broken", agent3.run("hello there") == "ok")
    finally:
        agent_mod.stream_chat = real_stream

print("\nAll good.")