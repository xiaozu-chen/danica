"""
settings.py - the list of settings the user can change in the /config pane, and how to check, apply and save one.

Only the USER can change these (the pane is part of the interface; no tool the model has can reach this file). A change
is applied to the running Danica straight away (it updates CONFIG, which the rest of the code reads when it needs a
value) and is written to config.toml so it is still there next time. Resetting a setting removes it from config.toml,
so the built-in default applies again.

To add a setting: add a Setting to SETTINGS below (its default must exist in config.DEFAULTS). If the value is copied
somewhere when Danica starts (not read from CONFIG each time), add a line to apply() so it reaches the copy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from danica import config
from danica.config import CONFIG, DEFAULTS


class SettingError(ValueError):
    """The typed value is not allowed. The message is fit to show the user."""


@dataclass(frozen=True)
class Setting:
    group: str                      # tab in the pane
    section: str                    # [section] in config.toml
    key: str
    label: str
    kind: str                       # "bool" | "choice" | "int" | "float" | "text"
    help: str
    choices: tuple[tuple[str, Any], ...] = ()      # (what the pane shows, the value stored)
    lo: float | None = None         # smallest / largest allowed number
    hi: float | None = None
    note: str = ""                  # shown after a change, e.g. when it only applies from the next command

    @property
    def path(self) -> str:
        return f"{self.section}.{self.key}"


SETTINGS: tuple[Setting, ...] = (
    # ---- speed and tools
    Setting("Speed & tools", "tools", "lazy", "Tool book", "bool",
            "On: rarely used tools are listed in tool_book and loaded when needed (shorter prompts). "
            "Off: every tool is sent with every request (longer prompts, no extra step).",
            note="applies from the next message"),
    Setting("Speed & tools", "verify", "mode", "Final check", "choice",
            "auto: re-check only work that changed files or had a plan.  always: after every answer that used tools.  off: never.",
            choices=(("auto", "auto"), ("always", "always"), ("off", "off"))),
    Setting("Speed & tools", "pruning", "keep_recent", "Tool results kept in full", "int",
            "Older tool results are shortened to save context. This many of the newest stay whole.", lo=1, hi=50),
    Setting("Speed & tools", "pruning", "min_chars", "Shorten results longer than (characters)", "int",
            "A tool result shorter than this is never shortened.", lo=100, hi=5000),
    Setting("Speed & tools", "compaction", "tail_fraction", "Recent chat kept when compacting", "float",
            "Share of the context window (0.05 to 0.5) kept word for word when an old conversation is summarised.", lo=0.05, hi=0.5),
    # ---- thinking and planning
    Setting("Thinking & plans", "thinking", "auto", "Automatic thinking", "bool",
            "Let Danica decide per message whether the model should think first."),
    Setting("Thinking & plans", "planning", "auto", "Offer a plan for big requests", "choice",
            "ask: Danica offers a plan when a request looks big.  off: only when you type /plan.",
            choices=(("ask", "ask"), ("off", "off"))),
    Setting("Thinking & plans", "planning", "thinking", "Think while planning", "bool",
            "Use the model's thinking mode when it drafts a plan."),
    Setting("Thinking & plans", "planning", "min_score", "Plan sensitivity", "int",
            "How many of the 5 'this looks big' signals must match before a plan is offered. Lower = offered more often.",
            lo=1, hi=5),
    # ---- computer use
    Setting("Computer use", "computer", "smooth", "Smooth mouse movement", "bool",
            "Glide the cursor instead of jumping, so you can follow what it does."),
    Setting("Computer use", "computer", "max_actions", "Actions per request", "int",
            "Clicks, keys and drags Danica may do before it must stop and report.", lo=1, hi=200),
    Setting("Computer use", "computer", "settle", "Wait after each action (seconds)", "float",
            "Pause so the window can react before the next picture is taken.", lo=0.0, hi=5.0),
    Setting("Computer use", "computer", "grid", "Grid on pictures", "bool",
            "Draw labelled cells (A1, B2...) on the pictures so Danica can name a spot."),
    Setting("Computer use", "computer", "coords", "Click coordinates", "choice",
            "normalized: x and y from 0 to 1000 across the picture (recommended).  pixels: pixel positions in the picture.",
            choices=(("normalized", "normalized"), ("pixels", "pixels"))),
    # ---- web and memory
    Setting("Web & memory", "web", "search_url", "Search server (SearXNG)", "text",
            "Address of your local search server, e.g. http://localhost:8888"),
    Setting("Web & memory", "web", "search_fallback", "Search fallback", "choice",
            "Used when the search server can't be reached.",
            choices=(("DuckDuckGo", "duckduckgo"), ("Off", ""))),
    Setting("Web & memory", "embeddings", "enabled", "Meaning-based search (embeddings)", "bool",
            "Needs the embedding server. Off: memory and the web cache match by keywords only."),
    Setting("Web & memory", "embeddings", "min_similarity", "Minimum match score", "float",
            "How close in meaning a note must be to be recalled (0 to 1). Higher = stricter.", lo=0.0, hi=1.0),
    Setting("Web & memory", "webcache", "enabled", "Keep fetched web pages", "bool",
            "Store pages Danica reads so they can be searched again for free."),
    Setting("Web & memory", "webcache", "max_pages", "Web cache size (pages)", "int",
            "The oldest, least-used pages are dropped beyond this.", lo=10, hi=5000),
    Setting("Web & memory", "webcache", "half_life_days", "Web cache memory (days)", "int",
            "Pages older than this count half as much when ranking.", lo=1, hi=365),
    # ---- display and files
    Setting("Display & files", "display", "images", "Show pictures", "choice",
            "sixel: draw them in the terminal (Windows Terminal 1.22+).  link: print a clickable path.  off: path only.",
            choices=(("sixel", "sixel"), ("link", "link"), ("off", "off"))),
    Setting("Display & files", "documents", "page", "Paper size", "choice",
            "For Word and PDF documents Danica makes.", choices=(("A4", "A4"), ("Letter", "LETTER"))),
    Setting("Display & files", "diagrams", "engine", "Diagram engine", "choice",
            "auto: Graphviz if installed, else the built-in drawer.",
            choices=(("auto", "auto"), ("built-in", "builtin"), ("Graphviz", "graphviz"))),
    # ---- limits
    Setting("Limits", "sandbox", "memory_mb", "Command memory cap (MB)", "int",
            "Most memory a command Danica runs may use. Too low and big tasks fail.", lo=256, hi=16384,
            note="applies from the next command"),
    Setting("Limits", "sandbox", "max_processes", "Command process cap", "int",
            "Most processes a command may start.", lo=8, hi=512, note="applies from the next command"),
)
BY_PATH = {s.path: s for s in SETTINGS}


def groups() -> list[str]:
    """Tab names, in order."""
    return list(dict.fromkeys(s.group for s in SETTINGS))


def in_group(group: str) -> list[Setting]:
    """The settings of one tab."""
    return [s for s in SETTINGS if s.group == group]


def default(s: Setting) -> Any:
    """The built-in default."""
    return DEFAULTS[s.section][s.key]


def current(s: Setting) -> Any:
    """The value in use right now."""
    return (CONFIG.get(s.section) or {}).get(s.key, default(s))


def show(s: Setting, value: Any) -> str:
    """A value as the pane shows it (a choice shows its label)."""
    if s.kind == "choice":
        return next((label for label, v in s.choices if v == value), str(value))
    if s.kind == "bool":
        return "on" if value else "off"
    return str(value)


def parse(s: Setting, text: str) -> Any:
    """Check what the user typed (or picked) and return the value to store. Raises SettingError."""
    text = str(text).strip()
    if s.kind == "bool":
        if text.lower() in ("true", "on", "yes", "1"):
            return True
        if text.lower() in ("false", "off", "no", "0"):
            return False
        raise SettingError("use on or off")
    if s.kind == "choice":
        for label, value in s.choices:
            if text.lower() in (label.lower(), str(value).lower()):
                return value
        raise SettingError("choose one of: " + ", ".join(label for label, _ in s.choices))
    if s.kind in ("int", "float"):
        try:
            number = int(text) if s.kind == "int" else float(text)
        except ValueError:
            raise SettingError("type " + ("a whole number" if s.kind == "int" else "a number")) from None
        if (s.lo is not None and number < s.lo) or (s.hi is not None and number > s.hi):
            lo = int(s.lo) if s.kind == "int" else s.lo
            hi = int(s.hi) if s.kind == "int" else s.hi
            raise SettingError(f"must be between {lo} and {hi}")
        return number
    if "\n" in text or "\r" in text or len(text) > 200:
        raise SettingError("one line, at most 200 characters")
    if s.key.endswith("url") and text and not text.lower().startswith(("http://", "https://")):
        raise SettingError("must start with http:// or https://")
    return text


def apply(s: Setting, value: Any, app=None) -> None:
    """Put the value to use in the running Danica. Most code reads CONFIG when it needs a value, so setting CONFIG is
    enough; the few places that copied a value when they started are updated here."""
    CONFIG.setdefault(s.section, {})[s.key] = value
    if app is None:
        return
    if s.path == "planning.auto":
        app.plan_auto = value                                  # copied when the app started
    if s.section == "computer":
        comp = getattr(getattr(app, "ws", None), "computer", None)
        if comp is not None and hasattr(comp, s.key):          # the session's Computer copied these when it was created
            setattr(comp, s.key, value)


def change(s: Setting, value: Any, app=None) -> str | None:
    """Apply and save. Returns None when it was saved, else why it could NOT be saved (it still applies until Danica closes)."""
    apply(s, value, app)
    try:
        config.save_setting(s.section, s.key, value)
    except (config.ConfigWriteError, OSError) as e:
        return str(e)
    audit = getattr(app, "audit", None)
    if audit is not None:
        audit.append("config_change", setting=s.path, value=str(value))
    return None


def reset(s: Setting, app=None) -> str | None:
    """Back to the default, and remove it from config.toml. Same return as change()."""
    apply(s, default(s), app)
    try:
        config.remove_setting(s.section, s.key)
    except (config.ConfigWriteError, OSError) as e:
        return str(e)
    audit = getattr(app, "audit", None)
    if audit is not None:
        audit.append("config_change", setting=s.path, value="(default)")
    return None
