"""
search.py - glob (find files by name) and grep (find text inside files).

Both skip junk folders (.git, .venv, node_modules...) because results from
those would burn context tokens for nothing. Both also re-check every hit
against the path jail, since a symlink inside the workspace could point
somewhere outside it.
"""

import fnmatch
import os
import re
from pathlib import Path

from danica.tools.registry import tool
from danica.workspace import Workspace

# Folders never worth searching
SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache"}

MAX_GLOB_RESULTS = 200
MAX_GREP_MATCHES = 100
MAX_GREP_LINE_CHARS = 300      # truncate long matching lines
MAX_GREP_FILE_BYTES = 2_000_000  # skip files bigger than ~2MB


@tool(
    "glob",
    "Find files by name pattern, e.g. '**/*.py' or 'src/*.ts'. Returns workspace-relative paths.",
    {
        "pattern": {"type": "string", "description": "Glob pattern, e.g. **/*.py"},
    },
)
def glob_files(ws: Workspace, pattern: str) -> str:
    # Reject patterns that could walk out of the workspace BEFORE globbing.
    # (Results are also re-checked below; this is belt and braces.)
    p = Path(pattern)
    parts = pattern.replace("\\", "/").split("/")
    if p.is_absolute() or p.drive or ".." in parts:
        return "ERROR: pattern must be relative and must not contain '..'"

    matches = []
    for hit in ws.root.glob(pattern):
        rel = hit.relative_to(ws.root)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        # Symlink check: the real location must still be inside the jail
        if not hit.resolve().is_relative_to(ws.root):
            continue
        if hit.is_file():
            matches.append(rel.as_posix())  # forward slashes: shorter, model-friendly

    matches.sort()
    if not matches:
        return f"No files match '{pattern}'"

    shown = matches[:MAX_GLOB_RESULTS]
    out = "\n".join(shown)
    if len(matches) > MAX_GLOB_RESULTS:
        out += f"\n... and {len(matches) - MAX_GLOB_RESULTS} more (narrow the pattern)"
    return out


@tool(
    "grep",
    "Search file contents with a regular expression. Returns 'file:line: text' matches.",
    {
        "pattern": {"type": "string", "description": "Regular expression to search for"},
        "path": {"type": "string", "description": "File or folder to search, relative to the workspace", "default": "."},
        "include": {"type": "string", "description": "Only search files matching this name pattern, e.g. *.py", "default": "*"},
        "ignore_case": {"type": "boolean", "description": "Case-insensitive search", "default": False},
    },
)
def grep(
    ws: Workspace,
    pattern: str,
    path: str = ".",
    include: str = "*",
    ignore_case: bool = False,
) -> str:
    try:
        regex = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as e:
        # Small models write bad regexes often; tell them exactly what broke
        return f"ERROR: invalid regex: {e}"

    target = ws.resolve(path)

    # Build the list of files to scan: either one file, or a walk of a folder
    files: list[Path] = []
    if target.is_file():
        files = [target]
    elif target.is_dir():
        for dirpath, dirnames, filenames in os.walk(target):
            # Editing dirnames in place stops os.walk from descending into them
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            for name in sorted(filenames):
                if fnmatch.fnmatch(name, include):
                    files.append(Path(dirpath) / name)
    else:
        return f"ERROR: '{path}' does not exist"

    results = []
    truncated = False
    for fp in files:
        # Jail re-check (symlinked files can point outside the workspace)
        real = fp.resolve()
        if not real.is_relative_to(ws.root):
            continue
        try:
            if real.stat().st_size > MAX_GREP_FILE_BYTES:
                continue
            raw = real.read_bytes()
        except OSError:
            continue  # unreadable file (locked, permissions): just skip it
        if b"\x00" in raw[:8192]:  # binary file
            continue

        text = raw.decode("utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                results.append(f"{ws.display(real)}:{lineno}: {line.strip()[:MAX_GREP_LINE_CHARS]}")
                if len(results) >= MAX_GREP_MATCHES:
                    truncated = True
                    break
        if truncated:
            break

    if not results:
        return f"No matches for '{pattern}'"
    out = "\n".join(results)
    if truncated:
        out += f"\n... stopped at {MAX_GREP_MATCHES} matches (narrow the pattern or path)"
    return out