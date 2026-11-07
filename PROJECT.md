# Danica: Enhanced Design and Build Sequence

*A local-first, CLI-only agentic assistant for coding, research, presentations, notes and productivity. Runs on Qwen3.5 via llama-server with a 65,536-token context per agent.*

## 1. Design principles

Your feature list is large, so these principles decide what gets built first and how.

1. **Small-model-friendly.** A local Qwen3.5 is far less forgiving than a frontier model. Prefer constrained outputs (grammar or JSON-schema decoding), few tools with clear names, fill-in templates instead of free-form generation, and short prompts.
2. **Context is the scarcest resource.** Everything that enters the 65k window must earn its place: prune, stub, summarize, and load on demand (skills, memory, notes).
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

**Suggested stack.** Python 3.11 with `prompt_toolkit` and `rich` (or Textual for the subagent dashboard), `httpx` for the llama-server API, SQLite with FTS5 and `sqlite-vec`, packaged as a single Windows executable with PyInstaller or Nuitka. Go or Rust are good alternatives if you want a lean binary, but Python gets you to a working agent fastest, and the component boundaries above keep a later rewrite possible.

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

## 3. Enhancements and corrections to the original ideas

**Tool calling and decoding**
- Use llama-server's grammar or JSON-schema constraints for tool calls so malformed calls are rare. Add a repair-and-retry step (max two attempts) before surfacing an error to the model.
- Keep the tool set small at first (about 10) and group related actions behind one tool with a mode argument.

**Context budget (65,536 tokens)**
- Reserve a fixed share: system prompt and tool schemas about 4k, memory and skills about 4k, output headroom about 6k. That leaves roughly 50k for the working conversation.
- Use **tiered** pressure handling rather than a single 90% trigger: prune old tool outputs at about 60%, compact at about 85%, and hard-stop at 90%. Pruning usually delays compaction substantially.
- Compaction must preserve: the user's goal, the plan, TODO.md contents, files touched, decisions made, and open questions. Re-inject TODO.md after every compaction.
- Because each agent has an isolated 65k window, the llama-server `-c` value must cover the largest single agent. Subagents run one at a time, so a single slot is enough, but switching agents invalidates the KV cache. Use `--slot-save-path` with slot save and restore, or accept a re-prefill on each switch.

**Thinking and non-thinking routing**
- Qwen3.5 exposes a thinking toggle through chat-template arguments, so the router only needs to produce a boolean. Start with simple rules (code, math, planning and multi-step requests think; greetings and lookups do not) and let the user override with `/mode`.
- A fine-tuned SmolLM2-135M classifier is feasible but needs a few thousand labeled examples. Log router decisions from day one so you collect that dataset naturally, then train the classifier late. Keep the rule-based router as a fallback.

**Web research and injection defense**
- Keep retrieval unrestricted, but restrict *downloads*: fetch only text-like content types (HTML, plain text, JSON, PDF converted to text), cap response size, and never write binaries to disk.
- Wrap all fetched text in clearly delimited, labeled blocks and instruct the model that content inside is untrusted data.
- **Taint tracking:** once untrusted content has entered the context, any later high-risk tool call (shell, file delete, writes outside the workspace, network writes, memory writes) requires explicit user confirmation, and the prompt shows which source tainted the context.
- Keep the canary token, add a URL allowlist for outbound requests that contain data from the context, and strip invisible Unicode and hidden HTML text from fetched pages before they reach the model.
- Search provider options for a local tool: a self-hosted SearxNG instance, or a search API with a key stored in the secret vault.

**Presentation of HTML charts**
- Define the tag precisely, for example `<present file="charts/sales.html"/>`. The CLI intercepts it, validates that the path is inside the workspace, strips it from the displayed text and renders an open button.
- The model should never write chart HTML. It writes a **data JSON file** and the tool fills a bundled template (pie, bar, line, scatter, area, table, multi-chart dashboard). Bundle the chart library locally so files work offline.
- Open files via `file://` or a tiny local HTTP server bound to localhost, whichever your terminal supports for clickable links.

**Memory**
- Embedding model note: nomic-embed-text-v1.5 expects task prefixes (`search_document:` and `search_query:`). Forgetting these degrades retrieval quality noticeably.
- Retrieval should be **hybrid**: SQLite FTS5 (BM25) plus vector similarity, merged with reciprocal rank fusion. Pure semantic search misses exact names and identifiers.
- Every fact should carry: scope, source, created and updated times, confidence, and a pointer to the Markdown page it came from. That makes forget, dedup and review reliable.
- Precedence for scopes: per-chat overrides per-project, which overrides global.

**Subagents**
- A subagent is a separate process with its own context, started in a new terminal window and connected to the parent over a local socket or named pipe using JSON lines. It receives a task brief and returns a **structured summary**, never its full transcript, which is what actually saves the parent's context.
- Enforce the limit of 5, depth of 1 (subagents cannot spawn subagents), a per-subagent turn and time budget, and inherit the parent's permission policy as a ceiling.

**Safety and undo**
- Checkpoints in project mode should use a **shadow git repository** (separate git dir, user's directory as work tree) so Danica never pollutes the user's own repo.
- Windows has no lightweight container, so define sandbox levels: (1) path-jailed tools with a restricted environment, (2) Job Object limits on spawned processes, (3) optional Docker or WSL execution, (4) full access toggle. Be honest in the UI about what each level does and does not stop.
- Make the audit log tamper-evident by chaining a hash of the previous entry into each new one.
- The secret scanner should work in *both* directions: block reads of `.env`, key files and credential stores into context, and scan model output before it is written to artifacts.

## 4. Build sequence

Each phase has a goal, the work, and an exit test. Phases are ordered by dependency: nothing is built before the thing it relies on.

### Phase 0: Foundations (a few days)
- Decide stack, repo layout, config format and logging.
- Run llama-server with Qwen3.5 and measure real speed, context behavior and tool-calling quality. Pick quantization and `-c` accordingly.
- Build a small **eval harness**: 20 to 30 scripted tasks (edit a file, answer from a file, simple web lookup) that you rerun after each phase to catch regressions.
- **Exit:** you can call the model from code with streaming, thinking on and off.

### Phase 1: Core agent loop (MVP)
- CLI shell with streaming output, input history, Ctrl+C handling.
- Agent loop: prompt assembly, constrained tool calls, execution, result feedback, stop conditions and a max-step guard.
- Tools: `read_file`, `write_file`, `edit_file` (exact-string replace), `list_dir`, `grep`/`glob`, `run_shell`.
- Per-chat isolated `workspace/` with a path jail.
- Token counter and a basic context bar.
- JSONL transcript saved per session.
- **Exit:** Danica can write and fix a small program inside a workspace, unattended.

### Phase 2: Safety baseline
- Permission tiers: read (free), write (free inside workspace), execute, destructive and outside-path (ask). Remember choices per session.
- Secret scanner (both directions), append-only hash-chained audit log.
- Sandbox level 1 and 2, with the full-access toggle.
- Checkpoints and `/undo`, `/rewind` (shadow git for workspaces).
- **Exit:** a deliberately destructive prompt is stopped or confirmed, and `/undo` restores state.

### Phase 3: Context management
- Tool-result pruning with informative stubs (for example, `read file X, 400 lines`).
- `TODO.md` scratchpad, re-injected after compaction.
- Compaction with the Context Summarizer role, tiered triggers, and a post-compaction sanity check.
- `/context` token breakdown, `/compact`, session list and `/resume`.
- **Exit:** a long multi-file task runs past three compactions without losing its goal.

### Phase 4: Web research
- Search and fetch tools with HTML-to-text extraction, size and content-type limits, and caching.
- Injection defenses: untrusted-content wrapping, sanitization, taint tracking, canary.
- Basic citation support: every retrieved passage keeps its URL and a snippet ID that the model can reference.
- **Exit:** a research question returns an answer with sources, and a test page containing injected instructions fails to hijack the agent.

### Phase 5: Modes and project registration
- Normal chat as the default; `Project mode` button or command with the trust-directory confirmation.
- Use the launch directory, register it, create its memory folder and shadow git.
- Relax tool limits inside the project, keeping confirmation only for critical actions.
- Slash commands: `/mode`, `/export` and the rest of the core set.
- **Exit:** launching in a code folder, trusting it, and editing real files works with undo.

### Phase 6: Memory system
- Markdown pages with YAML frontmatter (`title, created, updated, uuid, summary, scope`).
- SQLite facts table, FTS5 and sqlite-vec, second llama-server for embeddings.
- Retrieval at the start of each task and on demand through a `recall` tool; automatic fact capture proposals that the user can accept.
- `/memory` list and review, scopes with precedence, `forget` that deletes across Markdown, SQLite rows and vectors and verifies the result, dedup and merge pass.
- **Exit:** a preference stated in one session is correctly used in a later one, and `forget` leaves no trace.

### Phase 7: Output systems
- **Present tag** handling and the chart and table template library, plus the dashboard builder.
- **Artifact system:** after a task, list files written (excluding HTML) with open or copy actions.
- **Document generation:** docx, pdf and markdown through template-based generators; xlsx if wanted.
- **Diagrams:** Mermaid and Graphviz rendered to PNG or SVG.
- **Slide decks:** pptx theme library and an HTML slideshow.
- **Exit:** from one prompt, Danica produces a cited report as docx plus a chart you can open from the CLI.

### Phase 8: Planning, skills, verification
- **Plan mode:** plan file written and shown before large tasks, approve or edit, then execute against the plan.
- **Skills:** user-managed `SKILL.md` folders only under `%APPDATA%\Danica\skills`; each folder has a `SKILL.md` with `name` and `description` frontmatter. Only that metadata is loaded into context; the body loads when the model requests it. Never load skills from a cloned project.
- **Self-reflection:** a cheap final pass that checks tool-based results against the original request and approved plan, with at most one automatic repair attempt.
- **Exit:** a multi-step project completes with a plan, and the verification pass catches a seeded mistake.

### Phase 9: Subagents
- Process spawn into new terminal windows, local IPC, brief-in and summary-out protocol.
- Limits: 5 total, one at a time, depth 1, budgets, inherited permissions.
- Subagent dashboard with `run`, `peek`, `wait`, and `kill`; model delegation remains summary-only.
- **Exit:** Danica delegates a research subtask, and the parent context grows by only the summary.

### Phase 10: Local knowledge base and productivity
- Folder indexer for Markdown, Obsidian vaults, PDFs and papers: file watcher, content hashing for incremental updates, heading-aware chunking, hybrid search.
- Synthesis queries with citation mode (claim linked to file and passage); multi-step map-reduce summarization that respects the context limit.
- Obsidian write-back with preview and wikilink creation; daily and weekly digests; knowledge graph exported as interactive HTML.
- Reminders and scheduler (local cron-like tasks, run when Danica is open or through a small background service).
- Deep research reports with structured, cited output.
- **Exit:** ask for the three main bottlenecks from last month's notes and get a cited answer.

### Phase 11: Thinking mode and performance
- Use the fine-tuned SmolLM2-135M classifier only to choose the binary thinking mode (`true`/`false`); retain a deterministic thinking fallback.
- Tool selection stays with the main chat model's function-calling loop. Do not add a separate general-intent classifier or route-label dataset.
- Prompt caching, KV slot save and restore, parallel tool execution where safe, startup time.
- **Exit:** measured improvement on the eval harness in latency without a drop in task success.

### Phase 12: Extensibility
- MCP client, plugin folder (Python or JS tools), hooks before and after tool calls, headless mode (`Danica -p`), export bundle with README.
- All extensions pass through the same permission and audit layer.
- **Exit:** an MCP tool and a custom plugin tool are usable and audited.

### Phase 13: Hardening and release
- Red-team injection suite, secret-leak tests, crash recovery, Windows path and encoding edge cases.
- Installer or single executable, docs, first-run setup that downloads or locates models.
- **Exit:** a clean machine can install and run Danica end to end.

## 5. Suggested v1 cut line

Ship after **Phase 8**, plus the web-research defenses from Phase 4. That gives you an agent that codes, researches, presents and remembers, with safe permissions and context handling. Subagents, the notes knowledge base, extensibility and the trained classifier are strong v1.5 and v2 features, and each builds cleanly on the earlier phases.

## 6. Main risks and mitigations

| Risk | Mitigation |
|---|---|
| Local model makes unreliable tool calls | Constrained decoding, small tool set, retries, eval harness every phase |
| 65k fills fast on coding tasks | Pruning first, tiered compaction, line-range reads, subagent summaries |
| Prompt injection via unrestricted retrieval | Taint tracking, confirmation on risky tools after untrusted input, canary, sanitization |
| Windows sandboxing is weak | Layered levels, honest labeling, optional Docker or WSL |
| Memory grows noisy | Source and confidence on every fact, dedup passes, `/memory` review |
| Subagent windows are fragile | Strict IPC protocol, heartbeat, kill switch, fall back to in-process execution |
| Scope creep | Phase exit tests, v1 cut line, eval-driven priorities |

## 7. Where each original idea lands

| Original idea | Phase |
|---|---|
| CLI only, autonomous coding | 1 |
| Sandboxed execution, secret scanner, audit log | 2 |
| Checkpoints and undo | 2 |
| Live context bar, `/context`, tool-result pruning, TODO.md, auto compaction, session resume | 3 |
| Unrestricted search and fetch, download restriction, injection canary, citations | 4 |
| Normal chat vs project mode, trust dialog, project registration | 5 |
| Semantic memory, scopes, curation, forget | 6 |
| HTML charts and present tag, artifact system, documents, diagrams, slides, dashboards | 7 |
| Plan mode, skills, self-reflection | 8 |
| Subagents and dashboard | 9 |
| Notes and Obsidian agent, digests, knowledge graph, scheduler, research reports | 10 |
| Thinking classifier | 11 (rules earlier) |
| MCP, plugins, hooks, headless, export bundle | 12 |