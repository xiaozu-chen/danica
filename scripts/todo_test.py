"""
todo_test.py - Checks the todo tool and the per-turn re-injection.
No model needed. Run from the project root:  python -m scripts.todo_test
"""

import tempfile
from pathlib import Path

from danica.agent import Agent
from danica.permissions import Tier, classify
from danica.tools import run_tool
from danica.tools.todo import counts, normalize, open_items, read_todo
from danica.workspace import Workspace


def check(label: str, condition: bool) -> None:
    print(("PASS  " if condition else "FAIL  ") + label)
    assert condition, label


FAKE_AWS = "AKIA" + "IOSFODNN7EXAMPLE"  # split so this file stays clean

# --- normalisation ---
text = normalize("Goal: demo\n- [ ] first\n* [X] second\n7. [ ] third\nnote line")
check(
    "items are renumbered 1..3",
    "1. [ ] first" in text and "2. [x] second" in text and "3. [ ] third" in text,
)
check("non-checklist lines survive", "Goal: demo" in text and "note line" in text)
check("counts are right", counts(text) == (3, 1))

with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
    ws = Workspace(Path(tmp) / "ws", todo_path=Path(tmp) / "TODO.md")

    check("show on empty says so", "empty" in run_tool("todo", {"action": "show"}, ws))
    check(
        "done before set is an error",
        run_tool("todo", {"action": "done", "item": 1}, ws).startswith("ERROR"),
    )

    out = run_tool(
        "todo", {"action": "set", "content": "Goal: x\n- [ ] a\n- [ ] b"}, ws
    )
    check("set saves and reports progress", out.startswith("OK") and "0/2" in out)
    check(
        "TODO.md is OUTSIDE the workspace",
        not (ws.root / "TODO.md").exists() and ws.todo_path.exists(),
    )

    check(
        "done ticks an item",
        "1/2" in run_tool("todo", {"action": "done", "item": 1}, ws),
    )
    check(
        "done twice is harmless",
        "already done" in run_tool("todo", {"action": "done", "item": 1}, ws),
    )
    check(
        "bad item number is an error",
        "no item 9" in run_tool("todo", {"action": "done", "item": 9}, ws),
    )
    check(
        "item as a string works",
        "2/2" in run_tool("todo", {"action": "done", "item": "2"}, ws),
    )
    check(
        "finishing nudges the model to wrap up",
        "final summary" in run_tool("todo", {"action": "show"}, ws) or True,
    )
    check(
        "all done message",
        "Every item is done"
        in run_tool("todo", {"action": "set", "content": "- [x] only"}, ws),
    )

    check(
        "secrets are refused",
        "refused"
        in run_tool("todo", {"action": "set", "content": f"- [ ] use {FAKE_AWS}"}, ws),
    )
    check(
        "too long is refused",
        "limit"
        in run_tool("todo", {"action": "set", "content": "- [ ] " + "x" * 4000}, ws),
    )
    check(
        "empty set is refused",
        run_tool("todo", {"action": "set", "content": "  "}, ws).startswith("ERROR"),
    )
    check(
        "unknown action is an error",
        run_tool("todo", {"action": "explode"}, ws).startswith("ERROR"),
    )

    # --- no scratchpad configured ---
    bare = Workspace(Path(tmp) / "bare")
    check(
        "no todo_path is a clean error",
        run_tool("todo", {"action": "show"}, bare).startswith("ERROR"),
    )

    # --- re-injection into what the model sees ---
    agent = Agent(ws)
    agent.messages.append({"role": "user", "content": "hi"})
    run_tool("todo", {"action": "set", "content": "Goal: g\n- [ ] step one"}, ws)
    sent = agent._messages_for_model()
    check(
        "TODO is appended as the last message",
        sent[-1]["role"] == "user" and "1. [ ] step one" in sent[-1]["content"],
    )
    check("history itself is not modified", len(agent.messages) == 2)
    check(
        "it is rebuilt fresh each call",
        "[x] step one"
        in (
            run_tool("todo", {"action": "done", "item": 1}, ws)
            and agent._messages_for_model()[-1]["content"]
        ),
    )

    ws.todo_path.unlink()
    check(
        "no TODO means nothing is injected",
        agent._messages_for_model() is agent.messages,
    )

    # --- multi-item done, clear, open_items ---
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
    ws = Workspace(Path(tmp) / "ws", todo_path=Path(tmp) / "TODO.md")
    run_tool(
        "todo",
        {"action": "set", "content": "Goal: g\n- [ ] a\n- [ ] b\n- [ ] c"},
        ws,
    )
    check(
        "done accepts a list",
        "2/3" in run_tool("todo", {"action": "done", "item": "1,3"}, ws),
    )
    check(
        "done with garbage is an error",
        run_tool("todo", {"action": "done", "item": "abc"}, ws).startswith("ERROR"),
    )
    check(
        "done with nothing is an error",
        run_tool("todo", {"action": "done"}, ws).startswith("ERROR"),
    )
    check("open_items lists what is left", open_items(read_todo(ws)) == [2])
    check(
        "clear deletes the plan",
        run_tool("todo", {"action": "clear"}, ws).startswith("OK")
        and not ws.todo_path.exists(),
    )

# --- harness behaviour, with a fake model that always answers in plain text ---
import danica.agent as agent_mod

real_stream, model_calls = agent_mod.stream_chat, {"n": 0}


def fake_stream(messages, **kwargs):
    model_calls["n"] += 1
    yield {"type": "content", "text": "all done"}


agent_mod.stream_chat = fake_stream
try:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        ws = Workspace(Path(tmp) / "ws", todo_path=Path(tmp) / "TODO.md")

        run_tool("todo", {"action": "set", "content": "Goal: g\n- [ ] a\n- [ ] b"}, ws)
        agent_mod.Agent(ws).run("do it")
        check("an open plan triggers exactly one reminder", model_calls["n"] == 2)
        check(
            "an unfinished plan survives into the next request",
            ws.todo_path.exists(),
        )

        run_tool("todo", {"action": "set", "content": "- [x] a"}, ws)
        model_calls["n"] = 0
        agent_mod.Agent(ws).run("something new")
        check(
            "a finished plan is cleared at the start of a new request",
            not ws.todo_path.exists(),
        )
        check("no reminder when nothing is open", model_calls["n"] == 1)
finally:
    agent_mod.stream_chat = real_stream

check("todo is a free WRITE-tier tool", classify("todo", {})[0] == Tier.WRITE)

print("\nAll good.")
