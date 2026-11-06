"""
config.py - Loads settings from %APPDATA%/Danica/config.toml.

Anything the user doesn't set falls back to DEFAULTS, so the file is optional.
"""

import os
import tomllib # built into Python 3.11+, read-only TOML parser
from pathlib import Path

# Danica's home folder (matches the on-disk layout in PROJECT.md)
APP_DIR = Path(os.environ.get("APPDATA", Path.home())) / "Danica"
CONFIG_PATH = APP_DIR / "config.toml"
SKILLS_DIR = APP_DIR / "skills"

DEFAULTS = {
    "server": {"base_url": "http://127.0.0.1:8080"},
    "model": {"context": 65536},
    "sandbox": {"memory_mb": 2048, "max_processes": 64},
    "pruning": {"keep_recent": 6, "min_chars": 400},
    "compaction": {"tail_fraction": 0.12},
    "web": {"user_agent": "Danica/0.1 (local research assistant)",
            "search_provider": "searxng", "search_url": "http://localhost:8888"},
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
}

def load_config() -> dict:
    APP_DIR.mkdir(parents=True, exist_ok=True)

    # Copy defaults so we never mutate the module-level constant
    cfg = {section: dict(values) for section, values in DEFAULTS.items()}

    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "rb") as f:  # tomllib needs binary mode
            user = tomllib.load(f)
        # Merge section by section so a partial file doesn't wipe defaults
        for section, values in user.items():
            cfg.setdefault(section, {}).update(values)
    return cfg


# Loaded once at import; everything else does `from danica.config import CONFIG`
CONFIG = load_config()
