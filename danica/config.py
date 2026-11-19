"""
config.py - Loads settings from %APPDATA%/Danica/config.toml.

Anything the user doesn't set falls back to DEFAULTS, so the file is optional.
"""

import os
import tomllib # built into Python 3.11+, read-only TOML parser
import re
from pathlib import Path
from rich.console import Console
from rich.markup import escape

# Danica's home folder (matches the on-disk layout in PROJECT.md)
APP_DIR = Path(os.environ.get("APPDATA", Path.home())) / "Danica"
CONFIG_PATH = APP_DIR / "config.toml"
SKILLS_DIR = APP_DIR / "skills"

TAG_COLORS = {
    "ERROR": "#E03E3E",     # Clear, vibrant red
    "WARNING": "#E5C158",   # Brighter golden yellow
    "OK": "#4CAF50",        # Clear emerald green
    "FAIL": "#E67E22",      # Rich orange
    "INFO": "#F5F6FA",      # Bright clean white
    "THINKING": "#808080",  # Unchanged neutral gray
}
TAG_WIDTH = max(map(len, TAG_COLORS))   # updates itself if you add a longer label

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
    # tools.lazy: send only the tools a request needs (toolgroups.py). verify.mode: "auto" = check only work that changed files or had
    # a plan/TODO; "always" = every tool-based answer; "off" = never
    "tools": {"lazy": True},
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

def _log(status, text):
    key = status.upper()
    color = TAG_COLORS.get(key, "white")
    label = f"{key:^{TAG_WIDTH}}"
    console.print(f"[bold #000000 on {color}] {label} [/]::{escape(str(text))}", highlight=False)

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

def save_server_url(url: str) -> None:
    """Write base_url under [server] in config.toml, keeping everything else as it is."""
    text = CONFIG_PATH.read_text(encoding="utf-8") if CONFIG_PATH.exists() else ""
    line = f'base_url = "{url}"'
    section = re.search(r"(?ms)^\[server\][ \t]*\n(.*?)(?=^\[|\Z)", text)
    if section:
        body = section.group(1)
        if re.search(r"(?m)^[ \t]*base_url[ \t]*=", body):
            body = re.sub(r"(?m)^[ \t]*base_url[ \t]*=.*$", lambda _: line, body)
        else:
            body = line + "\n" + body
        text = text[:section.start(1)] + body + text[section.end(1):]
    else:
        text = text.rstrip() + ("\n\n" if text.strip() else "") + f"[server]\n{line}\n"
    CONFIG_PATH.write_text(text, encoding="utf-8")


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
