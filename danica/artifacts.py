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

from danica.docgen import is_danica_file
from danica.secret_scan import is_sensitive_path
from danica.workspace import PathJailError, Workspace

MAX_LISTED = 12
OPEN_TYPES = {".md", ".txt", ".png", ".jpg", ".jpeg"}
DOC_TYPES = {".pdf", ".docx", ".pptx", ".html"}   # opened only when Danica made them (for .html: its signature must verify)
SKIP_SUFFIXES = {".pyc", ".pyo", ".tmp"}


class ArtifactError(Exception):
    """A problem whose message is safe to show the user."""


@dataclass
class Artifact:
    rel: str          # workspace-relative path, forward slashes
    path: Path        # the real path
    status: str       # new, changed or written
    size: int


@dataclass
class ArtifactList:
    items: list[Artifact] = field(default_factory=list)
    deleted: int = 0
    more: int = 0     # files that did not fit in the list


def collect(ws: Workspace, checkpoints, snap: str | None, touched: list[str], shown: set[str]) -> ArtifactList:
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
            if not path.is_file() or is_sensitive_path(path.relative_to(ws.root)):
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