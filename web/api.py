"""FastAPI bridge between the Danica desktop shell and the web UI."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from fastapi.staticfiles import StaticFiles

from danica.config import CONFIG

app = FastAPI(title="Danica", version="1.0.0")
# Vite dev server origin only. Production requests use same-origin via the sidecar.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:1420", "http://127.0.0.1:1420"],
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=100_000)
    session_id: str | None = None


class _WebUI:
    """Noninteractive UI adapter. Danica's permission policy denies prompts without an approver."""

    def log(self, level: str, text: str) -> None: pass
    def say(self, text: str, style: str | None = None) -> None: pass
    def confirm(self, title: str, body: str = "", detail: str = "", yes: str = "Yes", no: str = "No") -> bool: return False
    def permission(self, tool: str, summary: str, tier: str, reason: str, can_remember: bool) -> str: return "deny"
    def choose(self, title: str, body: str, options: list[tuple[str, str]], detail: str = "") -> str | None: return None
    def ask_text(self, title: str, default: str = "") -> str | None: return None


class _WebSession:
    def __init__(self, parts: tuple[Any, ...]):
        self.workspace, self.audit, self.checkpoints, self.policy, self.agent = parts
        self.lock = threading.Lock()


_sessions: dict[str, _WebSession] = {}
_sessions_lock = threading.Lock()
_web_ui = _WebUI()


def _new_session_id() -> str:
    from danica.config import APP_DIR

    candidate = datetime.now()
    sessions_dir = APP_DIR / "sessions"
    while (sessions_dir / candidate.strftime("%Y%m%d-%H%M%S")).exists():
        candidate += timedelta(seconds=1)
    return candidate.strftime("%Y%m%d-%H%M%S")


def _create_session() -> tuple[str, _WebSession]:
    from danica.app_core import build_session

    with _sessions_lock:
        session_id = _new_session_id()
        parts = build_session(session_id, _web_ui, lambda kind, data: None, headless=True)
        session = _WebSession(parts)
        _sessions[session_id] = session
    return session_id, session


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/app")
def app_info() -> dict[str, object]:
    """Return display-safe runtime information; secrets/config values stay in Python."""
    return {
        "name": "Danica",
        "version": "1.0.0",
        "model_url": CONFIG.get("server", {}).get("base_url", ""),
    }


@app.get("/api/sessions")
def recent_sessions() -> list[dict[str, object]]:
    from danica.sessions import list_sessions

    return [
        {**session, "modified": session["modified"].isoformat()}
        for session in list_sessions()
    ]


@app.post("/api/sessions")
def create_session() -> dict[str, str]:
    try:
        session_id, _ = _create_session()
        return {"session_id": session_id}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Could not start a Danica session: {exc}") from exc


@app.post("/api/chat")
def chat(request: ChatRequest) -> dict[str, str]:
    message = request.message.strip()
    if not message:
        raise HTTPException(status_code=422, detail="Message cannot be blank")
    session = _sessions.get(request.session_id or "")
    session_id = request.session_id
    if session is None:
        try:
            session_id, session = _create_session()
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"Could not start a Danica session: {exc}") from exc
    try:
        with session.lock:
            answer = session.agent.run(message)
        return {"session_id": session_id or "", "reply": answer}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Danica could not complete that request: {exc}") from exc


def mount_frontend() -> None:
    candidates = [Path(__file__).resolve().parent / "dist", Path(__file__).resolve().parents[1] / "web" / "dist", Path.cwd() / "web" / "dist"]
    if getattr(__import__("sys"), "frozen", False):
        candidates.insert(0, Path(getattr(__import__("sys"), "_MEIPASS")) / "web" / "dist")
    frontend = next((path for path in candidates if (path / "index.html").is_file()), None)
    if frontend:
        app.mount("/", StaticFiles(directory=frontend, html=True), name="frontend")


mount_frontend()
