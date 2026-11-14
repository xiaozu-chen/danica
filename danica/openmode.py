"""
openmode.py - What Open Mode may never touch.

Chat Mode  : Danica lives in a throwaway folder.
Project Mode: Danica may use ONE trusted folder.
Open Mode  : Danica may use the whole disk, EXCEPT the protected areas defined here.

This file only DECIDES "is this path protected?". It is enforced in two places:
  - Workspace.resolve()  : every file tool goes through it, so a protected path is refused
                           before any tool code runs. This holds even with /fullaccess on.
  - permissions.py       : a shell command that MENTIONS a protected path always asks.

HONEST LIMIT: file tools are airtight (the path is resolved first, so ".." tricks, symlinks and
junctions all end up at the real location). A shell command is not: a program can build a path
at run time, and we only see the command text. That is why shell commands still ask in Open Mode.

What is protected (it cannot be loosened for the system areas, only ADDED to in config.toml):
  - the Windows folder, Program Files (both), ProgramData, boot/recovery/system-restore folders
  - the page file, hibernation file and the registry hive file NTUSER.DAT
  - AppData (browser profiles, password managers, tokens) and Danica's own data
  - other users' profile folders
  - credential folders (.ssh, .aws, .gnupg, .azure, .kube)
  - network (UNC) paths, device paths (\\\\.\\PhysicalDrive0) and NTFS alternate data streams
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path

from danica.config import APP_DIR, CONFIG
from danica.secret_scan import SENSITIVE_DIRS

# Names that exist at a drive root and are never the user's files
ROOT_ONLY_NAMES = {"$recycle.bin", "system volume information", "recovery", "boot", "windows.old",
                   "pagefile.sys", "hiberfil.sys", "swapfile.sys", "bootmgr", "$winreagent"}
# Registry hive files in a profile
PROFILE_FILE_PREFIXES = ("ntuser.dat",)
# Windows reserved device names (CON, NUL, COM1...): opening one is not opening a file
DEVICE_NAMES = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


def _env(name: str) -> Path | None:
    value = os.environ.get(name)
    if not value:
        return None
    try:
        return Path(value).resolve()
    except OSError:
        return None


@lru_cache(maxsize=1)
def protected_roots() -> tuple[tuple[Path, str], ...]:
    """(folder, why) pairs. Cached: call reset_cache() after changing the environment or config."""
    roots: list[tuple[Path | None, str]] = []
    if os.name == "nt":
        for var, why in (("SystemRoot", "the Windows folder"), ("windir", "the Windows folder"),
                         ("ProgramFiles", "Program Files"), ("ProgramFiles(x86)", "Program Files"),
                         ("ProgramW6432", "Program Files"), ("ProgramData", "ProgramData"),
                         ("APPDATA", "AppData (browser profiles, tokens, password managers)"),
                         ("LOCALAPPDATA", "AppData (browser profiles, tokens, password managers)")):
            roots.append((_env(var), why))
    else:
        for folder in ("/etc", "/proc", "/sys", "/dev", "/boot", "/run", "/var/lib", "/root"):
            roots.append((Path(folder), "a system folder"))
        home = Path.home()
        for sub in (".config", ".local/share", ".mozilla", ".cache", "Library"):
            roots.append((home / sub, "application data (browser profiles, tokens)"))
    roots.append((APP_DIR, "Danica's own data (sessions, logs, settings)"))
    for extra in CONFIG.get("open_mode", {}).get("block", []):      # your own additions
        try:
            roots.append((Path(str(extra)).expanduser().resolve(), "blocked in config.toml"))
        except OSError:
            continue
    found = []
    for folder, why in roots:
        if folder is None:
            continue
        try:
            found.append((folder.resolve(), why))
        except OSError:
            continue
    return tuple(found)


def reset_cache() -> None:
    protected_roots.cache_clear()
    _home_parent.cache_clear()


@lru_cache(maxsize=1)
def _home_parent() -> tuple[Path, Path] | None:
    """(users folder, my profile) so that OTHER profiles can be refused."""
    try:
        home = Path.home().resolve()
    except (OSError, RuntimeError):
        return None
    return (home.parent, home) if home.parent != home else None


def protected_reason(path: Path) -> str | None:
    """Why this (already resolved) path is off limits in Open Mode, or None if it is allowed."""
    text = str(path)
    if os.name == "nt":
        if text.startswith("\\\\"):
            return "a network or device path"
        if ":" in text[2:]:
            return "an NTFS alternate data stream"
    for part in path.parts[1:]:
        low = part.lower()
        if low.split(".")[0] in DEVICE_NAMES and os.name == "nt":
            return "a reserved device name"
        if low in SENSITIVE_DIRS:
            return "a credentials folder"
    if len(path.parts) >= 2 and path.parts[1].lower() in ROOT_ONLY_NAMES:
        return "a system file or folder"
    if path.name.lower().startswith(PROFILE_FILE_PREFIXES):
        return "a registry file"
    for folder, why in protected_roots():
        if path == folder or path.is_relative_to(folder):
            return why
    users = _home_parent()
    if users and os.name == "nt":
        users_dir, mine = users
        if path.is_relative_to(users_dir) and path != users_dir and not path.is_relative_to(mine):
            return "another user's profile"
    return None


# ------------------------------------------------------------------ shell commands

_QUOTED = re.compile(r"'([^']+)'|\"([^\"]+)\"")
_SPLIT = re.compile(r"[\s;|&(),=]+")
_PS_ENV = re.compile(r"\$env:(\w+)", re.IGNORECASE)


def _expand(token: str) -> str:
    token = _PS_ENV.sub(lambda m: os.environ.get(m.group(1), m.group(0)), token)
    return os.path.expandvars(os.path.expanduser(token))


def shell_protected_hit(command: str, base: Path) -> tuple[str, str] | None:
    """(the path as written, why) if a shell command MENTIONS a protected path, else None.
    Pattern matching on the command text: it catches honest mistakes and lazy attacks, not a
    program that builds its path at run time (see the HONEST LIMIT at the top)."""
    candidates = [a or b for a, b in _QUOTED.findall(command)]       # quoted: may contain spaces
    candidates += _SPLIT.split(_QUOTED.sub(" ", command))
    for raw in candidates:
        token = raw.strip("'\" ")
        if not token or not re.search(r"[\\/:~%$.]", token):
            continue                                                    # not path-shaped
        try:
            resolved = (base / _expand(token)).resolve()
        except (OSError, ValueError, RuntimeError):
            continue
        reason = protected_reason(resolved)
        if reason:
            return token, reason
    return None


# ------------------------------------------------------------------ the anchor folder

def anchor_dir() -> Path:
    """Danica's home base in Open Mode: ~/.danica (override: [open_mode] anchor = "D:/danica-home").
    It is used (1) as the START folder when the folder you launched from can't be (protected or too
    broad to snapshot) and (2) as the place generated files go (see outputs_dir). It is deliberately
    NOT the AppData folder: that one holds sessions/logs and stays protected."""
    custom = CONFIG.get("open_mode", {}).get("anchor")
    base = Path(str(custom)).expanduser() if custom else Path.home() / ".danica"
    try:
        return base.resolve()
    except OSError:
        return base


def outputs_dir() -> Path:
    """Where Danica saves files it GENERATES in Open Mode (decks, charts, documents, scratch)."""
    return anchor_dir() / "outputs"


def ensure_anchor() -> Path:
    """Create ~/.danica/outputs if missing and return the anchor. Safe to call repeatedly."""
    outputs_dir().mkdir(parents=True, exist_ok=True)
    return anchor_dir()


# ------------------------------------------------------------------ the start folder

def start_folder_problem(path: Path) -> str | None:
    """Why this folder can't be the Open Mode start folder, or None. (A start folder inside a
    protected area would make every relative path an error.)"""
    try:
        resolved = path.resolve()
    except OSError as exc:
        return str(exc)
    if not resolved.is_dir():
        return f"'{resolved}' is not a folder that exists"
    reason = protected_reason(resolved)
    return f"'{resolved}' is {reason}" if reason else None


def open_mode_note(start: Path, snapshots_off: bool = False) -> str:
    """What the model is told about Open Mode (one short block, sent with every request)."""
    out = outputs_dir()
    return (f"OPEN MODE is on. You may read and search ANY folder on this computer with absolute "
            f"paths (D:/Projects/app/main.py). Relative paths start from the start folder: {start}. "
            f"Some areas are protected and always refused: Windows and Program Files, AppData, other "
            f"users' profiles, credential folders, Danica's own data. Do not try to reach them another "
            f"way. Writing OUTSIDE the start folder makes the user confirm each time, and every shell "
            f"command except look-only ones (ls, cat, git status...) asks. "
            f"Files you GENERATE (decks, charts, documents, scratch work) go in {out} unless the user "
            f"names another place or you are editing an existing file; writing there never asks. "
            + ("/undo is off for this session. " if snapshots_off else "/undo covers only the start folder. ")
            + "Work in the start folder unless the task needs somewhere else.")
