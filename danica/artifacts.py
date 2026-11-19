"""
artifacts.py - "Which files did this request make?", plus /open and /save.

SAFETY: /open must never run something the model made. Only plain document and picture
types are opened with the default app; .pdf and .docx only if Danica itself made them
(docgen.is_danica_file). Everything else opens its FOLDER in Explorer instead, so a
model-written .bat or .py can never run because you pressed a number.
"""

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from danica.checkpoints import Checkpoints
from danica.docgen import is_danica_file
from danica.secret_scan import is_sensitive_path
from danica.workspace import PathJailError, Workspace

MAX_LISTED = 12
OPEN_TYPES = {".md", ".txt", ".png", ".jpg", ".jpeg"}
DOC_TYPES = {".pdf", ".docx", ".pptx", ".html"}   # opened only when Danica made them (for .html: its signature must verify)
SKIP_SUFFIXES = {".pyc", ".pyo", ".tmp"}
# Never shown or copied as text, whatever their bytes look like (binary formats and pictures)
NON_TEXT_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".pdf", ".docx", ".pptx", ".xlsx",
                     ".zip", ".gz", ".7z", ".exe", ".dll", ".pyd", ".so", ".db", ".sqlite", ".mp3", ".mp4"}
VIEW_MAX_BYTES = 400_000        # the split-screen viewer reads at most this much of a file
VIEW_MAX_LINES = 1500           # ...and shows at most this many lines
VIEW_MAX_COLS = 300             # a longer line is cut (a base64 image inside an .html would flood the pane)
COPY_MAX_BYTES = 1_000_000      # /copy refuses files bigger than this


class ArtifactError(Exception):
    """A problem whose message is safe to show the user."""


@dataclass
class Artifact:
    """Describe a workspace file created or changed during a request."""

    rel: str          # workspace-relative path, forward slashes
    path: Path        # the real path
    status: str       # new, changed or written
    size: int


@dataclass
class ArtifactList:
    """Collect changed files and counts for the artifact summary."""

    items: list[Artifact] = field(default_factory=list)
    deleted: int = 0
    more: int = 0     # files that did not fit in the list


def collect(
    ws: Workspace,
    checkpoints: Checkpoints | None,
    snap: str | None,
    touched: list[str],
    shown: set[str],
) -> ArtifactList:
    """The files this request made or changed.
    Sources: the shadow git diff against the checkpoint taken before the request (catches files
    made by shell scripts) AND the paths the file tools wrote (catches files the project's
    .gitignore hides). `shown` = pictures already displayed inline, which are not listed again."""
    rows: list[tuple[str, str]] = []
    if checkpoints is not None and getattr(checkpoints, "available", False) and snap:
        try:
            rows = checkpoints.changed_since(snap)
        except Exception:
            rows = []
    known = {name for _, name in rows}
    rows += [("W", rel) for rel in touched if rel not in known]

    names = {name.lower() for status, name in rows if status != "D"}
    shown_l = {s.lower() for s in shown}
    result = ArtifactList()
    listed: list[Artifact] = []
    for status, name in rows:
        low = name.lower()
        if status == "D":
            result.deleted += 1
            continue
        if low in shown_l or Path(low).suffix in SKIP_SUFFIXES:
            continue
        if low.endswith(".json") and (low[:-5] + ".png" in names or low[:-5] + ".png" in shown_l):
            continue                                        # chart data: the picture is the artifact
        try:
            path = ws.resolve(name)
            if not path.is_file() or is_sensitive_path(ws.rel(path)):
                continue
            size = path.stat().st_size
        except (PathJailError, OSError, ValueError):
            continue
        listed.append(Artifact(ws.display(path), path, {"A": "new", "M": "changed"}.get(status, "written"), size))

    result.items = listed[:MAX_LISTED]
    result.more = max(0, len(listed) - MAX_LISTED)
    return result


def is_openable(path: Path) -> bool:
    """May /open (or a clickable link) hand this file to the default app?"""
    ext = Path(path).suffix.lower()
    return ext in OPEN_TYPES or (ext in DOC_TYPES and is_danica_file(path))


def _startfile(path: str) -> None:
    os.startfile(path)                      # Windows only: opens with the default app. Tests replace this.


def _explorer(path: str) -> None:
    subprocess.Popen(f'explorer /select,"{path}"')     # opens the folder with the file selected. Tests replace this.


def open_artifact(a: Artifact) -> str:
    """Open an artifact safely or show its containing folder."""
    if not a.path.is_file():
        raise ArtifactError("that file no longer exists")
    try:
        if is_openable(a.path):
            _startfile(str(a.path))
            return f"opened {a.rel}"
        _explorer(str(a.path))
        return f"{a.rel} is not a type Danica opens automatically, so its folder was opened instead"
    except OSError as e:
        raise ArtifactError(f"could not open it: {e}")


def downloads_dir() -> Path:
    """The user's real Downloads folder. On Windows it is asked from the system (it may be moved to another drive or
    OneDrive, so ~/Downloads is often wrong); then ~/Downloads, Desktop, and finally the home folder."""
    if os.name == "nt":
        try:
            import ctypes
            import uuid
            # FOLDERID_Downloads, as the 16 bytes Windows expects
            raw = uuid.UUID("{374DE290-123F-4565-9164-39C4925E467B}").bytes_le
            folder_id = (ctypes.c_ubyte * 16)(*raw)
            out = ctypes.c_wchar_p()
            if (
                ctypes.windll.shell32.SHGetKnownFolderPath(
                    ctypes.byref(folder_id), 0, None, ctypes.byref(out)
                )
                == 0
                and out.value
            ):
                found = Path(out.value)
                ctypes.windll.ole32.CoTaskMemFree(out)
                if found.is_dir():
                    return found
        except Exception:
            pass                                       # fall back to the usual places
    home = Path.home()
    for candidate in (home / "Downloads", home / "Desktop"):
        if candidate.is_dir():
            return candidate
    return home


def save_copy(src: Path, dest_dir: Path) -> Path:
    """Copy a file into a folder without ever overwriting: a.txt, a (1).txt, a (2).txt..."""
    dest_dir = Path(dest_dir).expanduser()
    if not dest_dir.is_dir():
        raise ArtifactError(f"'{dest_dir}' is not a folder that exists")
    dest_dir = dest_dir.resolve()
    target = dest_dir / src.name
    n = 0
    while target.exists():
        n += 1
        if n > 999:
            raise ArtifactError("too many copies with that name")
        target = dest_dir / f"{src.stem} ({n}){src.suffix}"
    try:
        shutil.copy2(src, target)
    except OSError as e:
        raise ArtifactError(f"could not copy: {e}")
    return target


def pick(arg: str, items: list[Artifact]) -> Artifact:
    """The artifact a command refers to ("2", or nothing when there is only one)."""
    if not items:
        raise ArtifactError("no files from the last request (they are listed after a request that wrote files)")
    arg = arg.strip()
    if not arg:
        if len(items) == 1:
            return items[0]
        raise ArtifactError("which one? give its number from the list")
    if not arg.isdigit() or not 1 <= int(arg) <= len(items):
        raise ArtifactError(f"pick a number from 1 to {len(items)}")
    return items[int(arg) - 1]


# ------------------------------------------------------------------ text view and clipboard

def read_text(path: Path, limit: int = VIEW_MAX_BYTES) -> tuple[str, bool]:
    """(text, was_cut) for a text artifact, or ArtifactError if it is not text. Used by the split-
    screen viewer and /copy. Only reads the start of a big file. Pictures and office files are
    refused by their suffix; anything else is refused when its first bytes contain a NUL."""
    path = Path(path)
    if path.suffix.lower() in NON_TEXT_SUFFIXES:
        raise ArtifactError(f"{path.name} is not a text file")
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            raw = f.read(limit + 1)
    except OSError as e:
        raise ArtifactError(f"could not read it: {e}")
    if b"\0" in raw[:8192]:
        raise ArtifactError(f"{path.name} is not a text file")
    cut = len(raw) > limit or size > limit
    return raw[:limit].decode("utf-8-sig", errors="replace"), cut


def tidy_for_view(text: str) -> tuple[str, int]:
    """Text ready for the viewer: tabs expanded, over-long lines cut, at most VIEW_MAX_LINES lines.
    Returns (text, number of lines left out)."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    left_out = max(0, len(lines) - VIEW_MAX_LINES)
    out = []
    for line in lines[:VIEW_MAX_LINES]:
        line = line.expandtabs(4)
        out.append(line if len(line) <= VIEW_MAX_COLS else line[:VIEW_MAX_COLS] + " …")
    return "\n".join(out), left_out


def _clipboard(text: str) -> None:
    """Put text on the Windows clipboard. Tests replace this. Goes through PowerShell's
    Set-Clipboard with the text on STDIN (never in the command line: no quoting, no length limit)."""
    script = "[Console]::InputEncoding=[Text.Encoding]::UTF8; Set-Clipboard -Value ([Console]::In.ReadToEnd())"
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                   input=text.encode("utf-8"), check=True, timeout=20, creationflags=flags,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def copy_contents(a: Artifact) -> int:
    """Copy a text artifact's contents to the clipboard. Returns the number of characters."""
    if not a.path.is_file():
        raise ArtifactError("that file no longer exists")
    text, cut = read_text(a.path, COPY_MAX_BYTES)
    if cut:
        raise ArtifactError(f"{a.rel} is bigger than {COPY_MAX_BYTES // 1000} KB: use /save instead")
    try:
        _clipboard(text)
    except (OSError, subprocess.SubprocessError) as e:
        raise ArtifactError(f"could not reach the clipboard ({type(e).__name__}). Use /save instead")
    return len(text)
