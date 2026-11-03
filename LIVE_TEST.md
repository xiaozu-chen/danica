# Live testing guide

Run live/manual acceptance checks only after the remaining harness work is
complete. Until then, continue using offline exit tests; these steps are a
deferred checklist, not a request to start a model server now.

## Before starting

1. Finish the planned harness phases and their offline tests.
2. Use a disposable project/workspace and back up any data you care about.
3. Install Danica's declared dependencies in its virtual environment.
4. Start the chat llama-server on the configured local endpoint (default
   `http://127.0.0.1:8080`) with the chosen Qwen chat model and enough context
   for the test. Do not expose the server on a public interface.
5. For Phase 6 semantic-memory checks, separately start the configured
   embedding llama-server (default `http://127.0.0.1:8081`) with the expected
   embedding model, embedding mode, and dimensions. Confirm both endpoints
   work locally before testing.
6. Use a clean test profile or disposable `%APPDATA%/Danica` data directory so
   memory, sessions, trust records, and audit entries from the test do not
   affect real use.
7. Record the model name/quantization, server arguments, machine, config,
   start/end time, and any failures. Never include actual secrets in test
   prompts, test files, or captured logs.

Do not treat one successful model response as proof of reliability. Repeat
non-deterministic checks and inspect the created files, audit events, and
actual tool results.

## Phase 6: persistent memory

See [PHASE6.md](docs/PHASE6.md) for the storage layout and embedding configuration.

**Offline check before a later live pass**

```powershell
.\.venv\Scripts\python.exe -m scripts.phase6_exit
```

The offline exit test verifies the storage, ranking, consent, and deletion
paths. It does not need a model server.

**Deferred live pass**

1. Configure the local embedding endpoint, model name, dimensions, and recall
   limit in `%APPDATA%/Danica/config.toml`. Keep model and dimensions stable
   after creating the test database.
2. Start Danica with both local servers running. In chat, state a durable,
   non-sensitive preference, such as a preferred response style. Verify Danica
   proposes a scoped memory and does not save it until you accept.
3. Exit and start a different chat session. Ask a task that depends on the
   preference without repeating it. Confirm the response reflects the
   preference and `/memory` shows the fact at its intended scope.
4. Repeat with project-scoped and chat-scoped facts. Verify `/memory` shows
   them and that a narrower-scope fact takes precedence over a global one.
5. Stop the embedding server and repeat a recall. Confirm Danica reports the
   semantic-search fallback and continues with keyword search rather than
   failing the task. Restart the embedding server afterward.
6. Run `/memory review`; inspect likely duplicates. Merge only test facts, and
   verify the surviving page and index contain both pieces of information.
7. Use `/forget <id>` on a disposable fact and confirm the prompt. Verify the
   page and search result are gone. Try the same operation and answer `n`;
   verify nothing is removed.
8. Inspect the audit log and test database for unintended data. Keep prompts
   free of secrets; do not dump credentials or database contents into reports.

**Pass:** a preference is approved, survives a new session, is scoped as
expected, and can be forgotten. Declining a proposal or deletion must leave the
stored state unchanged.

## Phase 7: generated outputs

See [PHASE7.md](docs/PHASE7.md) for input formats, renderer limits, and output
behavior.

**Offline check before a later live pass**

```powershell
.\.venv\Scripts\python.exe -m scripts.phase7_exit
```

**Deferred live pass**

1. Ask Danica to create a chart from a small workspace dataset, then verify the
   JSON source and HTML output. Confirm the final response's present tag is
   removed from visible text and the clickable local link opens the chart.
2. Ask for a dashboard with a table and two chart types. Disconnect the
   machine from the network and confirm the HTML still renders without
   external assets.
3. Generate Markdown, DOCX, and PDF versions of a short report containing
   verified web citations. Open each output and compare the content and
   citations to the source Markdown.
4. Generate a Mermaid flowchart and a Graphviz/DOT diagram; inspect their SVGs
   in a local browser or image viewer. Include a deliberately unsupported
   syntax case and confirm the tool reports its limitation instead of
   claiming to render it.
5. Generate both a PPTX deck and its HTML slideshow. Open the PPTX in a slide
   application; test previous/next buttons and arrow-key navigation in the
   slideshow.
6. Confirm `/artifacts` lists non-HTML outputs, omits HTML, displays paths that
   can be copied, and provides working local links. Confirm an arbitrary
   workspace HTML file is not accepted by the presentation tag.
7. Compare generated artifacts against the source files and inspect the audit
   log. Delete only disposable test outputs after recording results.

**Pass:** one task can produce a cited report as DOCX plus a chart, and the
chart opens from the CLI without external network access.

## Earlier phases: rerun regression checks

These are offline scripts already present in the repository. Run them before
the live acceptance pass to distinguish harness failures from model behavior:

```powershell
.\.venv\Scripts\python.exe -m scripts.phase5_exit
.\.venv\Scripts\python.exe -m scripts.phase6_exit
.\.venv\Scripts\python.exe -m scripts.phase7_exit
.\.venv\Scripts\python.exe -m scripts.phase8_exit
```

The real-model Phase 3 and Phase 4 scripts are intentionally deferred until
the live test window. They require a running local chat model:

```powershell
.\.venv\Scripts\python.exe -m scripts.phase3_exit
.\.venv\Scripts\python.exe -m scripts.phase4_exit
```

Phase 4 may start a local hostile test web server as part of its test. Keep it
on localhost and inspect its output and test results. Phase 5 requires Git.

## Phase 8: plans, skills, and final verification

See [PHASE8.md](docs/PHASE8.md) for the skill format and limits.

**Offline check before a later live pass**

```powershell
.\.venv\Scripts\python.exe -m scripts.phase8_exit
```

**Deferred live pass**

1. Make a disposable project and ask for a clearly multi-step change. Confirm
   Danica writes and displays `PLAN.md` before running task tools.
2. Approve one plan and edit another. Confirm the accepted checklist appears in
   the model context; cancel a third and verify no task tools run.
3. Resume a deliberately interrupted planned session and confirm its plan is
   restored. Finish a planned task and confirm `PLAN.md` is removed afterward.
4. Add a test skill under `%APPDATA%/Danica/skills/<name>/SKILL.md`. Confirm
   `/skills` and the model context show only its name and description until the
   model requests `load_skill`.
5. Ask a task that matches the skill and confirm the body is loaded. Try an
   oversized, malformed, traversal, and secret-containing skill; each must be
   rejected without loading arbitrary files.
6. Seed a verifiable omission in a disposable task. Confirm the final review
   reports it, tries no more than one focused repair, and reports unresolved
   issues if the repair does not fix it.
7. Inspect the audit log and transcript. Confirm plan approvals, skill reads,
   tool permissions, and any repair attempt are represented without logging
   secret values.

**Pass:** a multi-step task cannot start before an approved plan, a skill body
is loaded only on demand, and the review detects a seeded omission while
limiting automatic repair to one attempt.

## Later phases

The later phases do not yet have finished implementations or live acceptance
procedures. Add their detailed checks to this document when the corresponding
harness phases are implemented; do not mark a future phase as passing based on
these placeholders.

- **Phase 9:** process/IPC lifecycle, subagent limits, inherited permissions,
  failure recovery, and parent-context size.
- **Phase 10:** incremental folder indexing, citation accuracy, Obsidian
  write-back preview, digests, graph output, and scheduler behavior.
- **Phase 11:** compare classifier and rule-router decisions on a fixed
  dataset; measure latency and task success before enabling a model change.
- **Phase 12:** MCP/plugin permissions and audit coverage, hooks, headless
  operation, and export bundles.
- **Phase 13:** red-team prompt suite, secret-leak checks, crash recovery,
  Windows paths/encodings, and clean-machine installation.

## Final report

For each completed live check, record the test name, exact command or
interaction, model/server configuration (no secrets), expected result,
observed result, pass/fail, and relevant artifact/log location. File an issue
for every failure and rerun the failing test after its fix. The live acceptance
pass is complete only when each implemented phase's exit criteria have
evidence; future phases remain explicitly not tested until implemented.
