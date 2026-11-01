"""
agent.py - The agent loop.

One "turn" of the loop:
    1. send the conversation + tool schemas to the model
    2. if the model answers in plain text -> we're done
    3. if it calls tools -> run them, append the results, go to step 1

Guards that keep a small local model from going off the rails:
    - max_steps: hard cap on model calls per user request
    - repeat detector: the same call over and over gets a nudge, then a stop
    - bad tool arguments come back as an ERROR string so the model can retry
"""

import json
from pathlib import Path
from typing import Callable

from danica.llm import stream_chat
from danica.tools import run_tool, tool_schemas
from danica.workspace import Workspace
from danica.config import CONFIG
from danica.compaction import (build_message, fallback_narrative, find_cut, harvest,
                               is_summary, render, summarize)
from danica.context import CHARS_PER_TOKEN, ContextTracker, estimate_tokens
from danica.permissions import PermissionPolicy, classify
from danica.audit import AuditLog
from danica.secret_scan import redact
from danica.checkpoints import Checkpoints
from danica.pruning import prune_messages
from danica.tools.todo import read_todo
from danica.tools.todo import counts, open_items, read_todo

SYSTEM_PROMPT = """You are Danica, a careful coding assistant running locally in a terminal on Windows.
You work inside a workspace folder. All file paths are relative to it.

Rules:
- Look before you change things: use list_dir, glob, grep and read_file.
- Never invent file contents. Read a file before you edit it.
- Use edit_file for small changes and write_file for new files.
- run_shell uses Windows PowerShell. Use it to run code and tests (python is available).
- After writing code, run it to check that it works, and fix any errors you see.
- If a tool returns ERROR, read the message and correct your call.
- Keep going until the task is done, then reply with a short summary.
- The shell has a deliberately minimal environment: variables like APPDATA or API keys are NOT available. Empty output for an environment variable is expected, so do not try to work around it.
- Old tool results and file contents in earlier messages may be replaced by short [pruned] notes to save space. Never copy a [pruned] note into a file or a command. If you need the content again, read the file or re-run the tool.
- For any task with three or more steps, first call the todo tool with action "set": a goal line plus a checklist of "- [ ]" steps. Tick each step off with action "done" (item "2", or "1,2,3") right after you finish it, not at the end. Your TODO is shown to you again every turn; trust it over your memory. Skip it for simple one-step requests. If the TODO belongs to an old, unrelated task, call action "clear".
"""

# Callback signature: on_event(kind, data). Kinds used below:
#   "thinking" / "content"  -> data is a streamed text fragment
#   "tool_call"             -> data is (name, args_dict_or_raw_string)
#   "tool_result"           -> data is the result string
EventCallback = Callable[[str, object], None]


def _safe_args(args: dict) -> dict:
    """Tool arguments made safe for the audit log: secrets redacted, long strings cut.
    File contents never go into the log, only their length."""
    out = {}
    for key, value in args.items():
        if isinstance(value, str):
            original_len = len(value)
            value, _ = redact(value)
            if len(value) > 200:
                value = value[:200] + f"...({original_len} chars total)"
        out[key] = value
    return out


class Agent:
    def __init__(
        self,
        ws: Workspace,
        transcript_path: Path | None = None,
        thinking: bool = False,
        max_steps: int = 25,
        on_event: EventCallback | None = None,
        policy: PermissionPolicy | None = None,
        audit: AuditLog | None = None,
        checkpoints: Checkpoints | None = None,
        
    ):
        self.ws = ws
        self.thinking = thinking
        self.max_steps = max_steps
        self.on_event = on_event or (lambda kind, data: None)
        self.transcript_path = transcript_path

        # The system prompt lives at index 0 of the message list but is NOT
        # written to the transcript (it is rebuilt every session).
        self.messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]

        # Tracks how full the context window is (exactly numbers from the server)
        self.tracker = ContextTracker(CONFIG["model"]["context"])

        # Compaction state: the model-written narrative, plus facts the harness
        # collects itself. They roll forward across every compaction.
        self.summary = ""
        self.user_requests: list[str] = []
        self.files_changed: list[str] = []
        self.files_read: list[str] = []
        self.compactions = 0

        # Decides whether each tool call may run (asks the user when needed)
        self.policy = policy or PermissionPolicy()
        # Consecutive permissions denials. Two "no"s in a row means the user is
        # telling us to stop, so we hand control back instead of probing more.
        self.denied_in_row = 0
        self.audit = audit # None = no logging (tests)
        self.checkpoints = checkpoints # None = no /undo support (tests)

    # ------------------------------------------------------------------ helpers

    def _write_transcript(self, record: dict) -> None:
        """Append one JSON line to the transcript (messages, and event records)."""
        if self.transcript_path:
            with open(self.transcript_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _add(self, message: dict) -> None:
        """Append a message to the conversation AND to the JSONL transcript."""
        self.messages.append(message)
        self._write_transcript(message)

    def _messages_for_model(self) -> list[dict]:
        """The history PLUS the current TODO.md as a final message.
        Built fresh on every call and never stored: the scratchpad is always
        the latest version, never duplicated, and never pruned or compacted.
        Role is "user" because Qwen's chat template only allows a system
        message at the very start."""
        text = read_todo(self.ws)
        if not text:
            return self.messages
        note = {
            "role": "user",
            "content": ("[TODO.md: your current plan, shown again every turn. This is a reminder "
                        "from the harness, not a new request from the user. Update it with the "
                        "todo tool.]\n" + text),
        }
        return self.messages + [note]   # new list: self.messages itself is not modified

    def _model_turn(self) -> tuple[str, list[dict]]:
        """Call the model once; stream text to the UI; return (content, tool_calls)."""
        content_parts: list[str] = []
        calls: list[dict] = []
        # How many messages the server is about to see (for the token tracker)
        msg_count = len(self.messages)

        for piece in stream_chat(
            self._messages_for_model(),
            thinking=self.thinking,
            temperature=0.3,       # low: tool calls should be steady, not creative
            max_tokens=4096,
            tools=tool_schemas(),
            include_usage=True,
        ):
            if piece["type"] == "usage":
                # Exact prompt size: replace all earlier estimates
                self.tracker.record(piece["prompt_tokens"], msg_count)
                continue
            if piece["type"] == "tool_calls":
                calls = piece["calls"]
                continue
            if piece["type"] == "content":
                content_parts.append(piece["text"])
            # Thinking text is shown but never stored in the history (saves context)
            self.on_event(piece["type"], piece["text"])

        return "".join(content_parts), calls

    def _log(self, event: str, **data) -> None:
        "Write an audit entry if auditing is on."
        if self.audit:
            self.audit.append(event, **data)

    def prune(self, keep_recent: int | None = None) -> int:
        """Replace old tool output with stubs. Returns how many items changed."""
        cfg = CONFIG["pruning"]
        keep = cfg["keep_recent"] if keep_recent is None else keep_recent
        changed, saved = prune_messages(self.messages, keep, cfg["min_chars"])
        if changed:
            self.tracker.adjust(saved)   # keep the exact-count bookkeeping honest
            self._log("prune", changed=changed, saved_tokens=saved)
            self.on_event("notice", f"[context] pruned {changed} old item(s), freed ~{saved:,} tokens")
        return changed

    def compact(self) -> bool:
        """Replace the old part of the conversation with a summary.
        Returns False if there was nothing worth summarizing."""
        window = self.tracker.window
        cut = find_cut(self.messages, int(window * CONFIG["compaction"]["tail_fraction"]))
        if cut is None:
            return False

        self.on_event("notice", "[context] compacting: summarizing earlier messages (takes a moment)...")
        before = self.tracker.used(self.messages)
        old = self.messages[1:cut]                        # index 0 is the system prompt
        tail = self.messages[cut:]
        body = [m for m in old if not is_summary(m)]      # the old summary is rolled in via self.summary

        # Facts collected by CODE: the model can't forget or invent these
        requests, changed, read = harvest(body)
        self.user_requests = list(dict.fromkeys(self.user_requests + requests))
        self.files_changed = list(dict.fromkeys(self.files_changed + changed))
        self.files_read = list(dict.fromkeys(self.files_read + read))

        # The summarizer prompt may use at most 40% of the window
        max_chars = int(window * 0.4 * CHARS_PER_TOKEN)
        narrative, problem = summarize(self.summary, render(body, max_chars))
        if narrative is None:
            narrative = fallback_narrative(self.summary, body)
            self.on_event("notice", f"[context] summarizer failed ({problem}); used a basic summary instead")
        self.summary = narrative

        msg = build_message(self.summary, self.user_requests, self.files_changed, self.files_read)
        removed = estimate_tokens(old)
        self.messages = [self.messages[0], msg] + tail
        self.compactions += 1

        # The exact size is unknown until the server answers again, so estimate it
        # from the old exact number. The next model call replaces this guess.
        after = max(before - removed + estimate_tokens([msg]), 0)
        self.tracker.record(after, len(self.messages))

        # The transcript keeps the full history; this marker lets /resume rebuild
        # the same state later (replace everything but the last tail_len messages).
        self._write_transcript({"event": "compaction", "tail_len": len(tail), "summary": msg["content"]})
        if self.transcript_path:
            folder = self.transcript_path.parent / "summaries"
            folder.mkdir(exist_ok=True)
            (folder / f"{self.compactions:03d}.md").write_text(msg["content"], encoding="utf-8")
        self._log("compact", number=self.compactions, tokens_before=before,
                  tokens_after=after, summarizer_ok=problem == "" or narrative is not None)
        self.on_event("notice", f"[context] compacted: {before:,} -> about {after:,} tokens")
        return True

    def _execute(self, call: dict) -> str:
        """Parse one tool call, check permission, run it, scrub the output. Returns a string."""
        raw = call["arguments"] or "{}"
        try:
            args = json.loads(raw)
            if not isinstance(args, dict):
                raise ValueError("arguments must be a JSON object")
        except (json.JSONDecodeError, ValueError) as e:
            self.on_event("tool_call", (call["name"], raw))
            self._log("bad_arguments", tool=call["name"])
            return f"ERROR: could not parse tool arguments as a JSON object ({e}). Try again."

        # Show the call BEFORE asking, so the user sees what is being approved
        self.on_event("tool_call", (call["name"], args))

        tier = classify(call["name"], args)[0]
        allowed, denial = self.policy.check(call["name"], args)
        self._log("tool_call", tool=call["name"], args=_safe_args(args),
                  tier=tier.value, allowed=allowed, how=self.policy.last_how)

        if not allowed:
            self.denied_in_row += 1
            return denial  # the model reads this and adapts

        self.denied_in_row = 0  # any approved action breaks the streak
        result = run_tool(call["name"], args, self.ws)

        # Output scan: secrets never reach the model's context
        cleaned, kinds = redact(result)
        found = sorted(set(kinds))
        if found:
            cleaned += (f"\nNOTE: {len(kinds)} secret-like value(s) ({', '.join(found)}) "
                        f"were redacted from this output.")
        self._log("tool_result", tool=call["name"], chars=len(result),
                  redacted=found, error=result.startswith("ERROR"))
        return cleaned

    # ---------------------------------------------------------------- main loop

    def run(self, user_text: str) -> str:
        """Handle one user request. Returns the model's final text answer."""
        # Snapshot the workspace BEFORE anything changes, so /undo can return here
        if self.checkpoints and self.checkpoints.available:
            try:
                snap = self.checkpoints.begin_request(user_text)
                self._log("checkpoint", id=snap)
            except Exception as e: # a failed snapshot must never block the user's work
                self.on_event("notice", f"WARNING: checkpoint failed ({e}). /undo will not cover this")
                # A finished (or empty) plan from the previous request must not leak into
        # this one. An UNFINISHED plan stays, so "continue" still works.
        if self.ws.todo_path and self.ws.todo_path.exists():
            total, done = counts(read_todo(self.ws))
            if total == 0 or done == total:
                self.ws.todo_path.unlink()
        self._add({"role": "user", "content": user_text})

        self.denied_in_row = 0

        # Log only the size of the request, not its text (the transcript has the text)
        self._log("user_request", chars=len(user_text))

        last_signature = None   # what the previous step called
        repeats = 0             # how many steps in a row had the identical calls
        nudged = False          # the open-TODO reminder is sent at most once per request

        for _ in range(self.max_steps):
            # Tiered pressure: prune first, compact only if pruning was not enough
            if self.tracker.level(self.messages) != "ok":
                self.prune()
                if self.tracker.level(self.messages) in ("compact", "stop"):
                    self.prune(keep_recent=2)     # more aggressive second pass
                if self.tracker.level(self.messages) in ("compact", "stop"):
                    self.compact()

            # Hard stop: a call at 90%+ could overflow the window
            if self.tracker.level(self.messages) == "stop":
                return ("[stopped: context is still 90% full even after pruning and compaction. "
                        "Use /undo or start a new session.]")
            content, calls = self._model_turn()

            # Build the assistant message exactly as the API expects it
            message: dict = {"role": "assistant", "content": content}
            if calls:
                message["tool_calls"] = [
                    {
                        "id": c["id"],
                        "type": "function",
                        "function": {"name": c["name"], "arguments": c["arguments"] or "{}"},
                    }
                    for c in calls
                ]
            self._add(message)

            # No tool calls = the model considers the task finished
            if not calls:
                # ...but if the plan still has open items, ask ONCE whether that is
                # really true. Small models forget to tick things off.
                left = open_items(read_todo(self.ws))
                if left and not nudged:
                    nudged = True
                    self._add({"role": "user", "content": (
                        "[harness reminder, not from the user] Your TODO.md still has open "
                        f"items: {', '.join(map(str, left))}. If they are finished, tick them "
                        "with the todo tool (action 'done', item like '1,2,3'). If not, keep "
                        "working. Then give your final summary.")})
                    continue
                return content

            # Repeat detector: same tool + same arguments, step after step
            signature = tuple((c["name"], c["arguments"]) for c in calls)
            repeats = repeats + 1 if signature == last_signature else 0
            last_signature = signature
            if repeats >= 4:
                return "[stopped: Danica kept repeating the same tool call]"

            for i, call in enumerate(calls):
                try:
                    result = self._execute(call)
                except KeyboardInterrupt:
                    # Every tool_call needs a matching result, otherwise the
                    # history is invalid next turn. Fill the gaps, then re-raise.
                    for rest in calls[i:]:
                        self._add({"role": "tool", "tool_call_id": rest["id"],
                                   "content": "ERROR: interrupted by the user"})
                    raise

                if repeats >= 2:
                    result += "\nNOTE: you have repeated this exact call. Try a different approach."

                self.on_event("tool_result", result)
                self._add({"role": "tool", "tool_call_id": call["id"], "content": result})

            # Circuit breaker: the user said no twice, so stop and ask them
            if self.denied_in_row >= 2:
                return "[stopped: you denied two actions in a row. Tell Danica what to do instead.]"
        return f"[stopped: reached the limit of {self.max_steps} steps]"
    