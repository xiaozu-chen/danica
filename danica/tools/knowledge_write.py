"""Preview-only Obsidian write-back; final writes require CLI confirmation."""

from pathlib import Path

from danica.knowledge import KnowledgeError
from danica.tools.registry import tool
from danica.workspace import Workspace


def _default_vault(index) -> tuple[str, str]:
    """(folder, "") for the vault to write into, or ("", error text for the model).

    The harness knows the vault only because the USER ran `/knowledge index <folder>`; that folder is saved in
    knowledge.db and survives restarts. The model is never told the path, so when it leaves `folder` out we pick:
      * no folder indexed          -> tell it to ask the user to index the vault
      * one folder                 -> that one
      * several, one has .obsidian -> that one (Obsidian marks a real vault with a .obsidian folder)
      * several, no clear winner   -> list them so the model can ask the user which
    """
    roots = index.roots()
    if not roots:
        return "", ("ERROR: no notes folder is indexed yet. Ask the user to run /knowledge index <path of their "
                    "Obsidian vault> first, then try again.")
    vaults = [r for r in roots if (Path(r) / ".obsidian").is_dir()] or roots
    if len(vaults) == 1:
        return str(vaults[0]), ""
    listing = "; ".join(str(r) for r in vaults)
    return "", f"ERROR: several folders are indexed ({listing}). Ask the user which one to use (ask_user), then call again with folder set."


@tool("obsidian_write_preview",
      "Create a preview for an Obsidian Markdown note in the user's indexed vault. "
      "The user must run /knowledge commit <preview-id> to review and confirm the write. "
      "Leave folder empty to use the user's vault (you do not know its path; the harness finds it).",
      {"path": {"type": "string", "description": "Relative Markdown path inside the vault, for example Ideas/Danica.md"},
       "title": {"type": "string", "description": "Note title"},
       "body": {"type": "string", "description": "Markdown note body"},
       "folder": {"type": "string", "default": "",
                  "description": "Only if several folders are indexed: the vault folder the user chose. Otherwise leave empty."},
       "wikilinks": {"type": "array", "items": {"type": "string"}, "default": [],
                     "description": "Optional related-note names for [[wikilinks]]"}})
def obsidian_write_preview(ws: Workspace, path: str, title: str, body: str, folder: str = "",
                           wikilinks: list[str] | None = None) -> str:
    """Create a preview for an Obsidian Markdown note in the user's indexed vault."""
    index = getattr(ws, "knowledge_index", None)
    if index is None:
        return "ERROR: local knowledge index is unavailable in this session."
    if not str(folder or "").strip():
        folder, problem = _default_vault(index)
        if problem:
            return problem
    try:
        preview_id, content, exists = index.preview_obsidian_write(
            ws, folder, path, title, body, wikilinks)
    except KnowledgeError as exc:
        return f"ERROR: write-back preview failed: {exc}"
    status = "existing file would be replaced" if exists else "new file"
    return (f"Preview {preview_id} ({status}) in {folder}. No file has been changed. "
            f"After reviewing it, the user must run /knowledge commit {preview_id}.\n\n"
            f"--- BEGIN PREVIEW ---\n{content}--- END PREVIEW ---")
