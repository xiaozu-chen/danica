"""
config.py - Loads settings from %APPDATA%/Danica/config.toml.

Anything the user doesn't set falls back to DEFAULTS, so the file is optional.
"""

import json
import os
import tomllib # built into Python 3.11+, read-only TOML parser
import re
from pathlib import Path
from rich.console import Console
from danica.ui import TAG_COLORS as UI_TAG_COLORS, format_log

# Danica's home folder (matches the on-disk layout in PROJECT.md)
APP_DIR = Path(os.environ.get("APPDATA", Path.home())) / "Danica"
CONFIG_PATH = APP_DIR / "config.toml"
SKILLS_DIR = APP_DIR / "skills"
TAG_COLORS = {status: tag["color"] for status, tag in UI_TAG_COLORS.items()}
TAG_COLORS["THINKING"] = "#808080"
TAG_WIDTH = max(map(len, TAG_COLORS))

DEFAULTS = {
    "server": {"base_url": "http://127.0.0.1:8080"},
    "model": {"context": 65536},
    "sandbox": {"memory_mb": 2048, "max_processes": 64},
    "pruning": {"keep_recent": 6, "min_chars": 400},
    "compaction": {"tail_fraction": 0.12},
    "web": {"user_agent": "Danica/1.3.2 (local research assistant)",
            "search_provider": "searxng", "search_url": "http://localhost:8888",
            "search_fallback": "duckduckgo"},   # used when local SearXNG is unreachable; "" disables
    "embeddings": {"enabled": True, "url": "http://localhost:8081",
                   "model_name": "nomic-embed-text-v1.5", "min_similarity": 0.50,
                   "merge_similarity": 0.88},
    # "sixel" draws images inside Windows Terminal (1.22+); "link" prints a clickable path; "off" prints the path only
    "display": {"images": "sixel" if os.environ.get("WT_SESSION") else "link"},
    "documents": {"page": "A4"},      # A4 or LETTER
    # engine: auto (Graphviz if found, else built-in), builtin, or graphviz. confine = run engines in a Job Object.
    "diagrams": {"engine": "auto", "confine": True, "dot_path": "", "mmdc_path": ""},
    # auto: "ask" offers a plan for big-looking requests, "off" = only /plan. min_score: how many of the 5 signals make a request "big"
    "planning": {"auto": "ask", "thinking": True, "min_score": 3},
    "thinking": {
        "auto": False,
        "classifier_enabled": False,
        "classifier_url": "http://127.0.0.1:8082",
        "classifier_model": "SmolLM2-135M-thinking",
        "log_decisions": True,
        "log_prompts": True,
    },
    # email (read-only IMAP, see mail.py). The PASSWORD is never stored here: /mail login keeps it in memory.
    "mail": {"host": "", "user": "", "port": 993, "folder": "INBOX", "ssl": True, "timeout": 20,
             "max_list": 30, "snippet_chars": 240, "body_chars": 6000},
    # computer use (computer.py): smooth = glide the mouse, max_actions = per request, settle = seconds to wait after an action
    # tools.lazy: the tool book (toolbook.py): rarely used tools are one line each in tool_book instead of a full definition in
    # every request; false = send every tool every time. verify.mode: "auto" = check only work that changed files or had
    # a plan/TODO; "always" = every tool-based answer; "off" = never
    "tools": {"lazy": False},
    "verify": {"mode": "auto"},
    "computer": {"smooth": True, "max_actions": 25, "settle": 0.35, "grid": True, "coords": "normalized"},   # coords: "normalized" = x,y 0-1000, "pixels" = picture pixels
    "mcp": {"servers": {}},
    "hooks": {"before": [], "after": []},
    "webcache": {
        "enabled": True,
        "max_pages": 400,
        "half_life_days": 30,
        "digest_items": 6
    }, 
}

console = Console()

def _log(status: str, text: str) -> None:
    console.print(format_log(status, str(text)), highlight=False)

def refresh_context_size() -> int:
    """Ask llama-server for n_ctx again (it may have been restarted with a different -c)."""
    try:
        CONFIG["model"]["context"] = get_llama_context_size(CONFIG["server"]["base_url"])
    except Exception:
        pass                      # keep the last known value
    return CONFIG["model"]["context"]

def get_llama_context_size(server_url: str, timeout: float = 2.0) -> int:
    """Fetches the configured context size (n_ctx) from a running llama-server."""
    import requests                       # imported here: it takes ~0.1 s to load and only this call needs it
    try:
        response = requests.get(f"{server_url}/props", timeout=timeout)
        response.raise_for_status()
        data = response.json()

        # extract context size from default generation settings
        n_ctx = data.get("default_generation_settings", {}).get("n_ctx")
        if not isinstance(n_ctx, int) or n_ctx <= 0:
            raise ValueError("Could not find a valid 'n_ctx' in the server properties response.")
        return n_ctx
    except requests.exceptions.RequestException as e:
        # _log("error", f"Error connecting to llama-server: {e}")
        raise

class ConfigWriteError(Exception):
    """config.toml could not be changed safely (the message says why and is fit to show the user)."""


def _toml_value(value) -> str:
    """A Python value as TOML text (bool, int, float and str are all the settings pane ever writes)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    return json.dumps(str(value), ensure_ascii=False)       # a JSON string is a valid TOML basic string


def _write_checked(new_text: str, section: str, key: str, expect) -> None:
    """Write config.toml only if the new text still parses (and, for a change, holds the value we meant to write).
    A hand-made layout this editor can't handle (an inline table, say) is refused instead of corrupting the file."""
    try:
        parsed = tomllib.loads(new_text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigWriteError(f"config.toml has a layout the settings pane can't edit safely ({e}). Edit it by hand.") from e
    if expect is not None and (parsed.get(section) or {}).get(key) != expect:
        raise ConfigWriteError(f"config.toml has a layout the settings pane can't edit safely ([{section}] {key}). Edit it by hand.")
    APP_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_bytes(new_text.encode("utf-8"))


def save_setting(section: str, key: str, value) -> None:
    """Write `key = value` under [section] in config.toml. Everything else in the file (other keys, comments, the
    line endings) stays as it is. Raises ConfigWriteError if the file can't be changed safely."""
    text = CONFIG_PATH.read_bytes().decode("utf-8-sig") if CONFIG_PATH.exists() else ""
    nl = "\r\n" if "\r\n" in text else "\n"
    line = f"{key} = {_toml_value(value)}"
    found = re.search(rf"(?ms)^\[{re.escape(section)}\][ \t]*\r?\n(.*?)(?=^\[|\Z)", text)
    if found:
        body = found.group(1)
        pattern = rf"(?m)^[ \t]*{re.escape(key)}[ \t]*=[^\r\n]*"
        if re.search(pattern, body):
            body = re.sub(pattern, lambda _: line, body, count=1)
        else:
            body = line + nl + body
        text = text[:found.start(1)] + body + text[found.end(1):]
    else:
        text = text.rstrip() + (nl * 2 if text.strip() else "") + f"[{section}]{nl}{line}{nl}"
    _write_checked(text, section, key, value)


def remove_setting(section: str, key: str) -> None:
    """Delete `key` from [section] in config.toml, so the default applies again. Nothing to do if it isn't there."""
    if not CONFIG_PATH.exists():
        return
    text = CONFIG_PATH.read_bytes().decode("utf-8-sig")
    found = re.search(rf"(?ms)^\[{re.escape(section)}\][ \t]*\r?\n(.*?)(?=^\[|\Z)", text)
    if not found:
        return
    body = re.sub(rf"(?m)^[ \t]*{re.escape(key)}[ \t]*=[^\r\n]*(\r?\n|\Z)", "", found.group(1), count=1)
    if body != found.group(1):
        _write_checked(text[:found.start(1)] + body + text[found.end(1):], section, key, None)


def save_server_url(url: str) -> None:
    """Write base_url under [server] in config.toml, keeping everything else as it is."""
    save_setting("server", "base_url", url)


def set_server(url: str, n_ctx: int) -> None:
    """Use this server from now on: update CONFIG, and remember the address if it changed."""
    changed = CONFIG["server"]["base_url"] != url
    CONFIG["server"]["base_url"] = url
    CONFIG["model"]["context"] = n_ctx
    if changed:
        save_server_url(url)

def load_config() -> dict:
    """Load defaults and merge settings from the user's configuration file."""
    APP_DIR.mkdir(parents=True, exist_ok=True)

    # Copy defaults so we never mutate the module-level constant
    cfg = {section: dict(values) for section, values in DEFAULTS.items()}


    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "rb") as f:  # tomllib needs binary mode
            user = tomllib.load(f)
        # Merge section by section so a partial file doesn't wipe defaults
        for section, values in user.items():
            if section == "routing":
                continue
            cfg.setdefault(section, {}).update(values)
        legacy = user.get("routing", {})
        current = user.get("thinking", {})
        if isinstance(legacy, dict):
            legacy_values = {
                "auto": legacy.get("auto_thinking"),
                "classifier_enabled": legacy.get("classifier_enabled"),
                "classifier_url": legacy.get("classifier_url"),
                "classifier_model": legacy.get("classifier_model"),
                "log_decisions": legacy.get("log_decisions"),
                "log_prompts": legacy.get("log_prompts"),
            }
            cfg["thinking"].update({key: value for key, value in legacy_values.items()
                                    if value is not None})
        if isinstance(current, dict):
            cfg["thinking"].update(current)
    return cfg


# Loaded once at import; everything else does `from danica.config import CONFIG`
CONFIG = load_config()
