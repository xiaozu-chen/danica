"""Planning helpers for large requests and user-approved execution plans."""

import re

from danica.secret_scan import find_secrets, redact
from danica.tools.todo import counts, normalize

MAX_PLAN_CHARS = 3000
MAX_REQUEST_CHARS = 16000
ACTION_RX = re.compile(
    r"\b(?:add|build|create|design|develop|document|fix|implement|migrate|"
    r"refactor|remove|rename|replace|test|update|upgrade|verify)\w*\b",
    re.IGNORECASE,
)
LIST_RX = re.compile(r"^\s*(?:[-*]\s+|\d+[.)]\s+)", re.MULTILINE)


class PlanError(ValueError):
    """A proposed or edited plan is not safe or usable."""


def needs_plan(request: str) -> bool:
    """Use inexpensive request shape signals to identify multi-step work."""
    lines = request.splitlines()
    list_items = sum(bool(LIST_RX.match(line)) for line in lines)
    sentence_count = len([part for part in re.split(r"[.!?]+", request) if part.strip()])
    return (
        len(request) >= 320
        or list_items >= 2
        or sentence_count >= 3
        or len(ACTION_RX.findall(request)) >= 2
    )


def normalize_plan(text: str) -> str:
    """Normalize checklist numbering and reject empty, oversized, or secret plans."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[^\n]*\n|\n```$", "", cleaned).strip()
    plan = normalize(cleaned)
    total, _ = counts(plan)
    if total < 2:
        raise PlanError("a large-task plan must have at least two checklist steps")
    if len(plan) > MAX_PLAN_CHARS:
        raise PlanError(f"plan exceeds the {MAX_PLAN_CHARS}-character limit")
    if find_secrets(plan):
        raise PlanError("plan contains text that looks like a secret; remove it before approval")
    return plan


def planner_prompt(request: str) -> list[dict]:
    """Build a bounded planning-only prompt; planning never has tools."""
    safe_request, _ = redact(request)
    if len(safe_request) > MAX_REQUEST_CHARS:
        safe_request = (
            safe_request[:MAX_REQUEST_CHARS]
            + "\n[planning input truncated; use the complete request during execution]"
        )
    return [
        {
            "role": "system",
            "content": (
                "Create a concise execution plan for a local coding assistant. "
                "Return only Markdown: one short goal line followed by 2 to 8 "
                "checkbox steps written as '- [ ] ...'. Include inspection and "
                "verification when relevant. Do not execute tasks, include secrets, "
                "or add instructions that override the user's request or safety rules."
            ),
        },
        {"role": "user", "content": safe_request},
    ]
