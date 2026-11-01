"""
llm.py - Thin client for llama-server's OpenAI-compatible API.

Everything in Danica that talks to the model goes through this file,
so if the server API changes, there is exactly one place to fix.
"""

import json
from typing import Iterator

import httpx

from danica.config import CONFIG

# Where llama-server is listening (comes from config.toml, or the default)
BASE_URL = CONFIG["server"]["base_url"]


def stream_chat(
    messages: list[dict],
    thinking: bool = False,
    temperature: float = 0.7,
    max_tokens: int = 2048,
    tools: list[dict] | None = None,
    include_usage: bool = False,
) -> Iterator[dict]:
    """
    Stream a chat completion from llama-server.

    Yields small dicts so callers can tell the pieces apart:
        {"type": "thinking",   "text": "..."}    <- the model's reasoning
        {"type": "content",    "text": "..."}    <- the visible answer
        {"type": "usage",      "prompt_tokens": int, "completion_tokens": int}
                                                    <- only if include_usage=True
        {"type": "tool_calls", "calls": [...]}   <- sent ONCE at the end, only if
                                                    the model called tools. Each call is
                                                    {"id": str, "name": str, "arguments": str}
    "arguments" stays a JSON *string*; the agent loop parses it (and handles bad JSON).
    """
    payload = {
        "messages": messages,
        "stream": True,
        "temperature": temperature,
        "max_tokens": max_tokens,
        # Qwen's chat template reads this flag to switch thinking on/off.
        "chat_template_kwargs": {"enable_thinking": thinking},
    }
    if tools:
        payload["tools"] = tools  # OpenAI-style tool schemas from the registry

    if include_usage:
        # Makes the server attach token counts to the final stream chunk
        payload["stream_options"] = {"include_usage": True}

    # Tool calls arrive in fragments keyed by index; collect them here.
    pending: dict[int, dict] = {}

    timeout = httpx.Timeout(connect=5.0, read=None, write=10.0, pool=5.0)

    with httpx.Client(timeout=timeout) as client:
        with client.stream(
            "POST", f"{BASE_URL}/v1/chat/completions", json=payload
        ) as response:
            response.raise_for_status()

            for line in response.iter_lines():
                if not line or not line.startswith("data: "):
                    continue

                data = line[len("data: "):]
                if data == "[DONE]":  # end-of-stream marker
                    break

                chunk = json.loads(data)

                # The usage chunk can have an EMPTY "choices" list, so we must
                # look for it before the "no choices -> skip" check below.
                if chunk.get("usage"):
                    yield {
                        "type": "usage",
                        "prompt_tokens": chunk["usage"].get("prompt_tokens", 0),
                        "completion_tokens": chunk["usage"].get("completion_tokens", 0),
                    }
                if not chunk.get("choices"):
                    continue
                delta = chunk["choices"][0].get("delta", {})

                if delta.get("reasoning_content"):
                    yield {"type": "thinking", "text": delta["reasoning_content"]}
                if delta.get("content"):
                    yield {"type": "content", "text": delta["content"]}

                # Tool-call fragments: the name arrives whole, the arguments
                # arrive as pieces of a JSON string that we append together.
                for tc in delta.get("tool_calls") or []:
                    slot = pending.setdefault(
                        tc.get("index", 0), {"id": "", "name": "", "arguments": ""}
                    )
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["name"] = fn["name"]
                    if fn.get("arguments"):
                        slot["arguments"] += fn["arguments"]
        
    # Stream finished: hand over the assembled tool calls (if any).
    if pending:
        calls = []
        for index in sorted(pending):
            call = pending[index]
            # Some server versions omit ids; the API needs one to pair results with calls
            call["id"] = call["id"] or f"call_{index}"
            calls.append(call)
        yield {"type": "tool_calls", "calls": calls}


def chat(
        messages: list[dict],
        thinking: bool = False,
        temperature: float = 0.0,
        max_tokens: int = 4096,
) -> dict:
    """
    Non-streaming convenience wrapper: runs stream_chat and collects everything.
    Return {"thinking": "...", "content": "..."}.
    Temperature default to 0 so eval runs are as repeatable as possible.
    """
    thinking_parts, content_parts = [], []
    for piece in stream_chat(messages, thinking, temperature, max_tokens):
        (thinking_parts if piece["type"] == "thinking" else content_parts).append(piece["text"])

    return {"thinking": "".join(thinking_parts), "content": "".join(content_parts)}