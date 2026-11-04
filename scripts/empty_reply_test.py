"""
empty_reply_test.py - An empty model reply must be retried once, then reported.
No model needed. Run from the project root:  python -m scripts.empty_reply_test
"""

import tempfile
from pathlib import Path

import danica.agent as agent_mod
from danica.agent import Agent
from danica.workspace import Workspace


def check(label: str, condition: bool) -> None:
    print(("PASS  " if condition else "FAIL  ") + label)
    assert condition, label


replies: list[str] = []      # what the fake model says next, one entry per model call
model_calls = {"n": 0}


def fake_stream(messages, **kwargs):
    model_calls["n"] += 1
    yield {"type": "content", "text": replies.pop(0)}


def run(*scripted: str) -> tuple[str, Agent]:
    replies[:] = list(scripted)
    model_calls["n"] = 0
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        agent = Agent(Workspace(Path(tmp) / "ws"))
        return agent.run("hello"), agent


real_stream = agent_mod.stream_chat
agent_mod.stream_chat = fake_stream
try:
    out, agent = run("", "here you go")
    check("an empty reply is retried once and the retry is used", out == "here you go" and model_calls["n"] == 2)
    check("the retry note is marked as not coming from the user",
          any("Your last reply was empty" in str(m.get("content")) and "not from the user" in str(m.get("content"))
              for m in agent.messages))

    out, _ = run("  \n ", "second try")
    check("a whitespace-only reply counts as empty", out == "second try" and model_calls["n"] == 2)

    out, _ = run("", "")
    check("two empty replies in a row are reported, not swallowed",
          out.startswith("[stopped") and "empty reply" in out and model_calls["n"] == 2)

    out, _ = run("a normal answer")
    check("a normal reply is untouched and costs one call", out == "a normal answer" and model_calls["n"] == 1)
finally:
    agent_mod.stream_chat = real_stream

print("\nAll good.")