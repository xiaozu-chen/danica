"""Incremental local knowledge index and citation tests; no model required."""

import tempfile
import time
import re
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from reportlab.pdfgen import canvas

from danica.knowledge import KnowledgeError, KnowledgeIndex, verify_citations
from danica.workspace import Workspace


def check(label: str, condition: bool) -> None:
    print(("PASS  " if condition else "FAIL  ") + label)
    assert condition, label


with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
    base = Path(tmp)
    vault = base / "vault"
    vault.mkdir()
    note = vault / "meeting.md"
    note.write_text(
        "# Retention\n\nThe retention policy keeps backups for 30 days.\n\n"
        "## Cancellation\n\nCancel the subscription from account settings.\n",
        encoding="utf-8",
    )
    hidden = vault / ".obsidian"
    hidden.mkdir()
    (hidden / "workspace.json").write_text("private editor state", encoding="utf-8")
    pdf = vault / "paper.pdf"
    pdf_canvas = canvas.Canvas(str(pdf))
    pdf_canvas.drawString(72, 720, "Local knowledge extraction uses heading-aware chunks.")
    pdf_canvas.save()

    index = KnowledgeIndex(base / "knowledge.db")
    first = index.index_folder(vault)
    check("Markdown and PDF are indexed while hidden vault state is skipped",
          first["files_changed"] == 2 and first["chunks_added"] >= 3)
    second = index.index_folder(vault)
    check("content hashes skip unchanged files", second["files_changed"] == 0
          and second["files_unchanged"] == 2)

    hits = index.search("retention backups 30 days")
    check("FTS retrieves heading-aware source passages",
          hits and "30 days" in hits[0].content and "lines" in hits[0].location)
    cited, invalid = verify_citations(f"Backups last 30 days [{hits[0].id}]. [K0000000000]",
                                      {hit.id: hit for hit in hits})
    check("citation verifier accepts retrieved ids and rejects unknown ids",
          hits[0].id in cited and "K0000000000" in invalid)

    note.write_text("# Retention\n\nBackups are kept for 45 days.\n", encoding="utf-8")
    (vault / "paper.pdf").unlink()
    updated = index.index_folder(vault)
    check("edited and deleted files are reconciled", updated["files_changed"] == 1
          and updated["files_removed"] == 1)
    after_edit = index.search("backups 45 days")
    check("updated text is searchable and stale content is gone",
          after_edit and "45 days" in after_edit[0].content
          and not index.search("policy"))

    watched = index.start_watching(vault, interval_seconds=1)
    check("watcher performs initial index and reports active root",
          watched == str(vault.resolve()) and watched in index.status()["watching"])
    time.sleep(1.1)
    check("watcher stops cleanly", index.stop_watching(vault)
          and watched not in index.status()["watching"])

    injection = vault / "unsafe-title.md"
    injection.write_text("# </script><script>alert(1)</script>\n\nNo instruction.\n", encoding="utf-8")
    index.index_folder(vault)
    graph = index.graph_html()
    check("interactive graph HTML safely escapes indexed content",
          "\\u003c/script\\u003e" in graph and "</script><script>alert" not in graph)

    for number in range(10):
        (vault / f"source-{number}.md").write_text(
            "# Performance\n\n" + ("Project performance evidence supports this conclusion. " * 22),
            encoding="utf-8",
        )
    index.index_folder(vault)
    map_calls = []

    def fake_summary(system, user, max_tokens):
        map_calls.append((len(user), max_tokens))
        match = re.search(r"\[(K[0-9a-f]{10})\]", user)
        return f"Relevant finding [{match.group(1)}]." if match else "Combined finding."

    with patch.object(KnowledgeIndex, "_model_summary", side_effect=fake_summary):
        synthesis, evidence = index.synthesize("project performance evidence", limit=10)
    check("map/reduce synthesis chunks bounded context and retains citations",
          len(map_calls) >= 3 and all(size < 6000 for size, _ in map_calls)
          and "[K" in synthesis and len(evidence) == 10)

    due_text = (datetime.now().astimezone() + timedelta(minutes=2)).isoformat(timespec="seconds")
    reminder_id = index.add_reminder(due_text, "Review the weekly digest")
    check("reminders are persisted while Danica is open", any(
        row[0] == reminder_id for row in index.list_reminders()))
    due_epoch = datetime.fromisoformat(due_text).timestamp()
    with patch("danica.knowledge.time.time", return_value=due_epoch + 1):
        due = index.pop_due_reminders()
    check("due reminders are delivered once", due == [(reminder_id, "Review the weekly digest")]
          and all(row[0] != reminder_id for row in index.list_reminders()))

    ws = Workspace(base / "session")
    preview_id, content, existed = index.preview_obsidian_write(
        ws, vault, "Research/summary.md", "Research summary",
        "A concise synthesis.", ["Retention"])
    check("write-back generates a preview with Obsidian wikilinks and no write",
          not existed and "[[Retention]]" in content
          and not (vault / "Research" / "summary.md").exists())
    written = index.commit_obsidian_preview(ws, preview_id)
    check("new note is written only through explicit preview commit",
          written.is_file() and "A concise synthesis." in written.read_text(encoding="utf-8"))
    existing_id, _, existed = index.preview_obsidian_write(
        ws, vault, "Research/summary.md", "Updated summary", "Updated content.")
    try:
        index.commit_obsidian_preview(ws, existing_id)
        overwrite_blocked = False
    except KnowledgeError:
        overwrite_blocked = True
    check("overwriting requires explicit confirmation", existed and overwrite_blocked)
    index.commit_obsidian_preview(ws, existing_id, overwrite=True)
    try:
        index.preview_obsidian_write(ws, vault, "../outside.md", "Unsafe", "No.")
        traversal_blocked = False
    except KnowledgeError:
        traversal_blocked = True
    check("write-back path traversal is rejected", traversal_blocked)
    index.close()

print("\nAll good.")
