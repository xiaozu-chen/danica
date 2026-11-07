"""Preview-only Obsidian write-back; final writes require CLI confirmation."""

from danica.knowledge import KnowledgeError
from danica.tools.registry import tool


@tool("obsidian_write_preview",
      "Create a preview for an Obsidian Markdown note in a user-indexed vault. "
      "The user must run /knowledge commit <preview-id> to review and confirm the write.",
      {"folder": {"type": "string", "description": "Path of the indexed Obsidian vault"},
       "path": {"type": "string", "description": "Relative Markdown path inside the vault"},
       "title": {"type": "string", "description": "Note title"},
       "body": {"type": "string", "description": "Markdown note body"},
       "wikilinks": {"type": "array", "items": {"type": "string"}, "default": [],
                     "description": "Optional related-note names for [[wikilinks]]"}})
def obsidian_write_preview(ws, folder: str, path: str, title: str, body: str,
                           wikilinks: list[str] | None = None) -> str:
    index = getattr(ws, "knowledge_index", None)
    if index is None:
        return "ERROR: local knowledge index is unavailable in this session."
    try:
        preview_id, content, exists = index.preview_obsidian_write(
            ws, folder, path, title, body, wikilinks)
    except KnowledgeError as exc:
        return f"ERROR: write-back preview failed: {exc}"
    status = "existing file would be replaced" if exists else "new file"
    return (f"Preview {preview_id} ({status}). No file has been changed. "
            f"After reviewing it, the user must run /knowledge commit {preview_id}.\n\n"
            f"--- BEGIN PREVIEW ---\n{content}--- END PREVIEW ---")
