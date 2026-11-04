"""
workspace.py - The path jail.

Every file tool gets paths from the model, and the model is not trusted.
This class turns a model-supplied path into a real path, and refuses
anything that lands outside the workspace folder.
"""

from pathlib import Path


class PathJailError(Exception):
    """Raised when a path tries to escape the workspace."""


class Workspace:
    def __init__(self, root: Path, todo_path: Path | None = None, sources_path: Path | None = None):
        # resolve() makes the root absolute and follows symlinks, so later
        # comparisons are against the *real* location on disk.
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        # Where this session's TODO.md lives (OUTSIDE the jail, on purpose).
        # None = no scratchpad (tests that don't need one).
        self.todo_path = Path(todo_path) if todo_path else None
        # Where fetched web sources are saved so [S#] ids survive /resume.
        # None = kept in memory only (tests).
        self.sources_path = Path(sources_path) if sources_path else None
        # Per-session web state (sources, cache, taint flag). Created on first
        # use by the web tools, so sessions that never browse pay nothing.
        self.web = None
        # The session's MemoryStore (set by the CLI) so the recall tool can reach it. None = no memory.
        self.memory = None

    def resolve(self, user_path: str) -> Path:
        """
        Convert a model-supplied path into a safe absolute Path.
        Relative paths are interpreted relative to the workspace root.
        Raises PathJailError if the final location is outside the root.
        """
        # If user_path is absolute (C:\Windows\...), the "/" operator throws
        # the root away and keeps the absolute path. That's fine: the check
        # below catches it.
        candidate = (self.root / (user_path or ".")).resolve()

        # resolve() has already collapsed ".." and followed symlinks, so
        # "..\\..\\secret.txt" and symlink tricks both end up outside root.
        # is_relative_to compares Windows paths case-insensitively.
        if not candidate.is_relative_to(self.root):
            raise PathJailError(f"path '{user_path}' is outside the workspace")
        return candidate

    def display(self, path: Path) -> str:
        """Workspace-relative path for showing to the model (short, no drive letters)."""
        # as_posix() forces forward slashes on Windows, so every tool shows
        # the same path style (glob already did). Empty relative path means root.
        return path.relative_to(self.root).as_posix()