"""
search.py - glob (find files by name) and grep (find text inside files).

Both skip junk folders (.git, .venv, node_modules...) because results from
those would burn context tokens for nothing. Both also re-check every hit
against the path jail, since a symlink inside the workspace could point
somewhere outside it.
"""

import fnmatch
import itertools
import os
import re
import time
from pathlib import Path

from danica.tools.registry import tool
from danica.workspace import Workspace
from danica.secret_scan import is_sensitive_path

# Folders never worth searching
SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache"}

MAX_GLOB_RESULTS = 200
MAX_GREP_MATCHES = 100
MAX_GREP_LINE_CHARS = 300      # truncate long matching lines
MAX_GREP_FILE_BYTES = 2_000_000  # skip files bigger than ~2MB
# Open Mode can point these at a whole drive, so every scan has a budget
SCAN_MAX_ENTRIES = 100_000       # glob: directory entries looked at
GREP_MAX_FILES = 20_000          # grep: files opened
SCAN_SECONDS = 15                # either: wall-clock limit


@tool(
    "glob",
    "Find files by name pattern, e.g. '**/*.py' or 'src/*.ts'. Returns paths. "
    "In Open Mode set 'path' to search another folder, e.g. path 'D:/Projects'.",
    {
        "pattern": {"type": "string", "description": "Glob pattern, e.g. **/*.py"},
        "path": {"type": "string", "description": "Folder to search in (default: the workspace)", "default": "."},
    },
)
def glob_files(ws: Workspace, pattern: str, path: str = ".") -> str:
    # Reject patterns that could walk out of the folder BEFORE globbing.
    # (Results are also re-checked below; this is belt and braces.)
    """Find files by name pattern, e.g. '**/*.py' or 'src/*.ts'."""
    p = Path(pattern)
    parts = pattern.replace("\\", "/").split("/")
    if p.is_absolute() or p.drive or ".." in parts:
        return "ERROR: pattern must be relative and must not contain '..' (use 'path' to pick a folder)"

    base = ws.resolve(path)
    if not base.is_dir():
        return f"ERROR: '{path}' is not a folder"

    matches = []
    deadline = time.monotonic() + SCAN_SECONDS
    stopped = ""
    looked = 0                       # stays 0 when the folder is empty (the loop body never runs)
    for looked, hit in enumerate(
        itertools.islice(base.glob(pattern), SCAN_MAX_ENTRIES), start=1
    ):
        if time.monotonic() > deadline:
            stopped = f"stopped after {SCAN_SECONDS}s"
            break
        rel = hit.relative_to(base)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        # Symlink check: the real location must still be allowed (inside the jail, or not protected)
        if not ws.contains(hit.resolve()):
            continue
        if hit.is_file():
            matches.append(ws.display(hit))   # forward slashes: shorter, model-friendly
    else:
        if looked >= SCAN_MAX_ENTRIES:
            stopped = f"stopped after {SCAN_MAX_ENTRIES:,} entries"

    matches.sort()
    if not matches:
        return f"No files match '{pattern}'" + (f" ({stopped}: narrow the folder)" if stopped else "")

    shown = matches[:MAX_GLOB_RESULTS]
    out = "\n".join(shown)
    if len(matches) > MAX_GLOB_RESULTS:
        out += f"\n... and {len(matches) - MAX_GLOB_RESULTS} more (narrow the pattern)"
    if stopped:
        out += f"\n(scan {stopped}: results may be incomplete, narrow the folder)"
    return out


@tool(
    "grep",
    "Search file contents with a regular expression. Returns 'file:line: text' matches.",
    {
        "pattern": {"type": "string", "description": "Regular expression to search for"},
        "path": {"type": "string", "description": "File or folder to search (relative to the workspace; Open Mode also accepts absolute paths)", "default": "."},
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
    """Search file contents with a regular expression."""
    try:
        regex = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as e:
        # Small models write bad regexes often; tell them exactly what broke
        return f"ERROR: invalid regex: {e}"

    target = ws.resolve(path)

    # Build the list of files to scan: either one file, or a walk of a folder
    files: list[Path] = []
    scan_cut = False
    if target.is_file():
        files = [target]
    elif target.is_dir():
        deadline = time.monotonic() + SCAN_SECONDS
        for dirpath, dirnames, filenames in os.walk(target):
            # Editing dirnames in place stops os.walk from descending into them.
            # (Open Mode also never walks into a protected folder: AppData, Windows...)
            dirnames[:] = sorted(
                d
                for d in dirnames
                if d not in SKIP_DIRS and ws.contains(Path(dirpath) / d)
            )
            for name in sorted(filenames):
                if fnmatch.fnmatch(name, include):
                    files.append(Path(dirpath) / name)
            if len(files) >= GREP_MAX_FILES or time.monotonic() > deadline:
                scan_cut = True
                break
    else:
        return f"ERROR: '{path}' does not exist"

    results = []
    truncated = False
    for fp in files:
        # Jail re-check (symlinked files can point outside the workspace)
        real = fp.resolve()
        if not ws.contains(real):
            continue
        # Never feed credential files into context
        if is_sensitive_path(ws.rel(real)):
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

    cut_note = (f"\n(the scan was cut short after {len(files):,} files or {SCAN_SECONDS}s: "
                f"narrow the path)") if scan_cut else ""
    if not results:
        return f"No matches for '{pattern}'" + cut_note
    out = "\n".join(results)
    if truncated:
        out += f"\n... stopped at {MAX_GREP_MATCHES} matches (narrow the pattern or path)"
    return out + cut_note