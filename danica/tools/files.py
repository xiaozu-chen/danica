"""
files.py - read_file, write_file, edit_file, list_dir.

Design notes for a small local model:
- read_file returns numbered lines and a line-range window, so big files
  don't flood the 65k context.
- edit_file uses exact-string replacement and refuses ambiguous matches,
  because a wrong silent edit is worse than an error the model can fix.
"""

from danica.tools.registry import tool
from danica.workspace import Workspace
from danica.secret_scan import find_secrets, is_sensitive_path

MAX_READ_LINES = 400      # hard cap per read_file call
MAX_LINE_CHARS = 2000     # truncate absurdly long lines (minified files etc.)
MAX_LIST_ENTRIES = 200    # cap directory listings
# Folders whose files run automatically on the USER's next git commit. The model
# must never write there, even in a trusted project (a planted hook = code execution).
GUARDED_DIRS = {".git", ".husky", ".githooks"}


def _read_text(path) -> str:
    """Read a UTF-8 text file, refusing binary files."""
    raw = path.read_bytes()
    if b"\x00" in raw[:8192]:  # NUL bytes in the first 8KB means binary
        raise ValueError("file looks binary, refusing to read it as text")
    return raw.decode("utf-8", errors="replace")


def _protected(ws: Workspace, p) -> str | None:
    """Error messages if p is a credential file the model must not touch."""
    reason = is_sensitive_path(ws.rel(p))
    if reason:
        return (f"ERROR: '{ws.display(p)}' is blocked: it looks like a {reason} that may "
                f"hold secrets. Do not try to read it another way. Tell the user what "
                f"you need from it instead."
                )
    return None


def _secret_check(text: str) -> str | None:
    """Error message if text contains something that looks like a real secret."""
    # Not a secret check, but this is the one gate every written text passes
    # through: refuse the model copying a pruning stub into a real file.
    if text.lstrip().startswith("[pruned"):
        return ("ERROR: refused to write this: it is a [pruned] placeholder, not real "
                "content. Read the file or re-run the tool to get the real content first.")
    kinds = sorted({kind for kind, _, _ in find_secrets(text)})
    if kinds:
        return (f"ERROR: refused to write this: it contains what looks like a secret "
               f"({', '.join(kinds)}). Use a placeholder like YOUR_API_KEY, or read the "
               f"value from an environment variable at runtime.")
    return None

def _write_guard(ws: Workspace, p) -> str | None:
    """Error message if p is inside a folder whose files run on their own."""
    if any(part.lower() in GUARDED_DIRS for part in ws.rel(p).parts):
        return (f"ERROR: '{ws.display(p)}' is blocked: files there run automatically "
                f"(git hooks). Tell the user what change you wanted instead.")
    return None

@tool(
    "read_file",
    "Read a text file. Returns numbered lines. Use offset/limit for big files.",
    {
        "path": {"type": "string", "description": "File path relative to the workspace"},
        "offset": {"type": "integer", "description": "First line to read (1-based)", "default": 1},
        "limit": {"type": "integer", "description": "Max lines to read", "default": 200},
    },
)
def read_file(ws: Workspace, path: str, offset: int = 1, limit: int = 200) -> str:
    """Read a text file."""
    p = ws.resolve(path)
    if not p.is_file():
        return f"ERROR: '{path}' is not a file (or does not exist)"

    blocked = _protected(ws, p)
    if blocked:
        return blocked

    lines = _read_text(p).splitlines()
    total = len(lines)
    offset = max(1, offset)
    limit = min(max(1, limit), MAX_READ_LINES)

    window = lines[offset - 1 : offset - 1 + limit]
    # "  12\ttext" format: line number, tab, content
    body = "\n".join(
        f"{i}\t{line[:MAX_LINE_CHARS]}" for i, line in enumerate(window, start=offset)
    )
    end = offset + len(window) - 1
    header = f"[{ws.display(p)}: lines {offset}-{end} of {total}]"
    if end < total:
        header += f" (more available: call again with offset={end + 1})"
    return f"{header}\n{body}"


@tool(
    "write_file",
    "Create a file or completely overwrite an existing one. Parent folders are created.",
    {
        "path": {"type": "string", "description": "File path relative to the workspace"},
        "content": {"type": "string", "description": "Full file contents"},
    },
)
def write_file(ws: Workspace, path: str, content: str) -> str:
    """Create a file or completely overwrite an existing one."""
    p = ws.resolve(path)
    if p.is_dir():
        return f"ERROR: '{path}' is a directory"

    guarded = _write_guard(ws, p)
    if guarded:
        return guarded

    refused = _secret_check(content)
    if refused:
        return refused
    
    existed = p.exists()
    p.parent.mkdir(parents=True, exist_ok=True)
    # newline="\n" keeps LF endings on Windows (default would write CRLF)
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    verb = "overwrote" if existed else "created"
    return f"OUTPUT: {verb} {ws.display(p)} ({len(content.splitlines())} lines)"


@tool(
    "edit_file",
    "Replace an exact string in a file. old_string must match exactly and be unique "
    "unless replace_all is true. Prefer this over write_file for small changes.",
    {
        "path": {"type": "string", "description": "File path relative to the workspace"},
        "old_string": {"type": "string", "description": "Exact text to find"},
        "new_string": {"type": "string", "description": "Replacement text"},
        "replace_all": {"type": "boolean", "description": "Replace every match", "default": False},
    },
)
def edit_file(
    ws: Workspace,
    path: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
) -> str:
    """Replace an exact string in a file. old_string must match exactly and be
        unique unless replace_all is true.
    """
    p = ws.resolve(path)
    if not p.is_file():
        return f"ERROR: '{path}' is not a file (or does not exist)"
    blocked = _protected(ws, p)
    guarded = _write_guard(ws, p)
    if guarded:
        return guarded
    if blocked:
        return blocked
    if not old_string:
        return "ERROR: old_string is empty (use write_file to create files)"
    refused = _secret_check(new_string)
    if refused:
        return refused
    if old_string == new_string:
        return "ERROR: old_string and new_string are identical, nothing to change"

    text = _read_text(p).replace("\r\n", "\n")  # normalise so \n in old_string matches
    count = text.count(old_string)

    if count == 0:
        return "ERROR: old_string not found. Re-read the file and copy the text exactly."
    if count > 1 and not replace_all:
        return (f"ERROR: old_string matches {count} places. Add surrounding lines "
                f"to make it unique, or set replace_all=true.")

    new_text = text.replace(old_string, new_string)  # replaces all; count==1 otherwise
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(new_text)
    return f"OUTPUT: edited {ws.display(p)} ({count} replacement{'s' if count != 1 else ''})"


@tool(
    "list_dir",
    "List files and folders in a directory (folders end with /).",
    {
        "path": {"type": "string", "description": "Directory relative to the workspace", "default": "."},
    },
)
def list_dir(ws: Workspace, path: str = ".") -> str:
    """List files and folders in a directory (folders end with /)."""
    p = ws.resolve(path)
    if not p.is_dir():
        return f"ERROR: '{path}' is not a directory (or does not exist)"

    # Folders first, then files, each alphabetical
    entries = sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
    shown = entries[:MAX_LIST_ENTRIES]
    lines = [e.name + ("/" if e.is_dir() else "") for e in shown]
    if len(entries) > MAX_LIST_ENTRIES:
        lines.append(f"... and {len(entries) - MAX_LIST_ENTRIES} more")
    return f"[{ws.display(p)}]\n" + ("\n".join(lines) if lines else "(empty)")
