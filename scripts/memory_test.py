"""
memory_test.py - Checks pages, scopes, cleaning, id lookup, tolerance for bad
files, the digest and its injection into the model's view. No model needed.
Run from the project root:  python -m scripts.memory_test
"""

import re
import tempfile
from pathlib import Path

import danica.memory as memory
from danica.agent import Agent
from danica.memory import MemoryStore, MemoryStoreError, Page, parse_page, render_page
from danica.workspace import Workspace


def check(label: str, condition: bool) -> None:
    print(("PASS  " if condition else "FAIL  ") + label)
    assert condition, label


def error_of(fn, *args) -> str:
    """The error message if fn raises MemoryStoreError, else ''."""
    try:
        fn(*args)
        return ""
    except MemoryStoreError as e:
        return str(e)


FAKE_AWS = "AKIA" + "IOSFODNN7EXAMPLE"   # split so this file stays clean

with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
    tmp = Path(tmp)
    store = MemoryStore(tmp / "g", tmp / "p", tmp / "c")

    check("empty store: no pages and no digest", store.list_pages() == [] and store.digest() == "")

    # --- adding and the page format ---
    g = store.add("global", "I prefer 4-space indentation")
    text = g.path.read_text(encoding="utf-8")
    check("a page has every frontmatter field",
          all(f"{k}: " in text for k in ("title", "created", "updated", "uuid", "summary", "scope"))
          and text.startswith("---\n"))
    check("the id is the first 8 characters of the uuid", len(g.id) == 8 and g.uuid.startswith(g.id))
    check("the file name is a safe slug", re.fullmatch(r"[a-z0-9-]+\.md", g.path.name) is not None)
    back = parse_page(text, "global", g.path)
    check("a page survives a write/read round trip",
          back is not None and (back.uuid, back.title, back.body, back.created) == (g.uuid, g.title, g.body, g.created))

    tricky = store.add("project", 'Use "quotes": always\nsecond line\n---\nthird')
    reloaded = [p for p in store.list_pages("project") if p.uuid == tricky.uuid][0]
    check("quotes and colons in a title are safe", reloaded.title == 'Use "quotes": always')
    check("multi-line bodies (even with ---) are preserved", "second line\n---\nthird" in reloaded.body)

    # --- cleaning and refusals ---
    check("invisible characters are removed", store.add("chat", "zero\u200bwidth").body == "zerowidth")
    check("secrets are refused", "secret" in error_of(store.add, "global", f"my key is {FAKE_AWS}"))
    check("an empty note is refused", "empty" in error_of(store.add, "global", "  \u200b "))
    check("a too-long note is refused", "limit" in error_of(store.add, "global", "x" * 2001))
    check("a duplicate is refused and names the original", g.id in error_of(store.add, "global", "I prefer 4-space indentation"))
    check("an unknown scope is refused", "scope" in error_of(store.add, "everywhere", "x"))
    check("project scope needs an open project", "project" in error_of(MemoryStore(tmp / "g2").add, "project", "x"))
    check("nothing was saved by the refused notes", len(store.list_pages("global")) == 1)

    # --- precedence order and the digest ---
    d = store.digest()
    check("the digest lists the most specific scope first", d.index("chat:") < d.index("project:") < d.index("global:"))
    check("the digest shows ids and says it is not a request", "(id " in d and "not a new request" in d)

    big = MemoryStore(tmp / "gb")
    for i in range(30):
        big.add("global", f"note number {i} " + "word " * 20)
    check("the digest is capped and says what it left out",
          len(big.digest()) <= memory.DIGEST_CHARS + 120 and "more notes" in big.digest())

    # --- finding by id ---
    check("find by full short id works", store.find(g.id).uuid == g.uuid)
    check("find by a 4-character prefix works", store.find(g.id[:4]).uuid == g.uuid)
    for bad in ["..\\x", "../x", "zz", "abc", "", "C:\\"]:
        check(f"find rejects {bad!r}", error_of(store.find, bad) != "")
    check("an unknown id is an error", "no note" in error_of(store.find, "00000000"))

    # --- ambiguous ids ---
    for n in (1, 2):
        uid = f"aaaaaaaa-0000-4000-8000-00000000000{n}"
        page = Page(uid, f"dup {n}", f"dup {n}", "global", "2026-01-01", "2026-01-01", f"body {n}", tmp / "g" / f"dup{n}.md")
        page.path.write_text(render_page(page), encoding="utf-8")
    check("an ambiguous id is refused", "matches 2" in error_of(store.find, "aaaaaaaa"))
    check("a longer id picks one", store.find("aaaaaaaa000040008000000000000001").title == "dup 1")

    # --- tolerance for bad and hand-edited files ---
    (tmp / "g" / "junk.md").write_text("no frontmatter here", encoding="utf-8")
    (tmp / "g" / "nouuid.md").write_text('---\ntitle: "x"\n---\nbody', encoding="utf-8")
    (tmp / "g" / "huge.md").write_text("x" * 30_000, encoding="utf-8")
    (tmp / "g" / "notes.txt").write_text("ignored", encoding="utf-8")
    (tmp / "g" / "folder.md").mkdir()
    pages = store.list_pages("global")
    check("bad files are skipped and counted, not crashed on", store.skipped == 3 and len(pages) == 3)

    hand = tmp / "g" / "hand.md"
    hand.write_text('---\ntitle: Hand written\nuuid: "bbbbbbbb-0000-4000-8000-000000000001"\n'
                    'tags: [a, b]\nscope: "project"\n---\nA note made by hand\n', encoding="utf-8")
    page = store.find("bbbbbbbb")
    check("hand-written frontmatter with extra keys is accepted", page.title == "Hand written" and page.body == "A note made by hand")
    check("the FOLDER decides the scope, not the scope: field", page.scope == "global")

    # --- delete ---
    store.delete(page)
    check("delete removes the file", not hand.exists() and "bbbbbbbb" not in [p.id for p in store.list_pages()])
    outside = Page("cccccccc-0000-4000-8000-000000000001", "o", "o", "global", "", "", "o", tmp / "outside.md")
    (tmp / "outside.md").write_text("x", encoding="utf-8")
    check("delete refuses a file outside the memory folder", "not inside" in error_of(store.delete, outside)
          and (tmp / "outside.md").exists())

    # --- the page limit ---
    real_max = memory.MAX_PAGES
    memory.MAX_PAGES = 2
    try:
        small = MemoryStore(tmp / "gs")
        small.add("global", "one")
        small.add("global", "two")
        check("a full scope refuses more notes", "full" in error_of(small.add, "global", "three"))
    finally:
        memory.MAX_PAGES = real_max

    # --- what the model sees ---
    agent = Agent(Workspace(tmp / "ws"))
    check("no memory attached: the history is passed through untouched", agent._messages_for_model() is agent.messages)

    agent.memory = store
    sent = agent._messages_for_model()
    check("the digest is appended and the history itself is not changed",
          len(sent) == len(agent.messages) + 1 and "Saved memory" in sent[-1]["content"] and len(agent.messages) == 1)

    agent.memory = MemoryStore(tmp / "empty")
    check("an empty store adds nothing", agent._messages_for_model() is agent.messages)

    (tmp / "TODO.md").write_text("Goal: g\n1. [ ] a\n", encoding="utf-8")
    agent2 = Agent(Workspace(tmp / "ws2", todo_path=tmp / "TODO.md"))
    agent2.memory = store
    sent = agent2._messages_for_model()
    check("memory comes before the TODO, and the TODO stays last",
          "Saved memory" in sent[-2]["content"] and "TODO.md" in sent[-1]["content"])
    check("a not-found id names the open scopes", "open scopes (chat, project, global)" in error_of(store.find, "00000000"))

print("\nAll good.")