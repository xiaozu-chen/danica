"""
capture_test.py - Checks the capture gate, the grounding check, what the extractor is
shown, the proposal queue and the agent hook. No model needed.
Run from the project root:  python -m scripts.capture_test
"""

import json
import tempfile
from pathlib import Path

import danica.agent as agent_mod
import danica.capture as capture
from danica.agent import Agent
from danica.capture import MAX_PENDING, ProposalQueue, extract, grounded, worth_checking
from danica.memory import MemoryStore, MemoryStoreError, parse_page
from danica.workspace import Workspace


def check(label: str, condition: bool) -> None:
    print(("PASS  " if condition else "FAIL  ") + label)
    assert condition, label


FAKE_AWS = "AKIA" + "IOSFODNN7EXAMPLE"   # split so this file stays clean

# --- the gate ---
check("a stated preference passes the gate", worth_checking("From now on I prefer tabs for indentation."))
check("a plain task does not", not worth_checking("Fix the bug in app.py please"))
check("tiny and huge messages do not", not worth_checking("always") and not worth_checking("I prefer tabs. " * 200))

# --- grounding ---
check("a note in the user's own words is grounded",
      grounded("I prefer 4-space indentation", "From now on I prefer 4-space indentation and type hints"))
check("an invented note is not", not grounded("The user deploys to AWS Lambda", "I prefer 4-space indentation"))
check("stemming: 'prefers' matches 'prefer'", grounded("User prefers tabs", "I prefer tabs"))

# --- notes with source and confidence ---
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
    tmp = Path(tmp)
    store = MemoryStore(tmp / "g0", None, tmp / "c0")
    page = store.add("global", "I like dark mode", source="captured", confidence="0.8")
    text = page.path.read_text(encoding="utf-8")
    check("a captured note records its source and confidence",
          'source: "captured"' in text and 'confidence: "0.8"' in text)
    back = parse_page(text, "global", page.path)
    check("they survive a read", back.source == "captured" and back.confidence == "0.8")
    try:
        store.add("global", "x", source="robot")
        refused = False
    except MemoryStoreError:
        refused = True
    check("an unknown source is refused", refused)
    old = parse_page('---\ntitle: "x"\nuuid: "aaaaaaaa-0000-4000-8000-000000000001"\n---\nbody', "global", tmp / "x.md")
    check("pages from older chunks default to user / 1.0", old.source == "user" and old.confidence == "1.0")

    # --- the queue ---
    qpath = tmp / "q.json"
    q = ProposalQueue(qpath)
    check("a new queue is empty", q.count() == 0)
    check("a proposal is queued", q.add("global", "I prefer tabs") and q.count() == 1)
    check("a duplicate is not (ignoring case)", not q.add("global", "i PREFER tabs"))
    check("the queue survives a restart", ProposalQueue(qpath).items()[0]["text"] == "I prefer tabs")
    q.remove(q.items()[0]["id"])
    check("removing works", q.count() == 0)
    for i in range(MAX_PENDING):
        q.add("global", f"note {i}")
    check("a full queue refuses more", not q.add("global", "one more"))
    qpath.write_text("{bad", encoding="utf-8")
    check("a corrupt queue file is an empty queue", ProposalQueue(qpath).count() == 0)
    qpath.write_text(json.dumps([{"id": "a1", "scope": "global", "text": "ok"},
                                 {"id": "a2", "scope": "weird", "text": "x"}, {"text": "no id"}, 5]), encoding="utf-8")
    check("malformed entries are ignored", ProposalQueue(qpath).count() == 1)

    # --- extraction, with a fake model ---
    replies: list = []
    calls: list = []

    def fake_chat(messages, **kwargs):
        calls.append(messages)
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return {"thinking": "", "content": reply}

    real_chat, real_stream = capture.chat, agent_mod.stream_chat
    capture.chat = fake_chat
    try:
        store = MemoryStore(tmp / "g", None, tmp / "c")      # no project open
        SRC = "From now on I prefer tabs for indentation and we use unittest here."

        replies[:] = ["global: I prefer tabs for indentation\nproject: We use unittest here"]
        got = extract(SRC, store)
        check("notes come back as (scope, text)", got[0] == ("global", "I prefer tabs for indentation"))
        check("a scope that isn't open falls back to chat", got[1] == ("chat", "We use unittest here"))
        msgs = calls[-1]
        check("the model sees ONLY the system prompt and the user's message",
              [m["role"] for m in msgs] == ["system", "user"] and msgs[1]["content"] == "USER MESSAGE:\n" + SRC)

        replies[:] = ["NONE"]
        check("NONE means no proposals", extract(SRC, store) == [])
        replies[:] = ["Sure! Here is what I found."]
        check("a chatty reply with no note lines gives nothing", extract(SRC, store) == [])
        replies[:] = ["global: The user lives in Paris and likes cheese"]
        check("an invented note is dropped", extract(SRC, store) == [])
        replies[:] = [f"global: my key is {FAKE_AWS}"]
        check("a note with a secret is dropped", extract(f"Remember that my key is {FAKE_AWS}", store) == [])

        SRC3 = "I prefer tabs. I prefer dark mode. I prefer short answers. I prefer vim. I prefer linux."
        replies[:] = ["global: I prefer tabs\nglobal: I prefer dark mode\nglobal: I prefer short answers\n"
                      "global: I prefer vim\nglobal: I prefer linux"]
        check("at most 3 proposals per request", len(extract(SRC3, store)) == 3)
        replies[:] = ["global: I prefer tabs\nglobal: i prefer TABS"]
        check("duplicates inside one reply collapse", len(extract(SRC3, store)) == 1)
        replies[:] = ["- global: I prefer tabs\n1. chat: I prefer dark mode"]
        check("bulleted and numbered lines are parsed", [s for s, _ in extract(SRC3, store)] == ["global", "chat"])
        long_src = "I prefer " + "tabs " * 70
        replies[:] = ["global: " + long_src]
        check("a note over 300 characters is dropped", extract(long_src, store) == [])

        store.add("global", "I prefer tabs")
        replies[:] = ["global: I prefer tabs"]
        check("a note that is already saved is dropped", extract(SRC3, store) == [])

        # --- the agent hook ---
        agent_mod.stream_chat = lambda messages, **kw: iter([{"type": "content", "text": "ok"}])
        events: list = []
        store2 = MemoryStore(tmp / "g2", None, tmp / "c2")
        agent = Agent(Workspace(tmp / "ws"))
        agent.on_event = lambda kind, data: events.append((kind, data))
        agent.memory = store2
        agent.proposals = ProposalQueue(tmp / "q2.json")

        calls.clear()
        replies[:] = ["global: I prefer tabs for indentation"]
        agent.run("From now on I prefer tabs for indentation.")
        check("a stated preference queues a proposal", agent.proposals.count() == 1 and len(calls) == 1)
        check("the user is told how to review it",
              any(k == "notice" and "/memory review" in d for k, d in events))
        check("NOTHING is saved without approval", store2.list_pages() == [])

        calls.clear()
        agent.run("Fix the bug in app.py please")
        check("a plain request makes no extra model call", len(calls) == 0)

        replies[:] = [RuntimeError("model down")]
        check("a dead model server never breaks a finished request",
              agent.run("From now on I always use tabs.") == "ok")

        calls.clear()
        agent2 = Agent(Workspace(tmp / "ws2"))
        agent2.memory = store2
        agent2.run("From now on I prefer tabs for indentation.")
        check("without a queue attached there is no capture", len(calls) == 0)
    finally:
        capture.chat, agent_mod.stream_chat = real_chat, real_stream

print("\nAll good.")