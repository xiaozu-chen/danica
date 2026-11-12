"""
pruning.py - Replaces OLD tool output with short informative stubs.

Works in place on the in-memory message list. The JSONL transcript on disk
is never changed, so the full history stays available for /resume.

What gets pruned (only in the "old" region, before the last N tool results):
  1. tool results longer than min_chars  -> a stub saying what the call was
  2. big text arguments in old assistant tool calls (file contents of
     write_file, old_string/new_string of edit_file) -> a length marker
Running it twice changes nothing the second time (stubs are recognised).
"""

import json

from danica.context import estimate_tokens

STUB_PREFIX = "[pruned]"
ARG_KEYS = ("content", "old_string", "new_string")   # arguments that carry file text
ARG_MIN_CHARS = 200                                  # shorter than this: leave alone
TAIL_LINES = 4                                       # lines kept from a failed shell command


def _index_calls(messages: list[dict]) -> dict[str, tuple[str, dict]]:
    """Map tool_call_id -> (tool name, parsed arguments), so a tool result can
    be matched with the call that produced it."""
    calls = {}
    for m in messages:
        for c in m.get("tool_calls") or []:
            fn = c.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            calls[c["id"]] = (fn.get("name", "?"), args if isinstance(args, dict) else {})
    return calls


def make_stub(name: str, args: dict, result: str) -> str:
    """Build the replacement text for one old tool result."""
    lines = result.splitlines()
    n = len(lines)

    if name == "read_file" and lines and lines[0].startswith("["):
        # First line is already "[path: lines a-b of N]": reuse it
        what = f"read_file {lines[0][:150]}"
    elif name == "grep":
        hits = sum(1 for ln in lines if not ln.startswith("..."))
        what = f"grep {args.get('pattern', '?')!r} in {args.get('path', '.')}: {hits} result lines"
    elif name == "glob":
        what = f"glob {args.get('pattern', '?')}: {n} lines of results"
    elif name == "list_dir":
        what = f"list_dir {args.get('path', '.')}: {max(n - 1, 0)} entries"
    elif name == "run_shell":
        status = lines[0] if lines else ""        # "[exit code N]" or "[TIMED OUT ...]"
        what = f"run_shell `{str(args.get('command', ''))[:100]}` -> {status}, {max(n - 1, 0)} output lines"
        if status != "[exit code 0]":
            # Failures: keep the end, that's where the error message lives
            tail = " | ".join(ln.strip() for ln in lines[-TAIL_LINES:])
            what += f". Last lines: {tail[:300]}"
    elif name == "web_fetch":
        what = (f"web_fetch {str(args.get('url', '?'))[:150]}: {n} lines of page text "
                f"(cached: fetching it again gives the same [S#.N] ids)")
    elif name == "web_search":
        what = (f"web_search {str(args.get('query', '?'))[:100]!r}: {n} lines of results "
                f"(search again if you need them)")
    elif name == "web_cache_search":
        what = (f"web_cache_search {str(args.get('query', '?'))[:100]!r}: {n} lines of results "
                f"(free to search again)")
    elif name == "recall":
        what = f"recall {str(args.get('query', '?'))[:100]!r}: {n} lines of notes (search again if you need them)"
    else:
        what = f"{name} result ({len(result)} chars)"

    return f"{STUB_PREFIX} {what} - output removed to save context; call the tool again if you need it."


def _shrink_arguments(call: dict) -> bool:
    """Shrink big text values inside one tool call's JSON arguments. True if changed."""
    fn = call.get("function", {})
    try:
        args = json.loads(fn.get("arguments") or "{}")
    except json.JSONDecodeError:
        return False
    if not isinstance(args, dict):
        return False

    changed = False
    for key in ARG_KEYS:
        value = args.get(key)
        if isinstance(value, str) and len(value) > ARG_MIN_CHARS and not value.startswith("[pruned"):
            args[key] = f"[pruned: {len(value)} chars, already applied to the file]"
            changed = True
    if changed:
        fn["arguments"] = json.dumps(args, ensure_ascii=False)  # still valid JSON
    return changed


def prune_messages(messages: list[dict], keep_recent: int, min_chars: int) -> tuple[int, int]:
    """Prune in place. Returns (items_changed, estimated_tokens_saved)."""
    tool_idx = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    if len(tool_idx) <= keep_recent:
        return 0, 0   # everything is "recent": nothing to do

    # Everything before `cutoff` is the old region.
    cutoff = tool_idx[-keep_recent] if keep_recent > 0 else len(messages)
    # Don't split a kept result from the assistant message that made the call
    if cutoff > 0 and messages[cutoff - 1].get("role") == "assistant":
        cutoff -= 1

    calls = _index_calls(messages)
    before = estimate_tokens(messages[:cutoff])
    changed = 0

    for m in messages[:cutoff]:
        if m.get("role") == "tool":
            content = m.get("content") or ""
            if content.startswith(STUB_PREFIX) or len(content) < min_chars:
                continue
            name, args = calls.get(m.get("tool_call_id"), ("unknown", {}))
            m["content"] = make_stub(name, args, content)
            changed += 1
        elif m.get("role") == "assistant":
            for c in m.get("tool_calls") or []:
                if _shrink_arguments(c):
                    changed += 1

    saved = max(before - estimate_tokens(messages[:cutoff]), 0)
    return changed, saved