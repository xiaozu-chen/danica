"""Binary thinking-mode classification with a deterministic fallback."""

import hashlib
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

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
        fallback = self.rules(text)
        if not self.config.get("classifier_enabled", False):
            return ThinkingDecision(fallback, "rules")
        url = str(self.config.get("classifier_url", "")).rstrip("/")
        model = str(self.config.get("classifier_model", "")).strip()
        if not url or not model:
            return ThinkingDecision(fallback, "rules_fallback",
                                    "classifier URL/model is not configured")
        schema = {
            "type": "json_schema",
            "json_schema": {
                "name": "thinking_decision",
                "schema": {
                    "type": "object", "additionalProperties": False,
                    "properties": {"thinking_mode": {"type": "boolean"}},
                    "required": ["thinking_mode"],
                },
            },
        }
        try:
            response = httpx.post(
                f"{url}/v1/chat/completions",
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": (
                            'Classify whether the user message needs deliberate reasoning. '
                            'Return exactly {"thinking_mode": true} or {"thinking_mode": false}.'
                        )},
                        {"role": "user", "content": text[:4000]},
                    ],
                    "temperature": 0,
                    "max_tokens": 40,
                    "response_format": schema,
                },
                timeout=httpx.Timeout(2.0, connect=0.5),
                trust_env=False,
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            return ThinkingDecision(parse_thinking_response(content), "smollm2-135m")
        except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError, json.JSONDecodeError) as exc:
            return ThinkingDecision(fallback, "rules_fallback",
                                    f"classifier unavailable or invalid ({type(exc).__name__})")


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
