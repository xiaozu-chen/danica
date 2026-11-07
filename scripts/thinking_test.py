"""Binary thinking-classifier and fallback tests; no model required."""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import httpx

import danica.agent as agent_mod
import danica.config as config_mod
from danica.agent import Agent
from danica.config import CONFIG
from danica.thinking import (
    ThinkingClassifier,
    ThinkingDecision,
    log_thinking_decision,
    parse_thinking_response,
)
from danica.workspace import Workspace


def check(label: str, condition: bool) -> None:
    print(("PASS  " if condition else "FAIL  ") + label)
    assert condition, label


rules = ThinkingClassifier({"classifier_enabled": False})
check("rule fallback classifies a multi-step request as thinking",
      rules.classify("Plan a multi-step refactor").thinking)
check("rule fallback classifies a simple lookup as non-thinking",
      not rules.classify("Hello, what time is it?").thinking)


class FakeResponse:
    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        return None

    def json(self):
        return {"choices": [{"message": {"content": self.content}}]}


configured = {"classifier_enabled": True, "classifier_url": "http://127.0.0.1:8082",
              "classifier_model": "SmolLM2-135M-thinking"}
with patch("danica.thinking.httpx.post", return_value=FakeResponse('{"thinking": true}')) as post:
    result = ThinkingClassifier(configured).classify("hello")
    request = post.call_args.kwargs["json"]
    response_schema = request["response_format"]["json_schema"]["schema"]
check("fine-tuned classifier returns a binary thinking decision",
      result == ThinkingDecision(True, "smollm2-135m")
      and request["model"] == "SmolLM2-135M-thinking"
      and response_schema["properties"] == {"thinking_mode": {"type": "boolean"}}
      and response_schema["required"] == ["thinking_mode"])

with patch("danica.thinking.httpx.post",
           return_value=FakeResponse("{'thinking_mode': true}")):
    screenshot_format = ThinkingClassifier(configured).classify("Compare these approaches")
check("classifier accepts screenshot's single-quoted thinking_mode response",
      screenshot_format == ThinkingDecision(True, "smollm2-135m"))
check("classifier response parser accepts false and legacy JSON key",
      parse_thinking_response("{'thinking_mode': false}") is False
      and parse_thinking_response('{"thinking": false}') is False)

with patch("danica.thinking.httpx.post",
           return_value=FakeResponse('{"thinking_mode":"yes","route":"coding"}')):
    invalid = ThinkingClassifier(configured).classify("Plan a migration")
check("non-boolean or extra classifier output falls back to thinking rules",
      invalid.source == "rules_fallback" and invalid.thinking and bool(invalid.error))

with patch("danica.thinking.httpx.post", side_effect=httpx.ConnectError("offline")):
    fallback = ThinkingClassifier(configured).classify("Plan a migration")
check("classifier outage is explicit and falls back to rules",
      fallback.source == "rules_fallback" and fallback.thinking and bool(fallback.error))

with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
    log_path = Path(tmp) / "thinking.jsonl"
    decision = ThinkingDecision(True, "smollm2-135m")
    log_thinking_decision("hello", decision, log_path,
                          {"log_decisions": False, "log_prompts": True})
    check("thinking logs are disabled unless explicitly opted in", not log_path.exists())
    secret = "AKIAIOSFODNN7EXAMPLE"
    log_thinking_decision(f"analyze {secret}", decision, log_path,
                          {"log_decisions": True, "log_prompts": True})
    entry = json.loads(log_path.read_text(encoding="utf-8"))
    check("opt-in decision logs redact secrets and contain only thinking metadata",
          secret not in json.dumps(entry) and entry["thinking"] is True
          and "text_hash" in entry and "route" not in entry)

old_auto = CONFIG["thinking"]["auto"]
old_stream = agent_mod.stream_chat
try:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        CONFIG["thinking"]["auto"] = True
        agent = Agent(Workspace(Path(tmp) / "workspace"))
        calls = []

        def fake_stream(messages, **kwargs):
            calls.append(kwargs)
            yield {"type": "content", "text": "done"}

        agent_mod.stream_chat = fake_stream
        with patch("danica.agent.ThinkingClassifier.classify",
                   return_value=ThinkingDecision(True, "smollm2-135m")):
            agent.run("compare two designs")
            agent.thinking_override = False
            agent.run("another complex request")
        check("automatic thinking classification applies unless manually overridden",
              calls[0]["thinking"] is True and calls[1]["thinking"] is False)
finally:
    CONFIG["thinking"]["auto"] = old_auto
    agent_mod.stream_chat = old_stream

with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
    app_dir = Path(tmp)
    config_path = app_dir / "config.toml"
    config_path.write_text(
        '[routing]\nauto_thinking = true\nclassifier_model = "legacy-model"\n'
        '[thinking]\nclassifier_model = "current-model"\n',
        encoding="utf-8",
    )
    with patch.object(config_mod, "APP_DIR", app_dir), \
            patch.object(config_mod, "CONFIG_PATH", config_path):
        loaded = config_mod.load_config()
    check("legacy routing config migrates to thinking-only settings",
          loaded["thinking"]["auto"] is True
          and loaded["thinking"]["classifier_model"] == "current-model"
          and "routing" not in loaded)

print("\nAll good.")
