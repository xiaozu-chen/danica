"""
audit.py - Append-only, hash-chained audit log.

Each line is one JSON entry. Every entry contains the hash of the entry before
it ("prev") and its own hash ("hash"). Change or delete any line in the middle
and the chain stops matching, which verify() detects.

LIMITS (be honest in the UI): this is tamper-EVIDENT, not tamper-PROOF.
Someone who rewrites the whole file from the start can forge a valid chain,
and deleting the last few lines is not detected.

AUTOMATIC REPAIR (repair_log, run when Danica starts and by /audit repair). Repair never hides anything:
  - TORN TAIL (a crash or power cut left a half-written last line; everything before it checks out):
    the broken bytes are cut off and kept in  audit.jsonl.torn-<time>,  the chain carries on.
  - ANY OTHER BREAK (an entry edited, removed or reordered): the log is NOT patched up. The whole file is
    moved to  audit.jsonl.broken-<time>  (it still fails verify(), on purpose) and a new chain starts.
  - Either way the new log's first entry is "audit_repair": what was wrong, where the old bytes went and
    their SHA-256. The user is told at startup.
Entries are appended under a lock and always link to the REAL last line of the file, so a subagent process
that writes to the same log cannot fork the chain (that was the usual cause of "chain broken").
"""

import contextlib
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

GENESIS = "0" * 64  # "previous hash" of the very first entry


def _digest(entry: dict) -> str:
    """Hash an entry (WITHOUT its own 'hash' field).
    sort_keys + fixed separators make the byte string deterministic."""
    blob = json.dumps(entry, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@contextlib.contextmanager
def _locked(path: Path):
    """Hold a cross-process lock (a small  <log>.lock  file) while one process reads the tail and appends, so two
    writers (Danica and a subagent) never write at the same moment. If locking is not possible it carries on
    without it, as before."""
    lock = None
    try:
        lock = open(Path(path).with_name(Path(path).name + ".lock"), "a+b")
        if sys.platform == "win32":
            import msvcrt
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)      # waits (about 10 s) for the other writer
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX)
    except Exception:
        pass
    try:
        yield
    finally:
        if lock is not None:
            lock.close()                                          # closing the file releases the lock


def _parses(raw: bytes) -> bool:
    """True if this line is a complete JSON entry (an object)."""
    try:
        return isinstance(json.loads(raw.strip().decode("utf-8")), dict)
    except (ValueError, UnicodeDecodeError):
        return False


def _last_entry_hash(path: Path) -> str:
    """The hash of the last complete entry, read from the END of the file. Always the real tail, even when another
    process appended since this one last looked. A half-written last line is skipped."""
    try:
        size = path.stat().st_size
    except OSError:
        return GENESIS
    if size == 0:
        return GENESIS
    with open(path, "rb") as f:
        f.seek(max(0, size - 65536))                              # entries are small: the tail holds the last few
        lines = f.read().split(b"\n")
    for raw in reversed(lines):
        try:
            return json.loads(raw.strip().decode("utf-8"))["hash"]
        except (ValueError, UnicodeDecodeError, KeyError, TypeError):
            continue
    last = GENESIS                                                # a very long line filled the window: read it all
    for raw in path.read_bytes().split(b"\n"):
        try:
            last = json.loads(raw.strip().decode("utf-8"))["hash"]
        except (ValueError, UnicodeDecodeError, KeyError, TypeError):
            pass
    return last


def _scan(data: bytes) -> tuple[int, int, str | None, bool]:
    """Walk the chain like verify(), on raw bytes. Returns (good entries, byte offset just after the last good
    entry, the first problem or None, torn). torn = the problem is a cut-off line and nothing valid follows it."""
    prev, good, good_end, offset = GENESIS, 0, 0, 0
    lines = data.split(b"\n")
    for i, raw in enumerate(lines):
        step = len(raw) + 1                                       # +1 for the newline (the last piece has none)
        text = raw.strip()
        if not text:
            offset += step
            continue
        problem, bad_json, claimed = None, False, None
        try:
            entry = json.loads(text.decode("utf-8"))
            if not isinstance(entry, dict):
                raise ValueError("not an object")
        except (ValueError, UnicodeDecodeError):
            problem, bad_json = f"line {i + 1}: not valid JSON", True
        else:
            claimed = entry.pop("hash", None)
            if entry.get("prev") != prev:
                problem = f"line {i + 1}: chain broken (an entry before it was removed, reordered or edited)"
            elif _digest(entry) != claimed:
                problem = f"line {i + 1}: contents do not match their hash (edited)"
        if problem is None:
            prev, good = claimed, good + 1
            offset += step
            good_end = min(offset, len(data))
            continue
        torn = bad_json and not any(_parses(x) for x in lines[i + 1:])
        return good, good_end, problem, torn
    return good, good_end, None, False


@dataclass
class Repair:
    """What repair_log did, for the audit entry and for the message the user sees."""
    kind: str          # "torn_tail" (cut-off last line removed) | "restart" (broken log set aside, new chain)
    good: int          # entries that still verified
    problem: str       # what verify() would have said
    moved: Path        # where the removed bytes / the old log now are
    size: int          # how many bytes were moved
    sha256: str        # their SHA-256, so the file can be matched to the audit entry later

    def short(self) -> str:
        """A few words, for a panel's status line."""
        return ("Audit log repaired: removed a cut-off last line" if self.kind == "torn_tail"
                else "Audit log repaired: broken log set aside, new log started")

    def message(self) -> str:
        """The full explanation for the user."""
        if self.kind == "torn_tail":
            return (f"Audit log: its last line was cut off (a crash or power cut?). It was removed and kept in "
                    f"{self.moved.name}; the chain is intact ({self.good} entries).")
        return (f"Audit log: the chain was broken ({self.problem}). The old log was kept as {self.moved.name} "
                f"(it still fails the check, on purpose) and a new log was started. If you did not expect this, "
                f"someone may have changed the file.")


def _free_name(path: Path, tag: str) -> Path:
    """audit.jsonl.<tag>-<time>, with a number added if that name is taken."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out, n = path.with_name(f"{path.name}.{tag}-{stamp}"), 1
    while out.exists():
        n += 1
        out = path.with_name(f"{path.name}.{tag}-{stamp}-{n}")
    return out


def repair_log(path: Path) -> Repair | None:
    """Check the log and fix what can be fixed without hiding anything (see the module docstring). None = it was fine.
    The caller (AuditLog.repair) writes the "audit_repair" entry."""
    path = Path(path)
    with _locked(path):
        if not path.exists() or path.stat().st_size == 0:
            return None
        data = path.read_bytes()
        good, good_end, problem, torn = _scan(data)
        if problem is None:
            return None
        if torn:
            tail = data[good_end:]
            side = _free_name(path, "torn")
            side.write_bytes(tail)                                # keep the evidence FIRST, then cut
            with open(path, "r+b") as f:
                f.truncate(good_end)
                f.flush()
                os.fsync(f.fileno())
            return Repair("torn_tail", good, problem, side, len(tail), hashlib.sha256(tail).hexdigest())
        old = _free_name(path, "broken")
        os.replace(path, old)                                     # nothing is deleted or edited: the file just moves
        return Repair("restart", good, problem, old, len(data), hashlib.sha256(data).hexdigest())


class AuditLog:
    """Append audit events to a hash-linked session log."""

    def __init__(self, path: Path, session: str = "", auto_repair: bool = True):
        """auto_repair=True checks the log now and repairs it if needed (Danica at startup). A subagent passes False:
        it shares the parent's file, and only the parent may move or cut it."""
        self.path = Path(path)
        self.session = session
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.repaired: Repair | None = None          # set when this start (or /audit repair) had to repair the log
        self.prev = _last_entry_hash(self.path)      # continue the existing chain across restarts
        if auto_repair:
            self.repair()

    def repair(self) -> Repair | None:
        """Check the log and repair it if needed; records an "audit_repair" entry. Returns what was done (None = fine)."""
        rep = repair_log(self.path)
        self.prev = _last_entry_hash(self.path)
        if rep is not None:
            self.repaired = rep
            self.append("audit_repair", kind=rep.kind, good_entries=rep.good, problem=rep.problem,
                        moved_to=rep.moved.name, moved_bytes=rep.size, moved_sha256=rep.sha256)
        return rep

    def append(self, event: str, **data: Any) -> None:
        """Write one entry. Never put raw secrets or full file contents in data."""
        with _locked(self.path):                     # one writer at a time, across processes
            self.prev = _last_entry_hash(self.path)  # link to the file's REAL last line, not to what we wrote last
            entry = {
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "session": self.session,
                "event": event,
                "data": data,
                "prev": self.prev,
            }
            entry["hash"] = _digest(entry)  # computed BEFORE the hash field exists
            line = (json.dumps(entry, ensure_ascii=False, default=str) + "\n").encode("utf-8")
            with open(self.path, "a+b") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                if size:                             # a cut-off last line must not swallow this entry
                    f.seek(size - 1)
                    if f.read(1) != b"\n":
                        line = b"\n" + line
                f.write(line)
                f.flush()
                os.fsync(f.fileno())  # force it to disk so a crash can't lose the entry
            self.prev = entry["hash"]


def verify(path: Path) -> tuple[bool, int, str]:
    """Walk the whole chain. Returns (ok, entries_checked, problem_description)."""
    path = Path(path)
    if not path.exists():
        return True, 0, ""

    prev = GENESIS
    count = 0
    with open(path, "r", encoding="utf-8") as f:
        for n, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                return False, count, f"line {n}: not valid JSON"

            claimed = entry.pop("hash", None)  # remove it so we can re-hash the rest
            if entry.get("prev") != prev:
                return False, count, (f"line {n}: chain broken "
                                      f"(an entry before it was removed, reordered or edited)")
            if _digest(entry) != claimed:
                return False, count, f"line {n}: contents do not match their hash (edited)"
            prev = claimed
            count += 1
    return True, count, ""
