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
) -> Iterator[dict]:
    """
    Stream a chat completion from llama-server.

    Yields small dicts so callers can tell reasoning apart from the answer:
        {"type": "thinking", "text": "..."}   <- the model's reasoning
        {"type": "content",  "text": "..."}   <- the visible answer
    """
    payload = {
        "messages": messages,
        "stream": True,
        "temperature": temperature,
        "max_tokens": max_tokens,
        # Qwen's chat template reads this flag to switch thinking on/off.
        # This is the "boolean" our future router will produce.
        "chat_template_kwargs": {"enable_thinking": thinking},
    }

    # timeout=None on read: local models can be slow to produce the first token.
    timeout = httpx.Timeout(connect=5.0, read=None, write=10.0, pool=5.0)

    with httpx.Client(timeout=timeout) as client:
        with client.stream(
            "POST", f"{BASE_URL}/v1/chat/completions", json=payload
        ) as response:
            response.raise_for_status()

            # The server sends Server-Sent Events: lines like "data: {...}"
            for line in response.iter_lines():
                if not line or not line.startswith("data: "):
                    continue

                data = line[len("data: "):]
                if data == "[DONE]":  # end-of-stream marker
                    break

                chunk = json.loads(data)
                if not chunk.get("choices"):
                    continue
                delta = chunk["choices"][0].get("delta", {})

                # llama-server puts reasoning in "reasoning_content"
                # (when --jinja is on) and the answer in "content".
                if delta.get("reasoning_content"):
                    yield {"type": "thinking", "text": delta["reasoning_content"]}
                if delta.get("content"):
                    yield {"type": "content", "text": delta["content"]}

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