# Live Test Guide: Phases 9-12

Use this guide to validate Danica interactively against a running local model.
For each feature, send the listed prompt, run the command test, and compare the
observed behavior with the expectations. Replace example paths with paths on
your machine.

## Before testing

1. Start the configured llama-server and confirm ordinary chat works.
2. From the repository root, start Danica with a disposable workspace:

   ```powershell
   .\.venv\Scripts\python.exe -m danica.cli --workspace "$env:TEMP\danica-live-test"
   ```

3. Keep `/audit` available for checking extension and action records. Do not
   enable full access just to make a test pass.
4. For knowledge tests, create a test vault outside the repository with at
   least two Markdown notes and one PDF containing known facts. Example note:

   ```markdown
   # Project bottlenecks

   The three bottlenecks are slow imports, flaky integration tests, and manual
   release notes. The team recorded these findings in October 2026.
   ```

   Add a second note with a related topic so graph links and search have
   something to display.

Tests that ask the model to use tools depend on the model honoring the tool
request. If it answers without calling a tool, retry with a direct instruction
to use the named capability; do not treat a fabricated answer as a pass.

## Phase 9: Subagents

### Delegate a research task and inspect its summary

- **Prompt/request:** `Delegate a research subtask: inspect the workspace for the test runner and report the relevant command and evidence. Return only the concise result.`
- **Command testing:** After the response, run `/agents`, then `/agents peek <id>` using the displayed child ID. For an independently controllable child, start `/agents run Inspect the workspace and report the test runner command.`; use `/agents peek <id>`, `/agents wait <id>`, or `/agents kill <id>`.
- **Expected output:** The delegated response contains a concise summary and changed-file metadata, not the child's transcript. The dashboard shows a child status and elapsed time. `wait` eventually shows completion; `kill` changes a running child to `killed`. The parent remains responsive for dashboard commands while a child started with `/agents run` is active.

### Check delegation limits and permissions

- **Prompt/request:** `Try delegating an empty task and then ask a delegated agent to create another subagent.`
- **Command testing:** Check `/agents` after each attempt. If the task would perform an outside-workspace action, observe the normal permission prompt. Optionally interrupt a long-running delegated request with Ctrl+C.
- **Expected output:** Empty and nested delegation are refused, only one child runs at a time, and the per-session maximum is five children. The child does not gain permissions beyond the parent. Failures are reported rather than returned as successful summaries.

## Phase 10: Local knowledge and productivity

### Index and search a test vault

- **Prompt/request:** `Search my indexed notes for the three October bottlenecks. Quote the relevant passage and identify its source.`
- **Command testing:**

  ```text
  /knowledge index "C:\path\to\danica-test-vault"
  /knowledge status
  /knowledge search October bottlenecks
  ```

- **Expected output:** Indexing reports changed files and passages. Repeating the index without edits reports unchanged files. Search results show stable `[K...]` passage IDs, source path, heading, and line/page location. The answer's citations refer to IDs actually returned by the search.

### Check updates and watching

- **Prompt/request:** `After I change the test note, find the updated value in my notes.`
- **Command testing:** Run `/knowledge watch "C:\path\to\danica-test-vault"`, edit and save a note, then run `/knowledge search` with the new phrase and `/knowledge status`. Finish with `/knowledge stop "C:\path\to\danica-test-vault"`.
- **Expected output:** The changed text becomes searchable while watching; status lists the active root. Stop reports the watcher was stopped and status no longer lists it.

### Test cited synthesis

- **Prompt/request:** `What are the three main bottlenecks recorded in October? Give a concise answer and cite each claim to the local notes.`
- **Command testing:** Ask Danica to use its local knowledge synthesis capability, or run `/knowledge search October bottlenecks` first for a baseline.
- **Expected output:** When synthesis is used, the response identifies the three seeded bottlenecks and places `[K...]` citations next to claims. Each ID must be among passages retrieved for this request. If the local model server is unavailable, the synthesis reports an explicit error; it must not invent a successful cited result.

### Test Obsidian write-back safeguards

- **Prompt/request:** `Create a short Obsidian note summarizing the test-vault findings, linking to the Project bottlenecks note. Do not write it yet; show me the preview.`
- **Command testing:** Review the full preview and note its ID. Confirm that no file exists yet. Then run `/knowledge commit <preview-id>` and explicitly answer `y` to the write confirmation.
- **Expected output:** The preview contains Markdown and the requested `[[Project bottlenecks]]` wikilink; it makes no change before commit. Commit asks before writing. A path outside the vault is rejected, and an existing note is not overwritten without confirmation.

### Test graph, digests, and reminders

- **Prompt/request:** `Show me the changes to indexed notes this week and remind me in two minutes to review the test vault.`
- **Command testing:**

  ```text
  /knowledge graph knowledge-test-graph.html
  /knowledge digest weekly
  /reminder list
  ```

  In PowerShell, generate a fresh due time and paste the printed reminder
  command into Danica:

  ```powershell
  $due = (Get-Date).AddMinutes(2).ToString("yyyy-MM-ddTHH:mm:sszzz")
  "/reminder add $due Review the test vault"
  ```

  Leave the interactive prompt open until the reminder is due.
- **Expected output:** Graph export reports a workspace-relative HTML file that can be opened in a browser. The digest lists recently changed indexed files and short excerpts (it is a change list, not a model-written summary). The reminder appears in the pending list and is announced while Danica is still waiting at the prompt; it is delivered only once.

## Phase 11: Thinking mode and performance

### Check the binary thinking classifier and fallback

- **Prompt/request:** With routing enabled, send one straightforward lookup such as `What is the capital of Japan?`, then a multi-step request such as `Compare these two approaches and explain the trade-offs.`
- **Command testing:** If a compatible local classifier endpoint is running, configure `[thinking]` in `%APPDATA%\Danica\config.toml` as described in [thinking-classifier.md](./docs/thinking-classifier.md), restart Danica, and run `/think auto` before each request. To check fallback, temporarily configure an unavailable local classifier URL, restart, and repeat. Restore the valid URL afterward.
- **Expected output:** `/think auto` removes the manual override. A valid endpoint returns only the boolean thinking decision; endpoint failure produces an explicit thinking notice and falls back to the heuristic. The two requests may select different thinking modes, but a classifier outage must not prevent answering. Tool choices continue to be made by the main chat model. Do not enable prompt logging for this test unless you explicitly accept storing prompt text locally.

### Check opt-in routing logs

- **Prompt/request:** With decision logging enabled and prompt logging disabled, send a harmless request containing a unique marker such as `thinking-check-2026-11-07`.
- **Command testing:** Inspect `%APPDATA%\Danica\logs\thinking.jsonl` after the request. Repeat with `log_decisions = false` to verify logging is opt-in.
- **Expected output:** When enabled, a record includes a timestamp, prompt hash, binary thinking decision, and decision source, but not the prompt text or an intent/route label. When disabled, no new record is added.

### Run the eval harness

- **Prompt/request:** No chat prompt; run the same eval task set under the baseline and candidate server/configuration.
- **Command testing:**

  ```powershell
  .\.venv\Scripts\python.exe -m evals.run
  ```

  Keep the model, server, machine load, and eval set comparable between runs. Preserve each generated JSON report in `evals\results\`.
- **Expected output:** The harness prints pass counts, median and p95 latency, and whether a previous run exists as a baseline. A candidate is not eligible for promotion unless latency improves by the configured threshold without a task-success regression. One run only establishes a baseline; it does not demonstrate improvement.

### Measure thinking-classifier accuracy (30 prompts)

- **Prompt/request:** No interactive prompt is needed; the evaluator sends 30 labeled user messages to the configured binary classifier.
- **Command testing:** Enable `[thinking].classifier_enabled` and point `classifier_url` at the running classifier endpoint, then run:

  ```powershell
  .\.venv\Scripts\python.exe -m evals.thinking_accuracy
  ```

  To keep the detailed per-item outputs:

  ```powershell
  .\.venv\Scripts\python.exe -m evals.thinking_accuracy --output evals\results\thinking-accuracy.json
  ```

- **Expected output:** Thirty rows show expected versus predicted `thinking_mode`, followed by accuracy, thinking precision/recall, and false-positive/false-negative counts. The set is balanced (15 true, 15 false). The runner refuses to report accuracy if the classifier is disabled or falls back to rules. Treat this as a smoke evaluation; inspect mismatches and use a larger held-out set for model-quality claims.

### Verify tool selection remains with the chat model

- **Prompt/request:** Try `Search my indexed notes for the cancellation policy` and `Use the sample plugin echo tool with value hello`. Approve any expected permissions.
- **Command testing:** Check the model's visible tool-use behavior and the resulting answer; run `/audit` if inspecting approved actions. Review the system design in [thinking-classifier.md](./docs/thinking-classifier.md).

- **Expected output:** The main chat model selects an available tool via function calling or responds directly. The thinking classifier emits no tool name, route label, or general-intent class. There is no separate general-routing classifier or classifier dataset.

## Phase 12: Extensibility

### Test a Python plugin

- **Prompt/request:** After installing a simple trusted test plugin, ask: `Use the sample plugin echo tool with the value hello-live-test and tell me its result.`
- **Command testing:** Create a plugin under `%APPDATA%\Danica\plugins\sample\` with a `plugin.toml` and Python module following [extensions.md](./docs/extensions.md). Restart Danica so it loads the plugin, then approve the `plugin_sample_echo` action when asked. Run `/audit` afterward.
- **Expected output:** The namespaced plugin tool runs in a separate worker process and returns `hello-live-test`. The action requires outside-workspace approval and is recorded in the audit log. Process separation is not an OS sandbox; use only code you trust.

### Test an MCP tool

- **Prompt/request:** With a trusted local stdio MCP server configured, ask: `Use the test MCP echo tool to return hello-mcp-live-test.`
- **Command testing:** Add the server under `[mcp.servers.<name>]` in `%APPDATA%\Danica\config.toml` using the command and arguments described in [extensions.md](./docs/extensions.md). Restart Danica, approve the namespaced `mcp_<server>_<tool>` call, then run `/audit`.
- **Expected output:** Startup discovers the server's tools using MCP JSON-RPC. The selected tool returns the expected value, requires outside-workspace approval, and leaves an audit record. Invalid or unavailable servers should produce an explicit error.

### Test before/after hooks

- **Prompt/request:** `Read a harmless file in the workspace.`
- **Command testing:** Configure a trusted `hooks.before` command that returns `{"allow": false}`. Restart Danica and request a read. Then configure an allow hook plus an `hooks.after` recorder, restart, and repeat. See [extensions.md](./docs/extensions.md) for hook input and configuration behavior.
- **Expected output:** The denying before-hook blocks the tool call and records the denial. With the allow hook, the read completes and the after-hook receives redacted arguments/result. Hook errors are surfaced; an after-hook failure warns that the tool already completed. Hooks do not run in headless mode.

### Test headless mode and export

- **Prompt/request:** Headless test request: `Run a shell command that prints DANICA_HEADLESS_TEST.`
- **Command testing:**

  ```powershell
  .\.venv\Scripts\python.exe -m danica.cli -p "Run a shell command that prints DANICA_HEADLESS_TEST." --workspace "$env:TEMP\danica-live-test"
  .\.venv\Scripts\python.exe -m danica.cli --export-bundle live-test.zip --workspace "$env:TEMP\danica-live-test"
  ```

  Inspect `live-test.zip` and its `README.md`. For the exclusion check, place a harmless file containing a known test secret pattern and a binary file in the disposable workspace before exporting.
- **Expected output:** Headless mode makes one request without an interactive approval callback; approval-gated shell/delegation/extension actions fail closed rather than silently running. Bundle export reports included/skipped counts and creates a ZIP with a README and eligible UTF-8 text files. Sensitive paths, detected secrets, binaries, oversized files, and the output archive itself are excluded.

## Record results

For each feature, record **Pass**, **Fail**, or **Blocked**, the exact command/prompt used, relevant output or audit entry, and any environment dependency (model, classifier endpoint, embedding server, MCP server, or plugin). Do not mark the Phase 11 performance exit as passed without comparable eval reports showing both improved latency and no task-success regression.
