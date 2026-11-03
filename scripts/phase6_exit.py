"""Phase 6 exit test for persistent scoped memory, recall, review, and forget."""

import json
import re
import struct
import tempfile
from pathlib import Path

import danica.agent as agent_module
import httpx
from danica.agent import Agent
from danica.memory import MemoryError, MemoryStore
from danica.permissions import PermissionPolicy
from danica.workspace import Workspace


def check(label: str, condition: bool) -> None:
    print(("PASS  " if condition else "FAIL  ") + label)
    assert condition, label


def fake_embedding(text: str, task: str) -> bytes:
    values = [0.0] * 768
    for word in re.findall(r"[a-z]+", text.casefold()):
        values[sum(word.encode("utf-8")) % len(values)] += 1.0
    return struct.pack("<768f", *values)


def unavailable_embedding(text: str, task: str) -> bytes:
    raise httpx.ConnectError("offline")


with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)
    global_dir = root / "memory" / "global"
    first = MemoryStore(
        root / "facts.db", global_dir, root / "project" / "memory", "project-1",
        root / "chat-1" / "memory", "chat-1",
    )
    first._embed = fake_embedding

    global_fact, warning = first.save("Answer style", "Use concise answers by default.", "global")
    check("a global Markdown page is created", Path(global_fact["markdown_path"]).is_file())
    page = Path(global_fact["markdown_path"]).read_text(encoding="utf-8")
    check("the page has required YAML frontmatter", all(
        field in page for field in ("title:", "created:", "updated:", "uuid:", "summary:", "scope:")
    ))
    check("the configured local embedding path indexes the fact", not warning)

    project_fact, _ = first.save("Answer style", "Use concise answers by default.", "project")
    chat_fact, _ = first.save("Answer style", "Use concise answers by default.", "chat")
    check("scope overrides rank chat before project before global",
          [f["scope"] for f in first.search("concise answers")[0]] == ["chat", "project", "global"])

    duplicate, duplicate_message = first.save(
        "Duplicate", "Use concise answers by default.", "global"
    )
    check("identical facts are not duplicated",
          duplicate["id"] == global_fact["id"] and "no duplicate" in duplicate_message)

    # A new session gets a new chat scope but can still retrieve persistent global memory.
    later = MemoryStore(root / "facts.db", global_dir, chat_dir=root / "chat-2" / "memory",
                        chat_id="chat-2")
    later._embed = fake_embedding
    recalled, _ = later.search("concise answers")
    check("a later session recalls the earlier global preference",
          any(f["id"] == global_fact["id"] for f in recalled))
    later._embed = unavailable_embedding
    fallback, warning = later.search("concise answers")
    check("unavailable embeddings report a warning and use FTS5",
          warning and any(f["id"] == global_fact["id"] for f in fallback))
    later._embed = fake_embedding
    changed_model = MemoryStore(root / "facts.db", global_dir)
    changed_model.embedding_model = "different-model"
    changed_model._embed = fake_embedding
    compatible_facts, warning = changed_model.search("concise answers")
    check("embedding model changes disable vectors without losing FTS recall",
          "differ" in warning and any(
              f["id"] == global_fact["id"] for f in compatible_facts
          ))

    # The agent adds retrieved facts to the model request without persisting the reminder.
    ws = Workspace(root / "workspace")
    ws.memory = later
    seen = []

    def fake_stream(messages, **kwargs):
        seen.extend(messages)
        yield {"type": "content", "text": "I will use concise answers."}

    real_stream = agent_module.stream_chat
    agent_module.stream_chat = fake_stream
    try:
        agent = Agent(ws)
        agent.run("What response style should I use?")
    finally:
        agent_module.stream_chat = real_stream
    check("task-start recall is included in the model context",
          any("Use concise answers by default." in str(m.get("content")) for m in seen))
    check("the transient memory reminder is not saved in the transcript",
          len(agent.messages) == 3 and all("Relevant user-approved memory" not in
                                           str(m.get("content")) for m in agent.messages))

    first.save(
        "Answer style", "Use concise answers by default. Brief.", "global"
    )
    pairs = first.duplicate_pairs()
    check("review identifies likely duplicates", bool(pairs))
    merged = first.merge(pairs[0][0]["id"], pairs[0][1]["id"])
    check("merge preserves content and removes the source Markdown page",
          not Path(pairs[0][0]["markdown_path"]).exists()
          and "brief" in Path(merged["markdown_path"]).read_text(encoding="utf-8").casefold())

    # A rejected model proposal must not reach the store; an accepted proposal must.
    calls = [
        {"id": "remember-1", "name": "remember",
         "arguments": json.dumps({"title": "Editor", "content": "Use VS Code.", "scope": "global"})},
    ]

    def proposal_stream(messages, **kwargs):
        if calls:
            call = calls.pop(0)
            yield {"type": "tool_calls", "calls": [{
                "id": call["id"], "name": call["name"], "arguments": call["arguments"],
            }]}
        else:
            yield {"type": "content", "text": "Understood."}

    agent_module.stream_chat = proposal_stream
    try:
        denied_ws = Workspace(root / "denied")
        denied_ws.memory = later
        denied = Agent(denied_ws, policy=PermissionPolicy(
            ask=lambda *args: "deny",
        ))
        denied.run("Remember that I use VS Code.")
        check("a declined memory proposal is not stored",
              not any(f["title"] == "Editor" for f in later.list_facts()))

        calls.append({
            "id": "remember-2", "name": "remember",
            "arguments": json.dumps({"title": "Editor", "content": "Use VS Code.", "scope": "global"}),
        })
        accepted = Agent(denied_ws, policy=PermissionPolicy(
            ask=lambda *args: "once",
        ))
        accepted.run("Remember that I use VS Code.")
    finally:
        agent_module.stream_chat = real_stream
    check("an accepted memory proposal is saved",
          any(f["title"] == "Editor" for f in later.list_facts()))

    # Deletion verifies the canonical file and all database search representations are gone.
    erasable, _ = later.save(
        "Erase verification", "erase-marker-6f9c2a-do-not-retain", "global"
    )
    marker = b"erase-marker-6f9c2a-do-not-retain"
    check("forget fixture is present before deletion", marker in (root / "facts.db").read_bytes())
    conn = later._connect()
    try:
        erasable_rowid = conn.execute(
            "SELECT rowid FROM facts WHERE id=?", (erasable["id"],)
        ).fetchone()[0]
    finally:
        conn.close()
    removed = later.forget(erasable["id"])
    conn = later._connect()
    try:
        check("forget removes the Markdown, FTS, vector, and fact rows",
              not Path(removed["markdown_path"]).exists()
              and conn.execute("SELECT COUNT(*) FROM facts WHERE id=?", (removed["id"],)).fetchone()[0] == 0
              and conn.execute("SELECT COUNT(*) FROM facts_fts WHERE fact_id=?",
                               (removed["id"],)).fetchone()[0] == 0
              and conn.execute("SELECT COUNT(*) FROM fact_vectors WHERE rowid=?",
                               (erasable_rowid,)).fetchone()[0] == 0)
    finally:
        conn.close()
    check("forget securely vacuums the deleted text",
          marker not in (root / "facts.db").read_bytes())
    try:
        later.forget("missing")
    except MemoryError:
        check("forget reports unknown memory ids", True)
    else:
        check("forget reports unknown memory ids", False)
    try:
        later.forget("%")
    except MemoryError:
        check("forget rejects wildcard ids", True)
    else:
        check("forget rejects wildcard ids", False)

print("\nPhase 6 exit test passed.")
