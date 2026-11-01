"""
prune_test.py - Checks the pruner on a synthetic conversation.
Run from the project root:  python -m scripts.prune_test
"""

import json
import tempfile
from pathlib import Path

from danica.context import ContextTracker, estimate_tokens
from danica.pruning import prune_messages
from danica.tools import run_tool
from danica.workspace import Workspace


def check(label: str, condition: bool) -> None:
    print(("PASS  " if condition else "FAIL  ") + label)
    assert condition, label


def pair(i: int, name: str, args: dict, result: str) -> list[dict]:
    """One assistant tool call plus its tool result, like the agent loop builds."""
    cid = f"c{i}"
    return [
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": cid, "type": "function",
                         "function": {"name": name, "arguments": json.dumps(args)}}]},
        {"role": "tool", "tool_call_id": cid, "content": result},
    ]


BIG_READ = ("[src/app.py: lines 1-200 of 400] (more available: call again with offset=201)\n"
            + "\n".join(f"{i}\tsome line of code {'x' * 40}" for i in range(200)))
FAILED_SHELL = ("[exit code 1]\n" + "noise line\n" * 100
                + "Traceback (most recent call last):\nZeroDivisionError: division by zero")
BIG_ARGS = {"path": "big.py", "content": "print('hello')\n" * 400}


def build(recent_pairs: int) -> list[dict]:
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}]
    msgs += pair(0, "read_file", {"path": "src/app.py"}, BIG_READ)             # old, big
    msgs += pair(1, "run_shell", {"command": "python app.py"}, FAILED_SHELL)   # old, failed
    msgs += pair(2, "write_file", BIG_ARGS, "OK: created big.py (400 lines)")  # old, short result
    for k in range(3, 3 + recent_pairs):                                       # recent, big
        msgs += pair(k, "read_file", {"path": "src/app.py"}, BIG_READ)
    return msgs


# --- nothing to prune when everything is recent ---
few = build(recent_pairs=2)   # 5 tool results total, keep 6
check("fewer results than keep_recent: no change", prune_messages(few, 6, 400) == (0, 0))

# --- the main case: 3 old pairs + 6 recent ones ---
msgs = build(recent_pairs=6)
count_before = len(msgs)
tokens_before = estimate_tokens(msgs)
changed, saved = prune_messages(msgs, keep_recent=6, min_chars=400)

# indexes: system 0, user 1, pair k -> assistant at 2+2k, tool at 3+2k
check("old big read_file became a stub", msgs[3]["content"].startswith("[pruned]"))
check("read stub names the file and range", "src/app.py" in msgs[3]["content"]
      and "lines 1-200 of 400" in msgs[3]["content"])
check("failed shell stub keeps the error tail", "ZeroDivisionError" in msgs[5]["content"]
      and "exit code 1" in msgs[5]["content"])
check("short results are left alone", msgs[7]["content"].startswith("OK: created"))

old_args = json.loads(msgs[6]["tool_calls"][0]["function"]["arguments"])
check("old write_file arguments are still valid JSON with the path", old_args["path"] == "big.py")
check("old write_file content was shrunk", old_args["content"].startswith("[pruned") and len(old_args["content"]) < 100)

check("recent results are untouched", all(msgs[3 + 2 * k]["content"] == BIG_READ for k in range(3, 9)))
check("recent call arguments are untouched", json.loads(
    msgs[2 + 2 * 3]["tool_calls"][0]["function"]["arguments"]) == {"path": "src/app.py"})
check("message count unchanged", len(msgs) == count_before)
check("every tool result still has its call id",
      all(m["tool_call_id"] == f"c{(i - 3) // 2}" for i, m in enumerate(msgs) if m["role"] == "tool"))
check("saved tokens is positive and real", saved > 0 and estimate_tokens(msgs) < tokens_before)
check("running again changes nothing (idempotent)", prune_messages(msgs, 6, 400) == (0, 0))

# --- tracker adjusts its exact count ---
t = ContextTracker(window=10000)
t.record(5000, 10)
t.adjust(1200)
check("tracker subtracts freed tokens", t.known_tokens == 3800)

# --- the file tools refuse to write a pruning stub ---
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
    ws = Workspace(Path(tmp) / "ws")
    out = run_tool("write_file", {"path": "x.py", "content": "[pruned: 500 chars, already applied to the file]"}, ws)
    check("write_file refuses a [pruned] placeholder", "refused" in out and not (ws.root / "x.py").exists())

print("\nAll good.")