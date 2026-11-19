"""
audit.py - Append-only, hash-chained audit log.

Each line is one JSON entry. Every entry contains the hash of the entry before
it ("prev") and its own hash ("hash"). Change or delete any line in the middle
and the chain stops matching, which verify() detects.

LIMITS (be honest in the UI): this is tamper-EVIDENT, not tamper-PROOF.
Someone who rewrites the whole file from the start can forge a valid chain,
and deleting the last few lines is not detected.
"""

import hashlib
import json
import os
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


class AuditLog:
    """Append audit events to a hash-linked session log."""

    def __init__(self, path: Path, session: str = ""):
        self.path = Path(path)
        self.session = session
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Continue the existing chain across restarts
        self.prev = self._last_hash()

    def _last_hash(self) -> str:
        if not self.path.exists():
            return GENESIS
        last = GENESIS
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    last = json.loads(line)["hash"]
                except (json.JSONDecodeError, KeyError):
                    pass  # corrupt line: verify() will flag it
        return last

    def append(self, event: str, **data: Any) -> None:
        """Write one entry. Never put raw secrets or full file contents in data."""
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "session": self.session,
            "event": event,
            "data": data,
            "prev": self.prev,
        }
        entry["hash"] = _digest(entry)  # computed BEFORE the hash field exists
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
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
