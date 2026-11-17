"""Binary thinking-mode classification with a deterministic fallback."""

import hashlib
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from danica import classifier
from danica.config import APP_DIR, CONFIG
from danica.secret_scan import redact

THINKING_RULES = re.compile(
    r"\b(code|bug|implement|refactor|plan|multi[- ]step|step by step|"
    r"compare|analy[sz]e|derive|prove|calculate|math|design|debug|test)\b", re.I
)
_PYTHON_STYLE_DECISION = re.compile(
    r"\{\s*'thinking_mode'\s*:\s*(true|false)\s*\}", re.IGNORECASE
)


def parse_thinking_response(content: str) -> bool:
    """Parse strict JSON or the classifier's Python-dict-style thinking_mode output."""
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        match = _PYTHON_STYLE_DECISION.fullmatch(content.strip())
        if match:
            return match.group(1).lower() == "true"
        raise ValueError("classifier output must be a thinking boolean object")

    if not isinstance(parsed, dict) or len(parsed) != 1:
        raise ValueError("classifier output must contain exactly one thinking field")
    key, value = next(iter(parsed.items()))
    if key not in ("thinking", "thinking_mode") or not isinstance(value, bool):
        raise ValueError("classifier output must contain a boolean thinking field")
    return value


@dataclass(frozen=True)
class ThinkingDecision:
    thinking: bool
    source: str
    error: str = ""


class ThinkingClassifier:
    def __init__(self, config: dict | None = None):
        self.config = config if config is not None else CONFIG["thinking"]

    @staticmethod
    def rules(text: str) -> bool:
        return bool(THINKING_RULES.search(text))

    def classify(self, text: str) -> ThinkingDecision:
        """The MAIN model decides (classifier.py: same server, its own tool). The regex rules are the fallback
        when the classifier is off, the server has too few slots, or it gives no usable answer."""
        fallback = self.rules(text)
        ready, _why = classifier.status()
        if not ready:                                   # off / one slot: rules only, no warning on every request
            return ThinkingDecision(fallback, "rules")
        value, why = classifier.decide_thinking(text)
        if value is None:
            return ThinkingDecision(fallback, "rules_fallback", why)
        return ThinkingDecision(value, "main-model")


def log_thinking_decision(text: str, decision: ThinkingDecision, path: Path | None = None,
                          config: dict | None = None) -> None:
    cfg = config if config is not None else CONFIG["thinking"]
    if not cfg.get("log_decisions", False):
        return
    safe_text, _ = redact(text[:4000])
    entry = {
        "ts": time.time(),
        "text_hash": hashlib.sha256(safe_text.encode("utf-8")).hexdigest(),
        "thinking": decision.thinking,
        "source": decision.source,
    }
    if cfg.get("log_prompts", False):
        entry["text"] = safe_text
    target = path or APP_DIR / "logs" / "thinking.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
