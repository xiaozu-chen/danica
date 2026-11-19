"""
export.py - /export: write a readable bundle of one session.

Bundle:  README.md, conversation.md, summary.md (if compacted), sources.md,
         todo.md (if there is one), workspace/ (chat sessions only).

Everything is scrubbed: secrets redacted, invisible characters removed, tool
results shortened, web page text omitted. HONEST LIMIT: redaction only knows
well-known secret formats, so read conversation.md before sharing a bundle.
"""

import json
import os
import re
import shutil
import unicodedata
from datetime import datetime
from pathlib import Path

from danica.titles import read_title
from danica.workspace import Workspace
from danica.compaction import harvest, is_summary
from danica.config import APP_DIR
from danica.projects import SKIP_DIRS
from danica.secret_scan import find_secrets, is_sensitive_path, redact
from danica.sessions import load_records
from danica.web import WebState

EXPORTS_DIR = APP_DIR / "exports"
MAX_RESULT_CHARS = 300      # tool results are shortened to this
MAX_ARG_CHARS = 120         # long tool-call arguments (file contents) are shortened to this
MAX_COPY_FILES = 500
MAX_COPY_BYTES = 20_000_000
MAX_SCAN_BYTES = 1_000_000  # bigger files are copied without a secret scan
MAX_LISTED = 30             # how many skipped files the README names


def scrub(text: str) -> str:
    """Remove invisible characters (keeping newlines and tabs) and redact secrets.
    Unlike the web cleaner this keeps indentation, which code needs."""
    text = "".join(ch for ch in text
                   if ch in "\n\t" or unicodedata.category(ch) not in ("Cc", "Cf", "Co", "Cn", "Cs"))
    return redact(text)[0]


def fence(text: str) -> str:
    """A markdown code block whose fence is longer than any backtick run inside,
    so the content can never break out of the block."""
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    ticks = "`" * max(3, longest + 1)
    return f"{ticks}\n{text}\n{ticks}"


def short_args(raw: str) -> str:
    """Tool-call arguments on one line, with big strings (file contents) cut short."""
    try:
        args = json.loads(raw or "{}")
    except ValueError:
        return raw[:MAX_ARG_CHARS]
    if not isinstance(args, dict):
        return raw[:MAX_ARG_CHARS]
    out = {}
    for key, value in args.items():
        if isinstance(value, str) and len(value) > MAX_ARG_CHARS:
            value = value[:MAX_ARG_CHARS] + f"... ({len(value)} chars)"
        out[key] = value
    return json.dumps(out, ensure_ascii=False)


def render_conversation(records: list[dict]) -> str:
    """The transcript as markdown."""
    out = ["# Conversation", "",
           "Tool results are shortened and web page text is left out.", ""]
    for rec in records:
        if rec.get("event") == "compaction":
            out += ["---", "*The conversation was compacted here: earlier messages were "
                           "summarized for the model.*", "---", ""]
            continue
        role, content = rec.get("role"), rec.get("content") or ""
        if role == "user":
            if content.startswith("[harness reminder"):
                continue                      # harness noise, not part of the story
            if is_summary(rec):
                out += ["*(context summary inserted by the harness)*", ""]
                continue
            out += ["## You", "", scrub(content), ""]
        elif role == "assistant":
            if content.strip():
                out += ["## Danica", "", scrub(content), ""]
            for call in rec.get("tool_calls") or []:
                fn = call.get("function", {})
                out += ["Tool call:", "", fence(scrub(f"{fn.get('name', '?')} {short_args(fn.get('arguments', ''))}")), ""]
        elif role == "tool":
            if "<untrusted_web_content" in content:
                shown = "[web page text omitted from the export]"
            else:
                shown = content[:MAX_RESULT_CHARS]
                if len(content) > MAX_RESULT_CHARS:
                    shown += f"\n... ({len(content)} chars)"
            out += ["Result:", "", fence(scrub(shown)), ""]
    return "\n".join(out)


def copy_workspace(root: Path, dest: Path) -> tuple[list[str], list[str]]:
    """Copy a (throwaway) workspace into the bundle. Returns (copied, skipped_with_reasons)."""
    copied, skipped, total = [], [], 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            src = Path(dirpath) / name
            rel = src.relative_to(root)
            label = rel.as_posix()
            if src.is_symlink():
                skipped.append(f"{label} (symlink)")
                continue
            if is_sensitive_path(rel):
                skipped.append(f"{label} (looks like a credentials file)")
                continue
            try:
                size = src.stat().st_size
            except OSError:
                continue
            if len(copied) >= MAX_COPY_FILES or total + size > MAX_COPY_BYTES:
                skipped.append(f"{label} (export size limit)")
                continue
            if size <= MAX_SCAN_BYTES:
                try:
                    text = src.read_bytes().decode("utf-8", errors="ignore")
                except OSError:
                    continue
                if find_secrets(text):
                    skipped.append(f"{label} (contains what looks like a secret)")
                    continue
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
            copied.append(label)
            total += size
    return copied, skipped


def build_readme(
    session_id: str,
    project: dict | None,
    records: list[dict],
    state: WebState,
    copied: list[str],
    skipped: list[str],
    has_summary: bool,
    has_todo: bool,
) -> str:
    """Build a Markdown transcript from session records and metadata."""
    requests, changed, _ = harvest([r for r in records if "role" in r])
    changed = list(dict.fromkeys(changed))
    compactions = sum(1 for r in records if r.get("event") == "compaction")
    mode = f"project {project['name']} ({project['path']})" if project else "normal chat"

    lines = [f"# {read_title(session_id) or 'Danica session'}", "",
             f"- Session: {session_id}",
             f"- Exported: {datetime.now():%Y-%m-%d %H:%M}",
             f"- Mode: {mode}",
             f"- Your requests: {len(requests)}; compactions: {compactions}", "",
             "## In this bundle", "",
             "- [conversation.md](conversation.md): what was said and which tools ran"]
    if has_summary:
        lines.append("- [summary.md](summary.md): the latest context summary")
    if state.by_id:
        lines.append("- [sources.md](sources.md): web pages fetched in this session")
    if has_todo:
        lines.append("- [todo.md](todo.md): the plan Danica was working from")
    if project:
        lines.append("- No workspace copy: the files live in your project folder")
    else:
        lines.append(f"- workspace/: {len(copied)} file(s) Danica made")

    if changed:
        lines += ["", "## Files written or edited by file tools", ""]
        lines += [f"- {c}" for c in changed[:60]]
    if skipped:
        lines += ["", "## Skipped from the workspace copy", ""]
        lines += [f"- {s}" for s in skipped[:MAX_LISTED]]
        if len(skipped) > MAX_LISTED:
            lines.append(f"- ... and {len(skipped) - MAX_LISTED} more")
    lines += ["", "## Before you share this", "",
              "Redaction only catches well-known secret formats. Read conversation.md first.", "",
              f"To continue the session in Danica: `/resume {session_id}`", ""]
    return "\n".join(lines)


def export_session(
    session_dir: Path,
    ws: Workspace,
    project: dict | None,
    out_root: Path = EXPORTS_DIR,
) -> Path:
    """Write the bundle and return its folder. Raises ValueError if there is nothing to export."""
    session_dir = Path(session_dir)
    records, _ = load_records(session_dir / "transcript.jsonl")
    if not any("role" in r for r in records):
        raise ValueError("this session has no conversation yet")

    out = Path(out_root) / f"{session_dir.name}-{datetime.now():%H%M%S}"
    out.mkdir(parents=True, exist_ok=False)
    (out / "conversation.md").write_text(render_conversation(records), encoding="utf-8")

    summaries = sorted((session_dir / "summaries").glob("*.md")) if (session_dir / "summaries").is_dir() else []
    if summaries:
        (out / "summary.md").write_text(scrub(summaries[-1].read_text(encoding="utf-8")), encoding="utf-8")

    todo = session_dir / "TODO.md"
    if todo.is_file():
        (out / "todo.md").write_text(scrub(todo.read_text(encoding="utf-8")), encoding="utf-8")

    state = WebState(session_dir / "sources.json")       # loads the saved sources
    if state.by_id:
        rows = ["# Web sources", ""]
        for s in state.by_id.values():
            title = " ".join(scrub(s.title).split())[:100] or "(no title)"
            rows.append(f"- {s.id}: {title} - {s.final_url or s.url}")
        (out / "sources.md").write_text("\n".join(rows) + "\n", encoding="utf-8")

    copied, skipped = ([], []) if project else copy_workspace(ws.root, out / "workspace")
    (out / "README.md").write_text(
        build_readme(session_dir.name, project, records, state, copied, skipped,
                     bool(summaries), todo.is_file()), encoding="utf-8")
    return out