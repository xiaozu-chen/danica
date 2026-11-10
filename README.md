# Danica (Dan)

*A local-first, CLI-only agentic assistant for coding, research, presentations, notes and productivity. Runs on Qwen3.5 via llama-server*

---

Design principles

1. **Small-model-friendly.** A local Qwen3.5 is far less forgiving than a frontier model. Prefer constrained outputs (grammar or JSON-schema decoding), few tools with clear names, fill-in templates instead of free-form generation, and short prompts.
2. **Context is the scarcest resource.** Everything that enters the `n_ctx` window must earn its place: prune, stub, summarize, and load on demand (skills, memory, notes).
3. **Harness over model.** Reliability comes from the surrounding code: permissions, validation, retries, checkpoints and verification passes, not from hoping the model behaves.
4. **Untrusted by default.** Anything fetched from the web, a PDF or a note is data, never instructions.
5. **Files first, database second.** Markdown for human-readable and flexible memory; SQLite only for facts, indices and embeddings.
6. **Build the loop, then the guardrails, then the features.** Each phase ends in something usable and testable.

## 2. Architecture overview

| Component | Responsibility |
|---|---|
| **CLI frontend** | Input, streaming output, context bar, slash commands, permission prompts, clickable open buttons |
| **Agent core** | The loop: prompt assembly, model call, tool-call parsing, tool execution, result handling |
| **llama-server (chat)** | Qwen3.5, OpenAI-compatible API, one slot per active agent |
| **llama-server (embeddings)** | Second instance with nomic-embed-text-v1.5 (`--embedding`), kept small |
| **Tool layer** | File ops, shell, web search and fetch, present, memory, notes, subagent spawn |
| **Policy layer** | Path jail, permission tiers, secret scanner, injection defenses, audit log |
| **Context manager** | Token counting, tool-result pruning, compaction, scratchpad, skill loading |
| **Memory store** | Markdown pages with YAML frontmatter plus SQLite (facts, FTS5, sqlite-vec) |
| **Session store** | Chat and project transcripts, resume list, per-session workspace |
| **Subagent supervisor** | Spawns separate terminal windows, talks to them over local IPC |

**Stack.** Python 3.11+ with Textual for the terminal UI (with `textual-image` for inline Sixel charts in Windows Terminal) and `rich` for rendering. The agent runs synchronously on a worker thread and talks to the UI through Textual messages, so streaming never blocks the screen. It calls llama-server's OpenAI-compatible API over `httpx` and reads the real context size from the server's `/props` endpoint at startup. Settings come from a TOML file in `%APPDATA%\Danica` (parsed with the built-in `tomllib`). Memory is stored in SQLite, with embeddings served by a separate local llama-server instance. Web search goes through a local SearXNG instance, and git provides the `/undo` and `/rewind` checkpoints. Packaging as a single Windows executable (PyInstaller or Nuitka) is still the plan. Go or Rust remain good alternatives if you want a leaner binary, but Python got to a working agent fastest, and the component boundaries keep a later rewrite possible.

**Suggested on-disk layout.**

```
%APPDATA%/Danica/
  config.toml
  memory/            global memory (.md pages)
  facts.db           SQLite: facts, FTS5, vectors, note index
  sessions/<id>/     transcript.jsonl, workspace/, TODO.md, checkpoints/
  projects/<hash>/   registered project: memory/, index, shadow git
  skills/            user-managed global SKILL.md folders (project skills are never loaded)
  plugins/  hooks/  logs/audit.jsonl
```

![alt text](something_something.jpg)

---

## Licenses

[MIT License](LICENSE)

[Third Party Licenses](THIRD_PARTY_LICENSES.md)