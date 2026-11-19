"""
titles.py - A short title for each session, written by a hidden background "worker".

WHEN   At the end of the FIRST finished response of a session (and only if the session has no title
       yet). It runs in a background thread, so the answer is never delayed.
HOW    It asks the SAME llama-server (same port) for a 3-6 word title. The request is the real
       conversation so far (same system prompt, same tools) plus ONE extra instruction, with a tiny
       reply limit and no thinking. Reusing the exact same start of the prompt matters: llama-server
       keeps a cache of the prompt it already processed, so the title request costs a few tokens
       instead of re-reading everything, and the next normal request still finds its cache intact.
STORED In <session folder>/title.txt, one line. It never enters the conversation history, so the
       model never sees it, and it costs no context.
USED   /sessions, the welcome panel's recent activity, the status bar, the terminal window title,
       /export and /resume.
If the server fails or returns rubbish, a plain fallback is made from the first request instead.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from danica import llm
from danica.config import APP_DIR, CONFIG

SESSIONS = APP_DIR / "sessions"
MAX_TITLE = 60
TITLE_TOKENS = 28                    # reply limit: a title is a few words
TIMEOUT = httpx.Timeout(connect=5.0, read=45.0, write=10.0, pool=5.0)

INSTRUCTION = ("[harness request, not from the user] Write a title for this conversation: 3 to 6 words, no "
               "quotes, no trailing punctuation, no markdown. Reply with the title only.")

_running: set[str] = set()           # sessions whose title is being made right now
_lock = threading.Lock()


# ------------------------------------------------------------------ storage

def _file(session: str | Path) -> Path:
    """title.txt for a session id or a session folder."""
    folder = Path(session)
    return (folder if folder.is_absolute() else SESSIONS / str(session)) / "title.txt"


def read_title(session: str | Path) -> str:
    """The saved title ("" if none yet). Never raises."""
    try:
        return _file(session).read_text(encoding="utf-8").strip()[:MAX_TITLE]
    except OSError:
        return ""


def write_title(session: Path | str, title: str) -> None:
    """Write a bounded session title using an atomic file replacement."""
    path = _file(session)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("title.txt.tmp")
    tmp.write_text(title.strip()[:MAX_TITLE] + "\n", encoding="utf-8")
    tmp.replace(path)                # atomic: a half-written title is never read


# ------------------------------------------------------------------ cleaning

def clean_title(raw: str) -> str:
    """The model's reply as a tidy one-line title ("" if it is not usable)."""
    text = re.sub(r"<think>.*?</think>", "", raw or "", flags=re.S | re.I)         # a reasoning block, if any
    text = re.sub(r"<[^>]{0,80}>", "", text)                                       # stray tags / tool-call markup
    text = text.strip().splitlines()[0] if text.strip() else ""
    text = re.sub(r"^(title|session title)\s*[:\-]\s*", "", text, flags=re.I)
    text = text.strip(" \t\"'`*#_.:;,-")
    text = " ".join(text.split())
    if not text or len(text.split()) > 12 or "harness" in text.lower():
        return ""
    return text[:MAX_TITLE]


def fallback_title(first_request: str) -> str:
    """A title with no model: the first few words of the user's first request."""
    words = re.sub(r"[^\w\s'-]", " ", first_request or "").split()
    return " ".join(words[:6])[:MAX_TITLE] or "New session"


# ------------------------------------------------------------------ the worker

def request_title(agent: Any) -> str:
    """Ask the model for a title. Blocking (a second or two). Returns "" on any failure."""
    base = CONFIG["server"]["base_url"]
    messages = list(agent._messages_for_model()) + [{"role": "user", "content": INSTRUCTION}]
    payload = {
        "messages": messages,
        "stream": False,
        "temperature": 0.2,
        "max_tokens": TITLE_TOKENS,
        "chat_template_kwargs": {"enable_thinking": False},
        "tools": _tools_of(agent),               # same tools = same prompt start = the server's cache is reused
        "tool_choice": "none",                   # (ignored by servers that don't know it: we only read the text)
    }
    try:
        with llm.local_client() as client:                 # local server: never via a proxy, one shared connection
            reply = client.post(f"{base}/v1/chat/completions", json=payload, timeout=TIMEOUT)
            reply.raise_for_status()
            content = reply.json()["choices"][0]["message"].get("content") or ""
    except (httpx.HTTPError, ValueError, KeyError, IndexError):
        return ""
    return clean_title(content)


def _tools_of(agent: Any) -> list[dict[str, Any]]:
    """The tool list the agent's own requests carry (so the server's cache matches); all tools for a bare agent."""
    pick = getattr(agent, "active_tools", None)
    if pick:
        return pick()
    from danica.tools.registry import tool_schemas
    return tool_schemas()


def first_request_text(agent: Any) -> str:
    """The user's first request in this session (for the fallback title)."""
    for m in agent.messages:
        if m.get("role") == "user" and isinstance(m.get("content"), str) and not m["content"].startswith("["):
            return m["content"]
    return ""


def start(agent: Any, session_id: str, on_done: Callable[[str], None]) -> bool:
    """Make a title in the background if the session has none. on_done(title) is called from the
    worker thread. Returns True if a worker was started."""
    with _lock:
        if session_id in _running or read_title(session_id):
            return False
        _running.add(session_id)

    def work():
        try:
            title = request_title(agent) or fallback_title(first_request_text(agent))
            write_title(session_id, title)
            on_done(title)
        except Exception:                        # a title must never break anything
            pass
        finally:
            with _lock:
                _running.discard(session_id)

    threading.Thread(target=work, name="danica-title", daemon=True).start()
    return True
