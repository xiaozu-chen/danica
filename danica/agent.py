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
from danica.context import ContextTracker

SYSTEM_PROMPT = """You are Danica, a careful coding assistant running locally in a terminal on Windows.
You work inside a workspace folder. All file paths are relative to it.

Rules:
- Look before you change things: use list_dir, glob, grep and read_file.
- Never invent file contents. Read a file before you edit it.
- Use edit_file for small changes and write_file for new files.
- run_shell uses Windows PowerShell. Use it to run code and tests (python is available).
- After writing code, run it to check that it works, and fix any errors you see.
- If a tool returns ERROR, read the message and correct your call.
- Keep going until the task is done, then reply with a short summary."""

# Callback signature: on_event(kind, data). Kinds used below:
#   "thinking" / "content"  -> data is a streamed text fragment
#   "tool_call"             -> data is (name, args_dict_or_raw_string)
#   "tool_result"           -> data is the result string
EventCallback = Callable[[str, object], None]


class Agent:
    def __init__(
        self,
        ws: Workspace,
        transcript_path: Path | None = None,
        thinking: bool = False,
        max_steps: int = 25,
        on_event: EventCallback | None = None,
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

    # ------------------------------------------------------------------ helpers

    def _add(self, message: dict) -> None:
        """Append a message to the conversation AND to the JSONL transcript."""
        self.messages.append(message)
        if self.transcript_path:
            with open(self.transcript_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(message, ensure_ascii=False) + "\n")

    def _model_turn(self) -> tuple[str, list[dict]]:
        """Call the model once; stream text to the UI; return (content, tool_calls)."""
        content_parts: list[str] = []
        calls: list[dict] = []
        # How many messages the server is about to see (for the token tracker)
        msg_count = len(self.messages)

        for piece in stream_chat(
            self.messages,
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

    def _execute(self, call: dict) -> str:
        """Parse the arguments of one tool call and run it. Always returns a string."""
        raw = call["arguments"] or "{}"
        try:
            args = json.loads(raw)
            if not isinstance(args, dict):
                raise ValueError("arguments must be a JSON object")
        except (json.JSONDecodeError, ValueError) as e:
            self.on_event("tool_call", (call["name"], raw))
            return f"ERROR: could not parse tool arguments as a JSON object ({e}). Try again."

        self.on_event("tool_call", (call["name"], args))
        return run_tool(call["name"], args, self.ws)

    # ---------------------------------------------------------------- main loop

    def run(self, user_text: str) -> str:
        """Handle one user request. Returns the model's final text answer."""
        self._add({"role": "user", "content": user_text})

        last_signature = None   # what the previous step called
        repeats = 0             # how many steps in a row had the identical calls

        for _ in range(self.max_steps):
            # Hard stop: a call at 90&+ could overflow the window and crash the server.
            # (Phase 3 replaces this with pruning and compaction.)
            if self.tracker.level(self.messages) == "stop":
                return ("[stopped: context is 90% full. Compaction arrives in Phase 3; for now start a new session.]")
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

        return f"[stopped: reached the limit of {self.max_steps} steps]"
    