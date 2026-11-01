"""
smoke_test.py - Phase 0 exit test.
Calls the model with thinking OFF then ON and prints the stream.
Run from the project root:  python -m scripts.smoke_test
"""

import time

from rich.console import Console

from danica.llm import stream_chat

console = Console()

QUESTION = "What is 17 * 24? Answer briefly."


def run(thinking: bool) -> None:
    console.rule(f"thinking = {thinking}")
    messages = [{"role": "user", "content": QUESTION}]

    start = time.time()
    tokens = 0  # rough count: one streamed chunk is about one token

    for piece in stream_chat(messages, thinking=thinking):
        tokens += 1
        if piece["type"] == "thinking":
            # dim grey so reasoning is visually separate from the answer
            console.print(piece["text"], style="dim", end="")
        else:
            console.print(piece["text"], end="")

    elapsed = time.time() - start
    console.print(f"\n[cyan]~{tokens} chunks in {elapsed:.1f}s "
                  f"({tokens / elapsed:.1f}/s)[/cyan]")


if __name__ == "__main__":
    run(thinking=False)
    run(thinking=True)