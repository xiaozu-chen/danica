"""
sessions.py - Listing old sessions and rebuilding a conversation from its
transcript.jsonl.

Transcript lines are of two kinds:
    a message          {"role": ..., ...}
    an event record    {"event": "compaction", "tail_len": N, "summary": "...", "narrative": "..."}
Replaying them in order gives back the same message list the live session had,
EXCEPT pruning (which only ever happened in memory).
"""

import json
import os
import shutil
import stat
import re
from datetime import datetime
from pathlib import Path

from danica.titles import read_title
from danica.compaction import harvest
from danica.config import APP_DIR

SESSIONS_DIR = APP_DIR / "sessions"
MAX_LIST = 15
ID_RX = re.compile(r"\d{8}-\d{6}")   # session ids look like 20261102-013000
INTERRUPTED = "ERROR: this call never finished (the session ended before it returned)."


def load_records(path: Path) -> tuple[list[dict], int]:
    """Read a transcript. Returns (records, number_of_unreadable_lines).
    A crash can leave a half-written last line; that must not block resuming."""
    records, bad = [], 0
    path = Path(path)
    if not path.exists():
        return records, bad
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                bad += 1
                continue
            if isinstance(rec, dict):
                records.append(rec)
            else:
                bad += 1
    return records, bad


def repair(messages: list[dict]) -> tuple[list[dict], int]:
    """Make the history valid for the server: every assistant tool call needs
    exactly one matching tool result right after it, and no result may be
    orphaned. Returns (fixed_messages, number_of_problems_fixed)."""
    out, fixed, i, n = [], 0, 0, len(messages)
    while i < n:
        m = messages[i]
        i += 1
        if m.get("role") == "tool":
            fixed += 1            # an orphan result (no call right before it): drop it
            continue
        out.append(m)
        calls = m.get("tool_calls") if m.get("role") == "assistant" else None
        if not calls:
            continue

        # Collect the results that directly follow this assistant message
        results: dict[str, dict] = {}
        while i < n and messages[i].get("role") == "tool":
            results[messages[i].get("tool_call_id")] = messages[i]
            i += 1
        matched = 0
        for call in calls:
            if call["id"] in results:
                out.append(results[call["id"]])
                matched += 1
            else:
                out.append({"role": "tool", "tool_call_id": call["id"], "content": INTERRUPTED})
                fixed += 1
        fixed += len(results) - matched      # results that matched no call: dropped
    return out, fixed


def rebuild(
    records: list[dict], system_message: dict
) -> tuple[list[dict], str, int, int]:
    """Replay records. Returns (messages, narrative, compactions, repairs_made)."""
    messages = [system_message]
    narrative, compactions = "", 0

    for rec in records:
        if "role" in rec:
            messages.append(rec)
        elif rec.get("event") == "compaction" and isinstance(rec.get("summary"), str):
            # Same operation Agent.compact() did live: keep the tail, swap the rest
            body = messages[1:]
            tail_len = int(rec.get("tail_len", 0))
            tail = body[-tail_len:] if tail_len > 0 else []
            messages = [system_message, {"role": "user", "content": rec["summary"]}] + tail
            # Older records have no "narrative"; the whole summary is a fine stand-in
            narrative = rec.get("narrative") or rec["summary"]
            compactions += 1
        # any other event record is ignored

    messages, fixed = repair(messages)
    return messages, narrative, compactions, fixed


def list_sessions(
    sessions_dir: Path = SESSIONS_DIR, limit: int = MAX_LIST
) -> list[dict]:
    """Newest first. Sessions without any user request are skipped."""
    sessions_dir = Path(sessions_dir)
    if not sessions_dir.exists():
        return []
    folders = sorted(
        (
            directory
            for directory in sessions_dir.iterdir()
            if directory.is_dir() and ID_RX.fullmatch(directory.name)
        ),
        key=lambda directory: directory.name,
        reverse=True,
    )
    found = []
    for folder in folders[:200]:           # never scan thousands of folders
        path = folder / "transcript.jsonl"
        if not path.exists():
            continue
        records, _ = load_records(path)
        requests = harvest([r for r in records if "role" in r])[0]
        if not requests:
            continue
        found.append({
            "id": folder.name,
            "modified": datetime.fromtimestamp(path.stat().st_mtime),
            "requests": len(requests),
            "compactions": sum(1 for r in records if r.get("event") == "compaction"),
            "preview": requests[0][:70],
            "title": read_title(folder),          # the generated title ("" until the first answer is done)
        })
        if len(found) >= limit:
            break
    return found


def resolve_session(sessions_dir: Path, arg: str, current: str = "") -> str:
    """Turn what the user typed after /resume into a session id.
    Accepts: nothing (newest other session), a list number, a full id or a unique prefix.
    Raises ValueError with a message that is safe to show."""
    arg = arg.strip()
    if not arg:
        for info in list_sessions(sessions_dir):
            if info["id"] != current:
                return info["id"]
        raise ValueError("there are no earlier sessions to resume (see /sessions)")

    # Strict format check: the text becomes part of a folder path
    if not re.fullmatch(r"[0-9-]{1,15}", arg):
        raise ValueError(f"'{arg}' is not a session. Use a number or id from /sessions")

    infos = list_sessions(sessions_dir)
    if len(arg) <= 3 and arg.isdigit():        # "2" means the 2nd row of /sessions
        n = int(arg)
        if not 1 <= n <= len(infos):
            raise ValueError(f"there is no session number {n} (see /sessions)")
        target = infos[n - 1]["id"]
    else:
        matches = [i["id"] for i in infos if i["id"].startswith(arg)]
        if not matches:
            raise ValueError(f"no session matches '{arg}' (see /sessions)")
        if len(matches) > 1:
            raise ValueError(f"'{arg}' matches {len(matches)} sessions; type more of the id")
        target = matches[0]

    if target == current:
        raise ValueError("that is the current session")
    return target

# ------------------------------------------------------------------ deleting sessions

def all_session_ids(sessions_dir: Path = SESSIONS_DIR) -> list[str]:
    """Every saved session folder (newest first), including empty ones that /sessions hides."""
    sessions_dir = Path(sessions_dir)
    if not sessions_dir.exists():
        return []
    return sorted(
        (
            directory.name
            for directory in sessions_dir.iterdir()
            if directory.is_dir() and ID_RX.fullmatch(directory.name)
        ),
        reverse=True,
    )


def folder_size(path: Path) -> int:
    """Total bytes under a folder. Links are counted as links, never followed."""
    total = 0
    for dirpath, _, filenames in os.walk(path, followlinks=False):
        for name in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                pass
    return total


def describe_session(sessions_dir: Path, sid: str) -> dict:
    """What the delete confirmation shows about one session."""
    folder = Path(sessions_dir) / sid
    requests, when = [], None
    path = folder / "transcript.jsonl"
    if path.exists():
        records, _ = load_records(path)
        requests = harvest([r for r in records if "role" in r])[0]
        when = datetime.fromtimestamp(path.stat().st_mtime)
    from danica.projects import load_session_meta
    meta = load_session_meta(folder)
    mine = folder / "workspace"                                   # only a CHAT session keeps its files inside its folder
    return {"id": sid, "modified": when, "requests": len(requests), "title": read_title(folder) or (requests[0][:60] if requests else ""),
            "project": meta.get("project") or "", "size": folder_size(folder), "has_files": mine.is_dir() and any(mine.iterdir())}


def select_sessions(sessions_dir: Path, words: list[str], current: str = "") -> tuple[list[str], list[str]]:
    """Turn the words after '/sessions delete' into (session ids, problems). Never includes the current session.
    Words: row numbers (3), ranges (2-5), ids or unique prefixes, `empty` (no requests), `all` (everything else)."""
    sessions_dir = Path(sessions_dir)
    chosen, problems = [], []

    def add(sid: str) -> None:
        if sid == current:
            problems.append(f"{sid} is the current session: type /new first, then delete it")
        elif sid not in chosen:
            chosen.append(sid)

    rows = [i["id"] for i in list_sessions(sessions_dir)]          # row numbers mean the rows /sessions shows
    for word in words:
        word = word.strip().lower()
        if word == "all":
            for sid in all_session_ids(sessions_dir):
                if sid != current:
                    add(sid)
        elif word == "empty":
            shown = {i["id"] for i in list_sessions(sessions_dir, limit=10 ** 6)}
            for sid in all_session_ids(sessions_dir):
                if sid not in shown and sid != current:
                    add(sid)
        elif re.fullmatch(r"\d{1,3}-\d{1,3}", word):
            a, b = (int(x) for x in word.split("-"))
            if not 1 <= a <= b <= len(rows):
                problems.append(f"'{word}' is not a range of /sessions rows (there are {len(rows)})")
            else:
                for sid in rows[a - 1:b]:
                    add(sid)
        elif re.fullmatch(r"\d{1,3}", word):
            n = int(word)
            if not 1 <= n <= len(rows):
                problems.append(f"there is no session number {n} (see /sessions)")
            else:
                add(rows[n - 1])
        elif re.fullmatch(r"[0-9-]{4,15}", word):
            hits = [s for s in all_session_ids(sessions_dir) if s.startswith(word)]
            if len(hits) == 1:
                add(hits[0])
            else:
                problems.append(f"'{word}' matches {len(hits)} sessions" + ("; type more of the id" if hits else ""))
        else:
            problems.append(f"'{word}' is not a session number, range, id, `empty` or `all`")
    return chosen, problems


def _unlock(func, path, _exc) -> None:
    """rmtree helper: Windows refuses to delete read-only files, so clear the flag and try again."""
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass


def delete_session(sessions_dir: Path, sid: str) -> bool:
    """Delete ONE session folder. Only folders named like a session id, directly inside sessions_dir, and never a
    link/junction (rmtree must not be pointed somewhere else). Returns True when the folder is gone."""
    sessions_dir = Path(sessions_dir)
    if not ID_RX.fullmatch(sid):
        raise ValueError(f"'{sid}' is not a session id")
    folder = sessions_dir / sid
    if not folder.is_dir():
        return False
    if folder.is_symlink() or (
        Path(os.path.realpath(folder))
        != Path(os.path.realpath(sessions_dir)) / sid
    ):
        raise ValueError(f"{sid} is a link, not a real session folder: left alone")
    shutil.rmtree(folder, onerror=_unlock)
    return not folder.exists()
