"""
forget_test.py - Checks that forget really removes a note everywhere and verifies it,
plus near-duplicate detection. No model needed, but sqlite-vec must be installed.
Run from the project root:  python -m scripts.forget_test
"""

import json
import tempfile
from pathlib import Path

from danica.forget import find_mentions, forget
from danica.memory import MemoryStore, MemoryStoreError
from danica.memory_index import MemoryIndex, _shred
from scripts.fakes import FakeEmbedder


def check(label: str, condition: bool) -> None:
    print(("PASS  " if condition else "FAIL  ") + label)
    assert condition, label


with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
    tmp = Path(tmp)

    # --- the shredder ---
    f = tmp / "z.bin"
    f.write_bytes(b"secret data" * 1000)
    _shred(f)
    check("shred zeroes a file in place", f.stat().st_size == 11000 and f.read_bytes() == b"\0" * 11000)

    # --- forgetting a note ---
    db = tmp / "facts.db"
    idx = MemoryIndex(db, embedder=FakeEmbedder(), min_similarity=0.5)
    a = MemoryStore(tmp / "g", tmp / "projects" / "A" / "memory", tmp / "sessions" / "s1" / "memory")
    a.index = idx
    keep = a.add("global", "I prefer 4-space indentation and type hints")
    other = a.add("project", "The deploy target is a Raspberry Pi running Debian")
    victim = a.add("project", "My kumquat orchard notes: tabs are forbidden in the kumquat repo")
    idx.recall(a, "kumquat repo")        # indexes every note and embeds it
    check("before: the word is in the database file", b"kumquat" in db.read_bytes())

    sessions = tmp / "sessions"
    (sessions / "s9").mkdir(parents=True)
    (sessions / "s9" / "transcript.jsonl").write_text(
        json.dumps({"role": "user", "content": "remember " + victim.body}) + "\n", encoding="utf-8")

    report = forget(a, victim, roots=[sessions])
    problems = "; ".join(f"{n}: {d}" for n, ok, d in report.checks if not ok)
    check("forget verified clean" + (f" (PROBLEMS: {problems})" if problems else ""), report.ok)
    check("the report has all six checks", len(report.checks) == 6)
    check("the note file is gone", not victim.path.exists())
    raw = db.read_bytes()
    check("the database file no longer holds its words", b"kumquat" not in raw and b"orchard" not in raw)
    check("no stray rebuild file is left behind", not (tmp / "facts.db.new").exists())
    check("other notes still work after the rebuild",
          idx.recall(a, "raspberry deploy")[0].scope == "project" and idx.recall(a, "indentation")[0].scope == "global")
    check("forget names transcripts that still quote the note", any("s9" in m for m in report.mentions))

    # --- a copied file shares the uuid: forget removes every copy ---
    twin = a.add("global", "Twin note about zucchini bread")
    idx.recall(a, "zucchini")
    copy = a.dirs["global"] / "copy.md"
    copy.write_text(twin.path.read_text(encoding="utf-8"), encoding="utf-8")
    check("a copied file shares the uuid", len(a.pages_with_uuid(twin.uuid)) == 2)
    report = forget(a, twin, roots=[])
    check("forgetting removes every copy", report.ok and a.pages_with_uuid(twin.uuid) == [] and not copy.exists())

    # --- no index: only the file checks run ---
    bare = MemoryStore(tmp / "bg")
    note = bare.add("global", "A plain note without an index")
    r = forget(bare, note, roots=[])
    check("without an index only the file checks run", r.ok and not note.path.exists() and len(r.checks) == 2)

    # --- a failed rebuild is reported, not hidden ---
    doomed = a.add("global", "Another note about papaya smoothies")
    idx.recall(a, "papaya")
    real_rebuild = idx.rebuild_file

    def failing() -> None:
        raise MemoryStoreError("disk is busy")

    idx.rebuild_file = failing
    try:
        r = forget(a, doomed, roots=[])
    finally:
        idx.rebuild_file = real_rebuild
    check("a failed rebuild is reported", not r.ok and any("disk is busy" in d for _, ok, d in r.checks if not ok))

    # --- mentions ---
    check("too-short notes are not searched for", find_mentions("tabs ok", [sessions]) == [])
    check("matching ignores case and spacing",
          find_mentions("MY  kumquat   orchard NOTES: tabs are forbidden in the kumquat repo", [sessions]) != [])
    check("a missing folder is fine", find_mentions("long enough text to search for", [tmp / "nope"]) == [])

    # --- near-duplicates ---
    nd_idx = MemoryIndex(tmp / "nd.db", embedder=FakeEmbedder(), min_similarity=0.5)
    n = MemoryStore(tmp / "ng", tmp / "projects" / "N" / "memory", tmp / "sessions" / "sn" / "memory")
    n.index = nd_idx
    p1 = n.add("project", "indentation tabs policy")
    p2 = n.add("project", "tabs indentation policy rules")
    n.add("project", "raspberry deploy target")
    n.add("chat", "indentation tabs policy")           # same words, different scope
    pairs = nd_idx.near_duplicates(n, 0.88)
    check("only the two near-identical project notes are paired",
          len(pairs) == 1 and {pairs[0][1], pairs[0][2]} == {p1.uuid, p2.uuid} and pairs[0][0] > 0.99)
    check("a stricter threshold finds nothing", nd_idx.near_duplicates(n, 1.01) == [])

    n2 = MemoryStore(tmp / "ng", tmp / "projects" / "M" / "memory", tmp / "sessions" / "sm" / "memory")
    n2.index = nd_idx
    check("another project does not see these notes", nd_idx.near_duplicates(n2, 0.88) == [])

    try:
        MemoryIndex(tmp / "plain.db").near_duplicates(n, 0.88)
        raised = False
    except MemoryStoreError:
        raised = True
    check("without an embedding server it says so", raised)

print("\nAll good.")