"""
recall.py - The recall tool: keyword search over the user's saved memory notes.

Read-only: the model cannot save or change notes. Notes are the user's own text,
so unlike web pages they are not wrapped as untrusted (and they never taint).
"""

from danica.memory import MemoryStoreError
from danica.memory_index import query_terms
from danica.tools.registry import tool
from danica.workspace import Workspace

BODY_CHARS = 400


@tool(
    "recall",
    "Search the user's saved memory notes (their preferences, and facts about them and their "
    "projects) by keywords. Use it before saying you don't know something about the user or "
    "their project. Read-only: you cannot save or change notes.",
    {
        "query": {"type": "string", "description": "A few specific keywords: the topic or a name"},
        "count": {"type": "integer", "description": "How many notes (1-8)", "default": 5},
    },
)
def recall(ws: Workspace, query: str, count: int = 5) -> str:
    """Search the user's saved memory notes (their preferences, and facts about
        them and their projects) by keywords.
    """
    store = ws.memory
    if store is None or store.index is None:
        return "ERROR: memory is not available in this session"
    try:
        count = int(count)
    except (TypeError, ValueError):
        return "ERROR: 'count' must be a number"

    terms = query_terms(str(query))
    if not terms:
        return "No searchable words in that query. Use a few specific keywords, such as the topic or a name."
    try:
        hits = store.index.recall(store, str(query), count)
    except MemoryStoreError as e:
        return f"ERROR: {e}"

    shown = " ".join(terms)       # echo the cleaned words, never the raw query
    # Our own words, never note or page text: why meaning search was skipped (if it was)
    note = store.index.note
    suffix = f"\n(Keyword matches only: {note})" if note else ""
    if not hits:
        return f'No saved notes match "{shown}". Try different or fewer keywords.{suffix}'
    lines = [f'Saved notes matching "{shown}" (the user\'s own notes; when two conflict, the more '
             f'specific scope wins: chat, then project, then global):']
    for h in hits:
        lines.append(f"[{h.scope}, id {h.id}] " + " ".join(h.body.split())[:BODY_CHARS])
    return "\n".join(lines) + suffix