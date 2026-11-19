"""
llm.py - Thin client for llama-server's OpenAI-compatible API.

Everything in Danica that talks to the model goes through this file,
so if the server API changes, there is exactly one place to fix.
"""

import contextlib
import json
import threading
import time
from typing import Any, Iterator

import httpx

from danica import perf
from danica.config import CONFIG

# Where llama-server is listening (comes from config.toml, or the default)
BASE_URL = CONFIG["server"]["base_url"]

# ONE shared HTTP client for the whole program. Making a new client per call loads the certificate store every time
# (about 0.1 s each, and a request with tool steps makes several calls) and throws the connection away.
_client: httpx.Client | None = None
_client_lock = threading.Lock()
TIMEOUT = httpx.Timeout(connect=5.0, read=None, write=10.0, pool=5.0)


def _http() -> httpx.Client:
    global _client
    with _client_lock:
        if _client is None:
            _client = httpx.Client(timeout=TIMEOUT)
        return _client


def reset_client() -> None:
    """Drop the shared client (after a connection error, so the next call starts clean)."""
    global _client
    with _client_lock:
        old, _client = _client, None
    if old is not None:
        try:
            old.close()
        except Exception:
            pass


@contextlib.contextmanager
def _shared():
    yield _http()


_local: httpx.Client | None = None


def local_http() -> httpx.Client:
    """The shared client for the small side calls (titles, classifier, embeddings): local server, never via a proxy.
    Give each request its own timeout=."""
    global _local
    with _client_lock:
        if _local is None:
            _local = httpx.Client(trust_env=False)
        return _local


@contextlib.contextmanager
def local_client() -> Iterator[httpx.Client]:
    """Return the shared HTTP client for the local model server."""
    yield local_http()


def _stream_chat(
    messages: list[dict],
    thinking: bool = False,
    temperature: float = 0.7,
    tools: list[dict] | None = None,
    include_usage: bool = False,
    stream_tools: bool = False,
    label: str = "chat",
    tool_choice: str | None = None,
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
        # Qwen's chat template reads this flag to switch thinking on/off.
        "chat_template_kwargs": {"enable_thinking": thinking},
        # Keep the processed prompt in the server's memory and reuse its start next time (the default on new servers,
        # explicit here so an older build does not re-read the whole conversation on every message)
        "cache_prompt": True,
    }
    if tools:
        payload["tools"] = tools  # OpenAI-style tool schemas from the registry
        if tool_choice:
            payload["tool_choice"] = tool_choice          # "none": the tools are listed (same prompt start, cache reused) but not used

    if include_usage:
        # Makes the server attach token counts to the final stream chunk
        payload["stream_options"] = {"include_usage": True}

    # Tool calls arrive in fragments keyed by index; collect them here.
    pending: dict[int, dict] = {}
    finish = ""          # why the server stopped: "stop", "length" (hit a token limit), "tool_calls"

    t0 = time.perf_counter()
    first = None                 # when the first word / tool call arrived
    usage: dict = {}
    timings: dict = {}           # llama-server's own numbers: how much of the prompt was reused from its cache

    with _shared() as client:
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
                    usage = chunk["usage"]
                if chunk.get("timings"):
                    timings = chunk["timings"]
                if chunk.get("usage"):
                    yield {
                        "type": "usage",
                        "prompt_tokens": chunk["usage"].get("prompt_tokens", 0),
                        "completion_tokens": chunk["usage"].get("completion_tokens", 0),
                    }
                if not chunk.get("choices"):
                    continue
                delta = chunk["choices"][0].get("delta", {})
                finish = chunk["choices"][0].get("finish_reason") or finish

                if first is None and (delta.get("reasoning_content") or delta.get("content") or delta.get("tool_calls")):
                    first = time.perf_counter() - t0
                if delta.get("reasoning_content"):
                    yield {"type": "thinking", "text": delta["reasoning_content"]}
                if delta.get("content"):
                    yield {"type": "content", "text": delta["content"]}

                # Tool-call fragments: the name arrives whole, the arguments
                # arrive as pieces of a JSON string that we append together.
                for tc in delta.get("tool_calls") or []:
                    index = tc.get("index", 0)
                    slot = pending.setdefault(
                        index, {"id": "", "name": "", "arguments": "", "announced": False}
                    )
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["name"] = fn["name"]
                    frag = fn.get("arguments") or ""
                    slot["arguments"] += frag

                    # Tell the caller the moment a call starts, then pass each argument piece on
                    if stream_tools and slot["name"]:
                        if not slot["announced"]:
                            slot["announced"] = True
                            yield {"type": "tool_start", "index": index, "name": slot["name"]}
                            frag = slot["arguments"]      # anything that arrived before the name
                        if frag:
                            yield {"type": "tool_delta", "index": index, "text": frag}

    now = time.perf_counter() - t0
    perf.record_call(
        label=label,
        first=first if first is not None else now,
        total=now,
        thinking=thinking,
        prompt_tokens=usage.get("prompt_tokens", 0),
        completion_tokens=usage.get("completion_tokens", 0),
        cache_n=timings.get("cache_n"),
        prompt_n=timings.get("prompt_n"),
        tools_chars=len(json.dumps(tools)) if tools else 0,
    )
    if include_usage and finish:           # a new piece type: only callers that asked for usage (the agent loop) get it
        yield {"type": "finish", "reason": finish}

    # Stream finished: hand over the assembled tool calls (if any).
    if pending:
        calls = []
        for index in sorted(pending):
            call = pending[index]
            call.pop("announced", None)
            # Some server versions omit ids; the API needs one to pair results with calls
            call["id"] = call["id"] or f"call_{index}"
            calls.append(call)
        yield {"type": "tool_calls", "calls": calls}


def stream_chat(*args: Any, **kwargs: Any) -> Iterator[dict]:
    """Stream a chat completion (see _stream_chat). After a connection error the shared client is dropped so the next
    call starts clean (for example after llama-server was restarted)."""
    try:
        yield from _stream_chat(*args, **kwargs)
    except (httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadError):
        reset_client()
        raise


def chat(
        messages: list[dict],
        thinking: bool = False,
        temperature: float = 0.0,
) -> dict:
    """
    Non-streaming convenience wrapper: runs stream_chat and collects everything.
    Return {"thinking": "...", "content": "..."}.
    Temperature default to 0 so eval runs are as repeatable as possible.
    """
    thinking_parts, content_parts = [], []
    for piece in stream_chat(messages, thinking, temperature):
        (thinking_parts if piece["type"] == "thinking" else content_parts).append(piece["text"])

    return {"thinking": "".join(thinking_parts), "content": "".join(content_parts)}
