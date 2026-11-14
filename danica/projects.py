"""
projects.py - Registering (trusting) real folders as Danica projects.

A registered project gets a folder under %APPDATA%/Danica/projects/<hash>/:
    project.json   the trusted path and when it was trusted
    memory/        project memory (used from Phase 6)
    git/           the shadow git repo for /undo and /rewind (see checkpoints.py)

Registration IS the trust decision, so the CLI asks the user first. Nothing
in this file talks to the model.

HONEST LIMIT: a project.json that is edited by hand is only partly checked
(the stored path must match the folder). It protects against accidents, not
against someone with write access to your AppData folder.
"""

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from danica.config import APP_DIR
from danica.secret_scan import redact
from danica.web import clean_text

PROJECTS_DIR = APP_DIR / "projects"

# Folders skipped when counting files (same idea as the search tools)
SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache"}
SCAN_LIMIT = 20000   # stop counting after this many files (the scan is advice, not a gate)


class ProjectError(Exception):
    """A problem whose message is safe to show the user."""


def _norm(path: Path) -> str:
    """One canonical spelling of a path. normcase makes Windows paths case-insensitive."""
    return os.path.normcase(str(Path(path).resolve()))


def project_id(path: Path) -> str:
    """Short stable id for a folder (the same folder always gives the same id)."""
    return hashlib.sha256(_norm(path).encode("utf-8")).hexdigest()[:12]


def check_allowed(path: Path) -> Path:
    """Return the resolved folder, or raise ProjectError if it must not be a project."""
    p = Path(path).expanduser().resolve()
    if not p.is_dir():
        raise ProjectError(f"'{p}' is not a folder that exists")
    if p == Path(p.anchor):
        raise ProjectError("a whole drive can't be a project: pick a specific folder")

    home = Path.home().resolve()
    if p == home or p in home.parents:
        raise ProjectError("your home folder (or anything above it) is too broad to be a project")

    # System folders
    for var in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData"):
        if var in os.environ:
            system = Path(os.environ[var]).resolve()
            if p == system or p.is_relative_to(system):
                raise ProjectError("system folders can't be projects")

    # Danica's own data (sessions, audit log, shadow repos)
    mine = APP_DIR.resolve()
    if p == mine or p.is_relative_to(mine) or mine.is_relative_to(p):
        raise ProjectError("Danica's own data folder can't be a project")
    return p


def lookup(path: Path, root: Path = PROJECTS_DIR) -> dict | None:
    """The project record for a trusted folder, or None if it was never trusted
    (or its record is damaged). The result gains a "dir" key: the project's data folder."""
    folder = Path(root) / project_id(path)
    try:
        info = json.loads((folder / "project.json").read_text(encoding="utf-8"))
        if info.get("path_norm") != _norm(path):    # a record that doesn't match its folder
            return None
        info["dir"] = str(folder)
        return info
    except (OSError, ValueError, AttributeError):
        return None


def register(path: Path, root: Path = PROJECTS_DIR) -> dict:
    """Trust a folder: create its data folder and record. Safe to call twice."""
    p = check_allowed(path)
    existing = lookup(p, root)
    if existing:
        return existing

    folder = Path(root) / project_id(p)
    (folder / "memory").mkdir(parents=True, exist_ok=True)
    info = {
        "id": project_id(p),
        "path": str(p),
        "path_norm": _norm(p),
        "name": p.name or str(p),
        "trusted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    tmp = folder / "project.tmp"
    tmp.write_text(json.dumps(info, indent=2), encoding="utf-8")
    os.replace(tmp, folder / "project.json")        # a crash never leaves half a file
    info["dir"] = str(folder)
    return info


def list_projects(root: Path = PROJECTS_DIR) -> list[dict]:
    """Every registered project, newest trust first."""
    found = []
    if Path(root).exists():
        for folder in Path(root).iterdir():
            try:
                info = json.loads((folder / "project.json").read_text(encoding="utf-8"))
                info["dir"] = str(folder)
                found.append(info)
            except (OSError, ValueError):
                continue
    return sorted(found, key=lambda i: i.get("trusted_at", ""), reverse=True)


def scan(path: Path) -> tuple[int, int, bool, bool]:
    """Quick look at a folder for the trust dialog.
    Returns (file_count, total_bytes, stopped_early, has_git_repo)."""
    files = size = 0
    has_git = (Path(path) / ".git").exists()
    for dirpath, dirnames, filenames in os.walk(path):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]   # don't descend into junk
        for name in filenames:
            files += 1
            try:
                size += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                pass
            if files >= SCAN_LIMIT:
                return files, size, True, has_git
    return files, size, False, has_git


# ------------------------------------------------------------ per-session mode

def save_session_meta(session_dir: Path, project_path: str | None, open_root: str | None = None) -> None:
    """Remember which folder a session worked in (None = a normal chat session).
    open_root: the START folder of an Open Mode session. Only the folder is remembered: Open
    Mode itself is never switched on again without asking (see open_resumed)."""
    meta = {"mode": "open" if open_root else "project" if project_path else "chat",
            "project": project_path, "open_root": open_root}
    Path(session_dir, "session.json").write_text(json.dumps(meta), encoding="utf-8")


def load_session_meta(session_dir: Path) -> dict:
    """{} for old sessions that have no session.json (they were chat sessions)."""
    try:
        meta = json.loads(Path(session_dir, "session.json").read_text(encoding="utf-8"))
        return meta if isinstance(meta, dict) else {}
    except (OSError, ValueError):
        return {}


# ------------------------------------------------------- project instructions

INSTRUCTIONS_FILE = "DANICA.md"
INSTRUCTIONS_MAX = 4000      # characters kept (about 1,100 tokens, paid on every call)


def read_instructions(project_path: str | Path) -> tuple[str, str, bool] | None:
    """The cleaned text of DANICA.md as (text, sha256_of_that_text, was_cut), or None.
    "Cleaned" = invisible characters removed, secrets redacted, cut to the limit.
    The hash covers exactly what the model would see, so approving a hash approves that text."""
    root = Path(project_path).resolve()
    f = root / INSTRUCTIONS_FILE
    try:
        # A symlink must not smuggle another file (a key, say) into the prompt
        if not f.is_file() or not f.resolve().is_relative_to(root):
            return None
        raw = f.read_bytes()[:100_000]
    except OSError:
        return None
    text, _ = clean_text(raw.decode("utf-8-sig", errors="replace"))
    text, _ = redact(text)
    cut = len(text) > INSTRUCTIONS_MAX
    text = text[:INSTRUCTIONS_MAX]
    if not text.strip():
        return None
    return text, hashlib.sha256(text.encode("utf-8")).hexdigest(), cut


def approve_instructions(info: dict, sha: str) -> None:
    """Record that the user saw and approved the instruction text with this hash."""
    record = Path(info["dir"]) / "project.json"
    data = json.loads(record.read_text(encoding="utf-8"))
    data["instructions_sha"] = sha
    tmp = record.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, record)             # a crash never leaves half a file
    info["instructions_sha"] = sha      # keep the in-memory record in step