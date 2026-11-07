# Local knowledge and productivity

Danica keeps a local SQLite index of folders you explicitly add. The original
Markdown and PDF files remain the source of truth.

## Index, search, and watch

Use the CLI commands:

```text
/knowledge index "C:\Users\you\Documents\Obsidian"
/knowledge search subscription cancellation
/knowledge watch "C:\Users\you\Documents\Obsidian"
/knowledge stop "C:\Users\you\Documents\Obsidian"
/knowledge status
```

Indexing supports UTF-8 Markdown, plain text, and PDFs up to 20 MiB each.
Hidden folders (including Obsidian's `.obsidian` settings) are skipped.
Content hashes avoid re-indexing unchanged files; removed and edited sources
are reconciled on the next scan. Watch mode polls while Danica is open.
Search combines SQLite FTS5 with embeddings when the configured embedding
server is available. If embeddings are unavailable, the CLI reports the
keyword-only fallback.

The `knowledge_search` tool returns passage IDs such as `[K0123456789]` with
file, heading, and line/page provenance. `knowledge_synthesize` uses bounded
map/reduce calls and returns cited evidence. Danica validates citations against
passages retrieved during the current request; this validates provenance, not
whether a passage actually supports a claim.

## Obsidian write-back and graph

`obsidian_write_preview` only prepares a preview in the current session. Review
the complete text in the CLI and run `/knowledge commit <preview-id>` to write
it. Existing notes require an explicit overwrite confirmation. The target must
be a Markdown file inside a vault indexed by the user; secrets and path escapes
are rejected.

`/knowledge graph graph.html` writes an interactive, self-contained graph into
the current workspace. `/knowledge digest daily` and `/knowledge digest weekly`
list recently changed indexed files. Reminders use local ISO timestamps:

```text
/reminder add 2026-11-07T18:00:00+08:00 Review the weekly digest
/reminder list
```

Due reminders are displayed while the interactive prompt is open. There is no
background service; reminders are stored locally and checked by the prompt's
async event loop while Danica is running.
