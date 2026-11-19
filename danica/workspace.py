"""
workspace.py - The path jail.

Every file tool gets paths from the model, and the model is not trusted.
This class turns a model-supplied path into a real path, and refuses
anything that lands outside the workspace folder.

OPEN MODE: with open_mode=True the folder is no longer a jail. `root` becomes the START
folder (where relative paths begin), and the only paths refused are the protected areas listed
in openmode.py (Windows folders, AppData, credentials...). Everything else on disk resolves.
"""

from pathlib import Path

from danica.openmode import protected_reason


class PathJailError(Exception):
    """Raised when a path tries to escape the workspace."""


class Workspace:
    """Resolve and constrain filesystem operations to a workspace root."""

    def __init__(
        self,
        root: Path,
        todo_path: Path | None = None,
        sources_path: Path | None = None,
        open_mode: bool = False,
    ):
        # resolve() makes the root absolute and follows symlinks, so later
        # comparisons are against the *real* location on disk.
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        # True = Open Mode: the whole disk is reachable except the protected areas (openmode.py)
        self.open_mode = open_mode
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
        # Set by the CLI in sessions that may delegate; worker workspaces keep this unset.
        self.subagent_supervisor = None
        self.knowledge_index = None
        self.knowledge_sources = {}
        self.knowledge_previews = {}
        self.extension_manager = None

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

        if self.open_mode:
            # No jail, but the protected areas stay closed. This runs on the RESOLVED path, so
            # "..", symlinks and junctions can't smuggle a protected folder in.
            reason = protected_reason(candidate)
            if reason:
                raise PathJailError(f"path '{user_path}' is {reason}, which Open Mode never opens")
            return candidate

        # resolve() has already collapsed ".." and followed symlinks, so
        # "..\\..\\secret.txt" and symlink tricks both end up outside root.
        # is_relative_to compares Windows paths case-insensitively.
        if not candidate.is_relative_to(self.root):
            raise PathJailError(f"path '{user_path}' is outside the workspace")
        return candidate

    def contains(self, path: Path) -> bool:
        """Is this (resolved) path one the tools may use? The jail check for results that were
        found by walking (glob, grep), where a symlink could lead anywhere."""
        if self.open_mode:
            return protected_reason(path) is None
        return path.is_relative_to(self.root)

    def inside_start(self, path: Path) -> bool:
        """Open Mode: is this path inside the START folder? (Writes elsewhere ask first.)"""
        return path.is_relative_to(self.root)

    def rel(self, path: Path) -> Path:
        """The path to feed sensitive-name checks: relative to the root when inside it, else the
        full path (its folder names still count: ...\\.ssh\\id_rsa)."""
        return path.relative_to(self.root) if path.is_relative_to(self.root) else path

    def display(self, path: Path) -> str:
        """Workspace-relative path for showing to the model (short, no drive letters)."""
        # as_posix() forces forward slashes on Windows, so every tool shows
        # the same path style (glob already did). Empty relative path means root.
        # Open Mode: a path outside the start folder is shown in full (D:/Notes/todo.txt).
        if path.is_relative_to(self.root):
            return path.relative_to(self.root).as_posix()
        return path.as_posix()