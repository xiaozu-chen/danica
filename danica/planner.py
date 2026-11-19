"""
planner.py - Plan mode: asking for a plan, checking it, saving it, and deciding when to offer one.

A plan is GOAL + 2-10 short STEPS + optional NOTES. The model drafts it in a fixed template; the
harness checks it (parse_plan), writes it as a Markdown file the user can edit, and on approval
turns it into TODO.md so the existing plan tracking executes it (todo_text).

HONEST LIMITS: a plan is a promise, not a lock (the model can still deviate); looks_big is a
keyword score and will sometimes guess wrong.
"""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from danica.markdown_doc import strip_invisible
from danica.secret_scan import find_secrets

MIN_STEPS, MAX_STEPS = 2, 10
MAX_STEP_CHARS, MAX_GOAL_CHARS, MAX_NOTE_CHARS, MAX_NOTES = 160, 200, 200, 8
MAX_PLAN_FILE = 30_000


class PlanFormatError(Exception):
    """The plan text is not in the expected format. The message says what to fix."""


class PlanError(Exception):
    """A plan could not be produced. The message is safe to show the user."""


@dataclass
class Plan:
    """Store a proposed sequence of steps for completing a request."""

    goal: str
    steps: list[str]
    notes: list[str] = field(default_factory=list)
    request: str = ""
    path: Path | None = None       # the plan file (None when there is no plans folder)


def _one_line(text: str) -> str:
    return " ".join(strip_invisible(text).split())


# ------------------------------------------------------------------- parsing

GOAL_RX = re.compile(r"^\W*goal\W*:\s*(.+)$", re.IGNORECASE)
STEPS_RX = re.compile(r"^\W*steps\W*$", re.IGNORECASE)
NOTES_RX = re.compile(r"^\W*(?:notes|questions|assumptions)\W*$", re.IGNORECASE)
ITEM_RX = re.compile(r"^\s*(?:\d{1,2}[.)]|[-*\u2022])\s*(?:\[[ xX]\]\s*)?(.+?)\s*$")


def parse_plan(text: str, request: str = "") -> Plan:
    """Read a plan from the model's template OR from the Markdown file we write (so an edited
    file parses too). Raises PlanFormatError saying what to fix."""
    goal, steps, notes, section = "", [], [], None
    for raw in strip_invisible(text).split("\n"):
        line = raw.strip()
        if not line:
            continue
        m = GOAL_RX.match(line)
        if m and not goal:
            goal, section = m.group(1).strip("*_ "), "goal"
            continue
        if STEPS_RX.match(line):
            section = "steps"
            continue
        if NOTES_RX.match(line):
            section = "notes"
            continue
        item = ITEM_RX.match(raw)
        if section == "steps":
            if item:
                steps.append(item.group(1))
            elif steps:
                steps[-1] += " " + line                  # a wrapped step continues on the next line
        elif section == "notes":
            notes.append(item.group(1) if item else line)

    goal = _one_line(goal)
    if not goal:
        raise PlanFormatError("there is no 'GOAL:' line")
    if len(goal) > MAX_GOAL_CHARS:
        raise PlanFormatError(f"the goal is too long ({len(goal)} characters; the limit is {MAX_GOAL_CHARS})")
    steps = [s for s in (_one_line(s) for s in steps) if s]
    if not steps:
        raise PlanFormatError("there is no 'STEPS:' list (numbered lines like '1. Write the module')")
    if len(steps) < MIN_STEPS:
        raise PlanFormatError(f"a plan needs at least {MIN_STEPS} steps")
    if len(steps) > MAX_STEPS:
        raise PlanFormatError(f"a plan has at most {MAX_STEPS} steps (this one has {len(steps)}): merge some")
    for i, s in enumerate(steps, start=1):
        if len(s) > MAX_STEP_CHARS:
            raise PlanFormatError(f"step {i} is too long ({len(s)} characters; the limit is {MAX_STEP_CHARS})")
    notes = [n[:MAX_NOTE_CHARS] for n in (_one_line(n) for n in notes) if n][:MAX_NOTES]
    if find_secrets("\n".join([goal] + steps + notes)):
        raise PlanFormatError("the plan contains what looks like a secret (key, token or password)")
    return Plan(goal=goal, steps=steps, notes=notes, request=_one_line(request)[:300])


# --------------------------------------------------------------- text builders

PLAN_INSTRUCTIONS = """PLAN MODE. Do NOT do the task now: write a plan for the user to approve.
You may look around first with read_file, list_dir, glob, grep and recall. Nothing else is available and nothing will be changed.
Then reply with EXACTLY this format and nothing else:

GOAL: <one sentence: what will exist when this is done>
STEPS:
1. <short imperative step>
2. <short imperative step>
NOTES:
- <an assumption you are making, or a question for the user>

Rules: 3 to 8 steps. Each step is short and checkable (what gets made or verified). The last step verifies the result (run the tests, open the file, check the numbers). Use the user's own words for names and files. Do not invent requirements. NOTES is optional (at most 4)."""


def render_steps(plan: Plan) -> str:
    """Format a plan goal and its steps for display."""
    return f"GOAL: {plan.goal}\nSTEPS:\n" + "\n".join(f"{i}. {s}" for i, s in enumerate(plan.steps, start=1))


def planning_prompt(request: str, feedback: str = "", previous: Plan | None = None) -> str:
    """The instruction sent to the model for one drafting round."""
    parts = [PLAN_INSTRUCTIONS, "", "USER REQUEST:", request.strip()]
    if previous is not None:
        parts += ["", "THE PREVIOUS PLAN (the user did not accept it as it was):", render_steps(previous)]
    if feedback:
        parts += ["", "THE USER'S FEEDBACK ON IT (follow it):", feedback.strip()]
    return "\n".join(parts)


def render_plan(plan: Plan) -> str:
    """The plan as the Markdown file the user can read and edit."""
    lines = ["# Plan", ""]
    if plan.request:
        lines += [f"**Request:** {plan.request}", ""]
    lines += [f"**Goal:** {plan.goal}", "", "## Steps", ""]
    lines += [f"{i}. {s}" for i, s in enumerate(plan.steps, start=1)]
    if plan.notes:
        lines += ["", "## Notes", ""] + [f"- {n}" for n in plan.notes]
    return "\n".join(lines) + "\n"


def todo_text(plan: Plan) -> str:
    """The plan as TODO.md in the exact format tools/todo.py uses (numbered, open items)."""
    return f"Goal: {plan.goal}\n\n" + "\n".join(f"{i}. [ ] {s}" for i, s in enumerate(plan.steps, start=1)) + "\n"


def plan_note(plan: Plan, include_steps: bool = False) -> str:
    """The harness note that goes into the conversation after the user's request."""
    lines = ["[harness reminder, not from the user] The user approved this plan for the request above. "
             "It is now your TODO.md (shown to you every turn): follow the steps in order, tick each one "
             "with the todo tool when it is really done, and carry out the final verification step for real. "
             "If you must change the plan, say so and why in your answer.",
             f"GOAL: {plan.goal}"]
    if include_steps:
        lines += [f"{i}. {s}" for i, s in enumerate(plan.steps, start=1)]
    if plan.notes:
        lines += ["Plan notes: " + " | ".join(plan.notes[:3])]
    return "\n".join(lines)


# --------------------------------------------------------------------- files

def write_plan_file(plans_dir: Path, plan: Plan) -> Path:
    """Save the plan as plans/NNN.md (a new number each time). Atomic: a crash leaves no half file."""
    plans_dir = Path(plans_dir)
    plans_dir.mkdir(parents=True, exist_ok=True)
    n = len(list(plans_dir.glob("*.md"))) + 1
    while (plans_dir / f"{n:03d}.md").exists():
        n += 1
    path = plans_dir / f"{n:03d}.md"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(render_plan(plan), encoding="utf-8")
    os.replace(tmp, path)
    return path


def reload_plan(plan: Plan) -> Plan:
    """Read the plan file again after the user edited it. Raises PlanFormatError (with advice)."""
    if plan.path is None:
        raise PlanFormatError("this plan has no file to reload")
    try:
        if plan.path.stat().st_size > MAX_PLAN_FILE:
            raise PlanFormatError("the plan file is too big")
        text = plan.path.read_text(encoding="utf-8-sig")
    except OSError as e:
        raise PlanFormatError(f"could not read the plan file ({e})")
    fresh = parse_plan(text, plan.request)
    fresh.path = plan.path
    return fresh


# --------------------------------------------------------- "is this request big?"

ACTION_WORDS = set("""create build write add make implement fix refactor search fetch research draw chart test run deploy
install convert migrate rewrite analyze analyse summarize summarise compare design generate update remove rename document
publish export import configure set setup clean download plot calculate collect""".split())
SEQUENCE_RX = re.compile(r"\b(then|after that|afterwards|finally|lastly|once that|first|second|third)\b", re.IGNORECASE)
LIST_LINE_RX = re.compile(r"(?m)^\s*(?:\d{1,2}[.)]|[-*])\s+\S")
DELIVERABLE_RX = re.compile(
    r"\b(reports?|decks?|slides?|slideshow|charts?|dashboards?|documents?|docx|pdf|apps?|packages?|modules?|library|"
    r"scripts?|tests?|readme|website|api|cli|diagrams?|spreadsheets?|presentations?)\b", re.IGNORECASE)


def looks_big(text: str, min_score: int = 3) -> tuple[bool, str, int]:
    """Does this request look like a multi-step task worth planning? Returns (big, why, score).
    Five signals, one point each; a keyword score, not understanding."""
    t = text.strip()
    if not t or t.startswith("/") or (len(t) < 80 and min_score > 0):
        return False, "", 0
    reasons = []
    if len(t) >= 240:
        reasons.append("it is long")
    if len(set(re.findall(r"[a-z]+", t.lower())) & ACTION_WORDS) >= 3:
        reasons.append("it asks for several different actions")
    if len(SEQUENCE_RX.findall(t)) >= 2 or len(LIST_LINE_RX.findall(t)) >= 3:
        reasons.append("it describes steps in order")
    if len({m.lower() for m in DELIVERABLE_RX.findall(t)}) >= 2:
        reasons.append("it asks for several deliverables")
    if t.count(",") + len(re.findall(r"\band\b", t, re.IGNORECASE)) >= 4:
        reasons.append("it lists many parts")
    return len(reasons) >= min_score, ", ".join(reasons[:3]), len(reasons)
