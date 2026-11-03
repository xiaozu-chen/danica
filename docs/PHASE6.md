# Phase 6: Memory system

Phase 6 gives Danica durable, searchable facts across sessions. Memory is
opt-in: Danica may propose a useful fact, but it is not saved until the user
accepts the proposal. Existing chat and project behavior does not depend on a
memory server being available.

## What changed

- Memory pages are Markdown with YAML frontmatter (`title`, `created`,
  `updated`, `uuid`, `summary`, `scope`, and an internal `scope_id`).
- SQLite keeps the searchable fact records, an FTS5 index, and a `sqlite-vec`
  vector index. The Markdown page is the human-readable source of truth.
- Hybrid retrieval combines full-text and vector rankings. Scope precedence is
  chat, then project, then global; narrower-scope matches get a small ranking
  preference.
- Relevant memories are retrieved at the start of each task and added as a
  transient context note. The note is not copied into the conversation
  transcript. The `recall` tool can search again on demand.
- The `remember` tool shows the exact fact and scope and requires an explicit
  yes before saving. Secrets and web-content blocks are rejected.
- `/memory` lists stored facts. `/memory review` surfaces likely duplicates;
  `/memory merge <source-id> <target-id>` merges after confirmation. Exact
  duplicates are not re-added.
- `/forget <id>` confirms before deleting. `forget_memory` is available to the
  agent but still requires a permission confirmation. Removal deletes the
  Markdown page and SQLite fact, FTS, and vector rows, then verifies them and
  vacuums the database.

## How it works

The shared SQLite database is `%APPDATA%/Danica/facts.db`. Global pages live in
`%APPDATA%/Danica/memory/global/`. Project pages use the registered project's
existing `memory/` folder. Chat pages live under that session's memory folder.
The store keeps scope identifiers in SQLite so one database can search all
three scopes while applying precedence.

Each task asks the embedding endpoint for a `search_query:` vector. Saved facts
are indexed with `search_document:` vectors. The default configuration expects
an OpenAI-compatible llama-server on port 8081 serving
`nomic-embed-text-v1.5` with 768 dimensions. For example, start a second server
with the embedding model and its embedding mode enabled, listening on
`127.0.0.1:8081`, then set the relevant model and dimensions in
`%APPDATA%/Danica/config.toml` if they differ from the defaults:

```toml
[memory]
embedding_url = "http://127.0.0.1:8081"
embedding_model = "nomic-embed-text-v1.5"
embedding_dimensions = 768
recall_limit = 5
```

Keep the embedding model and dimensions stable after creating the memory
database. If they differ from the values recorded in that index, Danica warns
and temporarily uses FTS5 only; automatic vector reindexing is not implemented.

When that endpoint is unavailable or returns invalid data, Danica reports that
semantic search is unavailable and continues with FTS5 when possible. The
embedding model is separate from the chat model and must be started by the
user; Danica does not download or launch models.

## Verification

Run the offline Phase 6 exit test from the repository root:

```powershell
.\.venv\Scripts\python.exe -m scripts.phase6_exit
```

The test uses deterministic local vectors and checks persistence across
sessions, scope precedence, context injection, proposal consent, duplicate
review and merge, and deletion from every index.
