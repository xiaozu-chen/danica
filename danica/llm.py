"""
llm.py - Thin client for llama-server's OpenAI-compatible API.

Everything in Danica that talks to the model goes through this file,
so if the server API changes, there is exactly one place to fix.
"""

import json
from typing import Iterator

import httpx

# Where llama-server is listening. Will move to config.toml in a later step.
BASE_URL = "http://127.0.0.1:8080"


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