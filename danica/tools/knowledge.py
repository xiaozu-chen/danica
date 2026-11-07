"""Read-only local knowledge search."""

from danica.knowledge import KnowledgeError, render_hits
from danica.tools.registry import tool


@tool("knowledge_search", "Search user-indexed local Markdown, Obsidian, and PDF sources. Cite passages with their [K...] id.",
      {"query": {"type": "string", "description": "Terms or a question to search for"},
       "limit": {"type": "integer", "default": 6, "description": "Maximum passages (1-12)"}})
def knowledge_search(ws, query: str, limit: int = 6) -> str:
    index = getattr(ws, "knowledge_index", None)
    if index is None:
        return "ERROR: local knowledge search is unavailable in this session."
    try:
        hits = index.search(query, limit)
    except KnowledgeError as exc:
        return f"ERROR: knowledge search failed: {exc}"
    ws.knowledge_sources.update({hit.id: hit for hit in hits})
    result = render_hits(hits)
    if index.embedding_note:
        result += f"\n\nNote: semantic search unavailable: {index.embedding_note}"
    return result
