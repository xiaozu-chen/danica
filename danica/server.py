"""
server.py - talking to llama-server: URL validation, a one-shot probe, and a background monitor.

probe() is the single place that asks the server for its real context size (n_ctx).
ServerMonitor re-asks every few seconds, so a dead server or a restart with a different
-c is noticed instead of silently breaking the context accounting.
"""

from __future__ import annotations

import re
import threading
from typing import Callable
from urllib.parse import urlparse

import httpx

from danica.config import CONFIG

RESERVED_PORTS = {8081, 8082}      # embeddings and the thinking classifier


class ServerError(Exception):
    """Something wrong with the server, worded so it can be shown to the user as is."""


def reserved_ports() -> set[int]:
    """8081/8082 plus whatever ports the config gives the embedding and classifier servers."""
    ports = set(RESERVED_PORTS)
    for url in (CONFIG.get("embeddings", {}).get("url"), CONFIG.get("thinking", {}).get("classifier_url")):
        if isinstance(url, str) and url:
            try:
                ports.add(urlparse(url).port or 80)
            except ValueError:
                pass
    return ports


def normalize_url(raw: str) -> str:
    """Clean up what the user typed. Raises ServerError if it can't be a main-model server URL."""
    url = raw.strip()
    if not url:
        raise ServerError("Enter the address of your llama-server, for example http://127.0.0.1:8080")
    if "://" not in url:
        url = "http://" + url
    url = url.rstrip("/")
    if re.search(r"[\s\"'\\]", url):
        raise ServerError("That address contains characters that are not allowed.")
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError:
        raise ServerError("That address has an invalid port.") from None
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ServerError("Use an address like http://127.0.0.1:8080")
    port = port or (443 if parsed.scheme == "https" else 80)
    if port in reserved_ports():
        raise ServerError(f"Port {port} is reserved for Danica's embedding and classifier "
                          f"servers. Use a different port for the main model.")
    return url


def probe(url: str, timeout: float = 2.0) -> int:
    """The server's real context window (n_ctx). Raises ServerError with a readable reason."""
    try:
        response = httpx.get(f"{url}/props", timeout=timeout)
    except httpx.TimeoutException:
        raise ServerError(f"{url} did not answer in time.") from None
    except httpx.HTTPError:
        raise ServerError(f"Could not connect to {url}. Is llama-server running?") from None
    if response.status_code == 503:
        raise ServerError("The server is still loading the model. Try again in a moment.")
    if response.status_code != 200:
        raise ServerError(f"The server answered HTTP {response.status_code}. Is this llama-server?")
    try:
        n_ctx = response.json()["default_generation_settings"]["n_ctx"]
    except (ValueError, KeyError, TypeError):
        raise ServerError("That does not look like llama-server (no context size in /props).") from None
    if not isinstance(n_ctx, int) or n_ctx <= 0:
        raise ServerError("The server reported an invalid context size.")
    return n_ctx


class ServerMonitor(threading.Thread):
    """Polls the server in the background and reports changes through on_event(kind, data):
         "offline"      data = reason       it stopped answering (after `misses_needed` misses in a row)
         "online"       data = n_ctx        it is answering again
         "ctx_changed"  data = (old, new)   it answered with a different n_ctx (restarted with another -c)
    on_event runs on this thread, so it must only hand the event to the UI (post_message)."""

    def __init__(self, get_url: Callable[[], str], on_event: Callable[[str, object], None],
                 interval: float = 4.0, misses_needed: int = 2) -> None:
        super().__init__(name="server-monitor", daemon=True)
        self.get_url = get_url
        self.on_event = on_event
        self.interval = interval
        self.misses_needed = misses_needed    # one slow reply under load must not look like an outage
        self.online = True
        self.n_ctx: int | None = None
        self._halt = threading.Event()        # (not "_stop": Thread already uses that name)
        self._wake = threading.Event()

    def mark_online(self, n_ctx: int) -> None:
        """The UI reconnected by itself; keep the monitor's picture in sync."""
        self.online, self.n_ctx = True, n_ctx

    def check_now(self) -> None:
        """Poll immediately (e.g. a request just failed with a connection error)."""
        self._wake.set()

    def stop(self) -> None:
        self._halt.set()
        self._wake.set()

    def _emit(self, kind: str, data) -> None:
        try:
            self.on_event(kind, data)
        except Exception:
            pass                              # a failing callback must not kill the monitor

    def run(self) -> None:
        misses = 0
        while not self._halt.is_set():
            try:
                n_ctx = probe(self.get_url(), timeout=2.0)
            except Exception as exc:          # ServerError, or anything unexpected: never let the thread die
                misses += 1
                if self.online and misses >= self.misses_needed:
                    self.online = False
                    self._emit("offline", str(exc) if isinstance(exc, ServerError)
                               else f"{type(exc).__name__}: {exc}")
            else:
                misses = 0
                old, self.n_ctx = self.n_ctx, n_ctx
                if not self.online:
                    self.online = True
                    self._emit("online", n_ctx)
                elif old is not None and old != n_ctx:
                    self._emit("ctx_changed", (old, n_ctx))
            self._wake.wait(self.interval)
            self._wake.clear()