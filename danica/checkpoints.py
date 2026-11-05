"""
checkpoints.py - Shadow-git snapshots of the workspace, for /undo and /rewind.

The git directory lives OUTSIDE the workspace (so the model can't touch it)
and the workspace is its "work tree". Danica never creates or modifies a
.git folder inside the user's files.

LIMITS: only files inside the workspace are covered. Things a shell command
did elsewhere, installed packages and running processes are NOT undone.
Folders listed in EXCLUDES are skipped, so they are neither saved nor restored.
"""

import os
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from danica.workspace import Workspace
from danica.pathsearch import find_program

# Never snapshotted, never touched by a restore (keeps snapshots fast and
# stops an undo from wiping your virtualenv). ".git/" also means a real
# repo inside the workspace is left strictly alone.
EXCLUDES = [".git/", ".venv/", "node_modules/", "__pycache__/",
            ".mypy_cache/", ".pytest_cache/"]

MAX_STACK = 50   # remember at most this many requests for /undo

# Readable words for `git diff --name-status` letters, from the UNDO's point
# of view (the diff goes restore-target -> current state).
ACTION_WORDS = {"A": "removed", "D": "restored", "M": "reverted"}


class CheckpointError(Exception):
    """A git operation failed. The message is safe to show the user."""


class Checkpoints:
    def __init__(self, ws: Workspace, git_dir: Path):
        self.ws = ws
        self.git_dir = Path(git_dir)
        # One entry per request: the snapshot id from BEFORE it ran,
        # or None if taking that snapshot failed.
        self.undo_stack: list[str | None] = []
        self.git = find_program("git")           # never searches the current directory (see pathsearch.py)
        self.available = self.git is not None
        if self.available:
            self._init_repo()

    # ------------------------------------------------------------ git plumbing

    def _git(self, *args: str, check: bool = True) -> str:
        """Run git against the shadow repo and return stdout."""
        # GIT_DIR / GIT_WORK_TREE point git at the shadow repo and the workspace.
        # These go ONLY to this child process, never to the model's shell.
        env = {
            **os.environ,
            "GIT_DIR": str(self.git_dir),
            "GIT_WORK_TREE": str(self.ws.root),
            "GIT_TERMINAL_PROMPT": "0",     # never wait for a password prompt
        }
        cmd = [
            self.git,
            "-c", "core.autocrlf=false",    # store bytes exactly as they are
            "-c", "core.safecrlf=false",    # no line-ending warnings
            "-c", "core.quotepath=false",   # readable non-ASCII file names
            "-c", "user.name=Danica",       # commits need an identity
            "-c", "user.email=danica@localhost",
            "-c", "commit.gpgsign=false",
            *args,
        ]
        proc = subprocess.run(
            cmd, cwd=self.ws.root, env=env,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            stdin=subprocess.DEVNULL, timeout=120,
        )
        if check and proc.returncode != 0:
            raise CheckpointError(f"git {args[0]} failed: {proc.stderr.strip()[:300]}")
        return proc.stdout.strip()

    def _init_repo(self) -> None:
        """Create the shadow repo once, and (re)write its ignore list."""
        if not (self.git_dir / "HEAD").exists():
            self.git_dir.mkdir(parents=True, exist_ok=True)
            self._git("init", "-q")
        # info/exclude is git's private ignore file: it never touches the workspace
        (self.git_dir / "info").mkdir(exist_ok=True)
        (self.git_dir / "info" / "exclude").write_text("\n".join(EXCLUDES) + "\n", encoding="utf-8")

    # --------------------------------------------------------------- snapshots

    def snapshot(self, label: str) -> str:
        """Commit the whole workspace as it is right now. Returns the short id."""
        self._git("add", "-A")
        # --allow-empty: a request that changed nothing still gets a checkpoint
        self._git("commit", "-q", "--allow-empty", "-m", " ".join(label.split())[:100])
        return self._git("rev-parse", "--short=8", "HEAD")

    def begin_request(self, user_text: str) -> str:
        """Called at the start of every request: snapshot, then remember it for /undo."""
        try:
            snap = self.snapshot("before: " + " ".join(user_text.split())[:80])
        except Exception:
            self.undo_stack.append(None)   # keep the stack aligned with requests
            raise
        self.undo_stack.append(snap)
        del self.undo_stack[:-MAX_STACK]   # drop the oldest beyond the cap
        return snap

    def changed_since(self, snap: str) -> list[tuple[str, str]]:
        """Files that differ now from checkpoint `snap`, as (status, path): A new, M changed, D deleted.
        Includes files made by shell scripts. Respects the project's .gitignore (ignored files are
        invisible here). Stages the workspace in the SHADOW repo only; nothing is committed."""
        if not re.fullmatch(r"[0-9a-f]{4,40}", snap or ""):
            raise CheckpointError("not a checkpoint id")      # the id goes onto a git command line
        self._git("add", "-A")
        out = self._git("diff", "--cached", "--no-renames", "--name-status", snap)
        rows = []
        for line in out.splitlines():
            status, _, name = line.partition("\t")
            if name:
                rows.append((status[:1], name))
        return rows

    def list_checkpoints(self, limit: int = 15) -> list[tuple[str, str, str]]:
        """Newest first: (id, time, label). --all also lists the safety copies."""
        out = self._git("log", "--all", "-n", str(limit), "--abbrev=8",
                        "--format=%h%x09%ct%x09%s", check=False)
        rows = []
        for line in out.splitlines():
            ident, stamp, label = line.split("\t", 2)
            when = datetime.fromtimestamp(int(stamp)).strftime("%d %b %H:%M")
            rows.append((ident, when, label))
        return rows

    # ----------------------------------------------------------------- restore

    def _restore(self, target: str) -> tuple[str, str]:
        """Make the workspace match snapshot `target`. Returns (summary, safety_id)."""
        label = self._git("log", "-1", "--format=%s", target)

        # Safety copy of the CURRENT state first, so this restore can be undone.
        safety = self.snapshot("safety copy before restore")
        # A tag keeps the safety commit alive even though the branch is about
        # to move backwards (git would otherwise garbage-collect it one day).
        self._git("tag", f"safety-{safety}", safety, check=False)

        # What will change? Diff goes target -> current, so "A" = added by the
        # request (we will remove it) and "D" = deleted by it (we will restore it).
        changes = self._git("diff", "--no-renames", "--name-status", target, safety)

        self._git("reset", "--hard", "-q", target)   # tracked files back to target
        self._git("clean", "-fdq")                    # drop leftover untracked files/folders

        lines = [f"Restored the workspace to: {label}"]
        rows = changes.splitlines()
        if not rows:
            lines.append("No net file changes: the workspace looked the same before and after "
                         "that request (anything it created and then deleted cancels out).")
        for row in rows[:12]:
            status, name = row.split("\t", 1)
            lines.append(f"  {ACTION_WORDS.get(status, status):<9} {name}")
        if len(rows) > 12:
            lines.append(f"  ... and {len(rows) - 12} more")
        lines.append(f"To undo this restore: /rewind {safety}")
        return "\n".join(lines), safety

    def undo(self) -> tuple[str, str | None]:
        """Revert the most recent request that has not been undone yet."""
        if not self.undo_stack:
            return "Nothing to undo.", None
        target = self.undo_stack.pop()
        if target is None:
            return "That request has no checkpoint (the snapshot failed), so it can't be undone.", None
        return self._restore(target)

    def rewind(self, checkpoint_id: str) -> tuple[str, str]:
        """Jump to any checkpoint from /rewind's list."""
        # Strict format check: the id goes straight onto a git command line
        if not re.fullmatch(r"[0-9a-f]{4,40}", checkpoint_id):
            raise CheckpointError(f"'{checkpoint_id}' is not a checkpoint id (use /rewind to list them)")
        if self._git("cat-file", "-t", checkpoint_id + "^{commit}", check=False) != "commit":
            raise CheckpointError(f"no checkpoint with id '{checkpoint_id}'")
        self.undo_stack.clear()   # the old stack no longer matches the new timeline
        return self._restore(checkpoint_id)