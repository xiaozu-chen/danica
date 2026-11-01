"""
tasks.py - The scripted eval tasks.

Each task is: a prompt, whether thinking is on, and a check() function that
looks at the model's answer and returns True (pass) or False (fail).
New phases add new tasks here; never delete old ones, or you lose regression
coverage.
"""

import json
from dataclasses import dataclass
from typing import Callable


@dataclass
class Task:
    name: str                       # unique id, shown in the results table
    prompt: str                     # what we send as the user message
    check: Callable[[str], bool]    # gets the model's answer, returns pass/fail
    thinking: bool = False          # thinking mode for this task


# A small fake config used by the "answer from a file" tasks
SAMPLE_FILE = """\
# server.conf
host = 10.0.0.5
port = 8472
workers = 6
debug = false
"""


def _is_json_with_name(answer: str) -> bool:
    """Passes if the answer is valid JSON with name == 'Danica'."""
    # Models like to wrap JSON in ```fences```; strip them before parsing
    cleaned = answer.strip().removeprefix("```json").removeprefix("```")
    cleaned = cleaned.removesuffix("```").strip()
    try:
        return json.loads(cleaned).get("name") == "Danica"
    except (json.JSONDecodeError, AttributeError):
        return False


TASKS = [
    Task(
        name="arithmetic",
        prompt="What is 17 * 24? Reply with only the number.",
        check=lambda a: "408" in a,
    ),
    Task(
        name="exact_word",
        prompt="Reply with exactly one word and nothing else: PINEAPPLE",
        check=lambda a: a.strip().strip(".").upper() == "PINEAPPLE",
    ),
    Task(
        name="answer_from_file_port",
        prompt=f"{SAMPLE_FILE}\nWhat port does the server use? Reply with only the number.",
        check=lambda a: "8472" in a,
    ),
    Task(
        name="answer_from_file_workers",
        prompt=f"{SAMPLE_FILE}\nHow many workers are configured? Reply with only the number.",
        check=lambda a: "6" in a,
    ),
    Task(
        name="json_output",
        prompt='Return a JSON object with a single key "name" whose value is "Danica". Output only JSON.',
        check=_is_json_with_name,
    ),
    Task(
        name="logic_puzzle_thinking",
        prompt=(
            "Alice is taller than Bob. Bob is taller than Cara. "
            "Dan is shorter than Cara. Who is the tallest? Reply with only the name."
        ),
        check=lambda a: "alice" in a.lower(),
        thinking=True,
    ),
]