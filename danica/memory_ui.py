"""
memory_ui.py - The /memory command and its dialogs, with no terminal code in it.

Ported from cli.py (forget_note, review_proposals, merge_notes, handle_memory). Output and
questions go through the `ui` object (danica/ui.py). review_proposals, merge_notes and the
forget confirmation BLOCK on the user: call from a worker/engine thread under the TUI.
"""

from __future__ import annotations

from typing import Any

from danica.capture import ProposalQueue, extract
from danica.compaction import harvest
from danica.config import CONFIG
from danica.forget import forget
from danica.memory import SCOPES, MemoryStore, MemoryStoreError

USAGE = ("usage: /memory [list | add [scope] <text> | review | capture | search <words> | "
         "merge | forget <id> | show <id> | status | reindex | path]")


def forget_note(store: MemoryStore, page: Any, ui: Any) -> bool:
    """Forget one note for real and report every verification check. True if all passed."""
    report = forget(store, page)
    for name, ok, detail in report.checks:
        ui.log("ok" if ok else "fail", f"  {name}" + (f":{detail}" if detail else ""))
    if report.mentions:
        ui.log("warning", f"  Its text still appears in {len(report.mentions)} file(s) that forget does "
                          f"not edit (transcripts, summaries, exports):")
        lines = ["  " + m for m in report.mentions[:5]]
        if len(report.mentions) > 5:
            lines.append(f"  ... and {len(report.mentions) - 5} more")
        ui.say("\n".join(lines))
    ui.say("The audit log and any backups of your AppData folder are never edited.", style="dim")
    return report.ok


def review_proposals(
    store: MemoryStore, queue: ProposalQueue, audit: Any, ui: Any
) -> None:
    """/memory review: go through proposed notes one at a time."""
    items = queue.items()
    if not items:
        ui.log("info", "  No proposed notes")
        return
    for item in items:
        scope, text = item["scope"], item["text"]
        if scope not in store.dirs:
            ui.say(f"[{scope}] {text}\n  (left for later: {scope} memory is not open here)", style="dim")
            continue
        answer = ui.choose(f"Proposed note [{scope}]", text,
                           [("y", "Save"), ("e", "Edit"), ("n", "Drop"), ("s", "Skip"), ("q", "Stop")])
        if answer is None or answer == "q":
            return
        edited = False
        if answer == "e":
            new_text = ui.ask_text("Edit the note", default=text)
            if new_text is None:
                continue                                   # dismissed: leave it in the queue
            text, answer, edited = new_text.strip(), "y", True
        if answer == "n":
            queue.remove(item["id"])
        elif answer == "y":
            try:
                # A note you edited is your own wording: full confidence, source "user"
                page = store.add(scope, text, source="user" if edited else "captured",
                                 confidence="1.0" if edited else "0.8")
            except MemoryStoreError as e:
                ui.log("error", f"  {e}")
                queue.remove(item["id"])
                continue
            queue.remove(item["id"])
            audit.append("memory_add", scope=scope, id=page.id, source=page.source)
            ui.log("ok", f"  Saved to {scope} memory (id {page.id})")
        # anything else (s) leaves it in the queue


def merge_notes(store: MemoryStore, audit: Any, ui: Any) -> None:
    """/memory merge: pairs of near-duplicate notes (same scope, almost the same meaning)."""
    if store.index is None:
        ui.log("info", "  Search is not available in this session")
        return
    pairs = store.index.near_duplicates(store, CONFIG["embeddings"]["merge_similarity"], 10)
    if not pairs:
        ui.log("info", "  No near-duplicate notes found")
        return
    for sim, ua, ub in pairs:
        a, b = store.get(ua), store.get(ub)
        if a is None or b is None:
            continue                       # one of them was already dealt with in this loop
        answer = ui.choose(f"These two {a.scope} notes look alike (similarity {sim:.2f})",
                           f"1. {a.body}\n\n2. {b.body}",
                           [("1", "Keep first"), ("2", "Keep second"), ("m", "Write one merged note"),
                            ("s", "Skip"), ("q", "Stop")])
        if answer is None or answer == "q":
            return
        if answer == "1":
            drop = [b]
        elif answer == "2":
            drop = [a]
        elif answer == "m":
            merged = ui.ask_text("Merged note", default=a.body)
            if merged is None:
                continue
            try:
                page = store.add(a.scope, merged.strip())      # saved FIRST: a refusal loses nothing
            except MemoryStoreError as e:
                ui.log("error", f"  {e}")
                continue
            audit.append("memory_add", scope=a.scope, id=page.id, source="merge")
            drop = [a, b]
        else:
            continue
        for old in drop:
            ok = forget_note(store, old, ui)
            audit.append("memory_forget", scope=old.scope, id=old.id, ok=ok, reason="merge")


def handle_memory(
    text: str, store: MemoryStore, audit: Any, in_project: bool, agent: Any, ui: Any
) -> None:
    """The /memory command. Sub-commands: list, add, review, capture, show, forget (delete),
    merge, search, status, reindex, path."""
    parts = text.split(maxsplit=2)               # "/memory", the sub-command, then the rest
    sub = parts[1].lower() if len(parts) > 1 else "list"
    rest = parts[2] if len(parts) > 2 else ""
    try:
        if sub == "list":
            pages = store.list_pages()
            if not pages:
                ui.log("info", "  No saved memory yet. Try: /memory add <text>")
            lines = [f"{p.id}  {p.scope:<7} {p.source:<8} {p.updated[:10]}  {p.summary}" for p in pages]
            if lines:
                ui.say("\n".join(lines))
            if store.skipped:
                ui.say(f"({store.skipped} file(s) in the memory folders were skipped: not valid "
                       f"notes, or too big)", style="dim")
            if agent.proposals and agent.proposals.count():
                ui.log("warning", f"({agent.proposals.count()} proposed note(s) waiting: /memory review)")

        elif sub == "add":
            words = rest.split(maxsplit=1)
            if words and words[0].lower() in SCOPES:          # /memory add global <text>
                scope, body = words[0].lower(), (words[1] if len(words) > 1 else "")
            else:                                             # default: project if open, else global
                scope, body = ("project" if in_project else "global"), rest
            page = store.add(scope, body)
            audit.append("memory_add", scope=scope, id=page.id)   # the text itself is never logged
            ui.log("ok", f"  Saved to {scope} memory (id {page.id})")

        elif sub == "review":
            review_proposals(store, agent.proposals, audit, ui)

        elif sub == "capture":
            recent = harvest(agent.messages)[0][-5:]          # your last requests, verbatim
            if not recent:
                ui.log("info", "  Nothing to look at yet: send a request first")
            else:
                added = 0
                try:
                    for request in recent:
                        for scope, note in extract(request, store):
                            added += agent.proposals.add(scope, note)
                except Exception as e:                        # most likely: llama-server is down
                    ui.log("error", f"  Could not ask the model ({type(e).__name__})")
                ui.log("info", f"  {added} note(s) proposed. /memory review shows them.")

        elif sub == "show":
            page = store.find(rest)
            ui.say(f"[{page.scope}] {page.title}\nid {page.id}   source {page.source}   "
                   f"confidence {page.confidence}\ncreated {page.created}   updated {page.updated}"
                   f"\n\n{page.body}")

        elif sub in ("forget", "delete"):
            page = store.find(rest)
            sure = ui.confirm(f"Forget [{page.scope}] note for good?", body=page.summary,
                              yes="Forget it", no="Keep")
            if sure:
                ok = forget_note(store, page, ui)
                audit.append("memory_forget", scope=page.scope, id=page.id, ok=ok)
            else:
                ui.log("info", "  Kept")

        elif sub == "merge":
            merge_notes(store, audit, ui)

        elif sub == "search":
            if store.index is None:
                ui.log("info", "  Search is not available in this session")
            else:
                hits = store.index.recall(store, rest, 5)
                lines = []
                for h in hits:
                    sim = f"{h.sim:.2f}" if h.sim is not None else " - "
                    lines.append(f"{h.id}  {h.scope:<7} {h.via:<8} sim {sim}  {h.summary}")
                if lines:
                    ui.say("\n".join(lines))
                if not hits:
                    ui.log("info", "  No matching notes")
                    if store.index.nearest is not None:
                        ui.say(f"(closest by meaning: {store.index.nearest:.2f}, below the "
                               f"{store.index.min_similarity:.2f} threshold)", style="dim")
                if store.index.note:
                    ui.log("warning", f"keyword search only: {store.index.note}")

        elif sub == "status":
            if store.index is None:
                ui.say("search is not available in this session")
            else:
                ui.say("\n".join(store.index.status_lines(store)))

        elif sub == "reindex":
            if store.index is None:
                ui.say("search is not available in this session")
            else:
                n = store.index.reindex()
                ui.say(f"cleared {n} vector(s); they are rebuilt on the next search")

        elif sub == "path":
            ui.say("\n".join(f"{scope:<8} {folder}" for scope, folder in store.dirs.items()))

        else:
            ui.say(USAGE)
    except MemoryStoreError as e:
        ui.log("error", f"{e}")
