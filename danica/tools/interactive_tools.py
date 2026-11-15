"""
interactive_tools.py - Four tools:

  quiz        a Q&A page with a working Submit button (graded or survey)     -> quizzes/x.html
  flashcards  a flip-card study page                                         -> cards/x.html
  flowchart   a flow chart from shapes placed on a grid                      -> flows/x.png (+ interactive viewer)
  ask_user    ask the USER clarifying questions in a dialog (missing context, optional features, preferences)

Like the chart tool, the model writes DATA with write_file (a small JSON file); these tools check it
and build the output with fixed code. The model never writes HTML or JavaScript.
The success text starts with "OUTPUT: saved ..." (the same wording the other file-making tools use).
"""

import json

from danica.flowgen import FlowError, render as render_flow, validate as validate_flow
from danica.interactive import (InteractiveError, MAX_SPEC_BYTES, validate_cards, validate_quiz, write_page)
from danica.secret_scan import is_sensitive_path
from danica.tools.files import _write_guard
from danica.tools.registry import tool
from danica.workspace import Workspace


def _load(ws: Workspace, file: str):
    """(path, data) for a JSON spec file, or (None, "ERROR: ...")."""
    p = ws.resolve(file)
    if p.suffix.lower() != ".json":
        return None, "ERROR: the data file must end in .json (write it with write_file first)"
    if not p.is_file():
        return None, f"ERROR: '{file}' is not a file. Write the JSON data file with write_file first."
    if is_sensitive_path(ws.rel(p)):
        return None, "ERROR: that file is blocked: it looks like a credentials file"
    if p.stat().st_size > MAX_SPEC_BYTES:
        return None, f"ERROR: the data file is too big (limit {MAX_SPEC_BYTES // 1000} KB)"
    try:
        return p, json.loads(p.read_text(encoding="utf-8-sig"))
    except (ValueError, UnicodeError, RecursionError) as e:
        return None, f"ERROR: the data file is not valid JSON ({type(e).__name__}: {str(e)[:100]})"


def _done(shown: str, size: int, what: str) -> str:
    return (f'OUTPUT: saved {shown} ({max(1, size // 1024)} KB). It is finished: do NOT call this tool again for this '
            f'file. To show it in chat, write <present file="{shown}"/> on its own line in your reply, then add one '
            f'short sentence about {what} and stop.')


# ------------------------------------------------------------------ quiz

@tool(
    "quiz",
    "Make an interactive quiz page (questions, a Submit button, a score with explanations, and Copy/Download "
    "results) from a JSON file you wrote with write_file (for example quizzes/capitals.json). The page is saved "
    "next to it (quizzes/capitals.html). Data: "
    '{"title": "...", "questions": [{"q": "Capital of France?", "type": "choice", "options": ["Paris", "Rome"], '
    '"answer": 0, "explanation": "..."}, {"q": "Pick primes", "type": "multi", "options": [...], "answer": [0, 2]}, '
    '{"q": "The sun is a star", "type": "truefalse", "answer": true}, '
    '{"q": "Capital of Japan?", "type": "text", "answer": ["Tokyo"]}]}. '
    '"answer" is the option number (0 = first) or the option text. 1 to 30 questions, 2 to 6 options each. '
    'For a SURVEY (no right answers, just collect the user\'s answers) leave out "answer" and add "mode": "survey". '
    "Never write HTML or JavaScript yourself.",
    {"file": {"type": "string", "description": "Path of the JSON data file, for example quizzes/capitals.json"}},
)
def quiz(ws: Workspace, file: str) -> str:
    p, data = _load(ws, file)
    if p is None:
        return data
    out = p.with_suffix(".html")
    guarded = _write_guard(ws, out)
    if guarded:
        return guarded
    try:
        spec = validate_quiz(data)
        write_page(spec, out)
    except InteractiveError as e:
        return f"ERROR: {e}"
    return _done(ws.display(out), out.stat().st_size, "the quiz")


# ------------------------------------------------------------------ flash cards

@tool(
    "flashcards",
    "Make an interactive flash card page (flip, got it / again, shuffle, review missed) from a JSON file you wrote "
    "with write_file (for example cards/spanish.json). The page is saved next to it (cards/spanish.html). Data: "
    '{"title": "Spanish basics", "cards": [{"front": "hola", "back": "hello", "hint": "optional"}]}. '
    "1 to 100 cards. Never write HTML or JavaScript yourself.",
    {"file": {"type": "string", "description": "Path of the JSON data file, for example cards/spanish.json"}},
)
def flashcards(ws: Workspace, file: str) -> str:
    p, data = _load(ws, file)
    if p is None:
        return data
    out = p.with_suffix(".html")
    guarded = _write_guard(ws, out)
    if guarded:
        return guarded
    try:
        write_page(validate_cards(data), out)
    except InteractiveError as e:
        return f"ERROR: {e}"
    return _done(ws.display(out), out.stat().st_size, "the flash cards")


# ------------------------------------------------------------------ flow chart

@tool(
    "flowchart",
    "Draw a flow chart from shapes placed on a GRID. Write a JSON file with write_file (for example "
    "flows/login.json); the picture is saved next to it (flows/login.png) and opens as an interactive diagram. Data: "
    '{"type": "flow", "title": "Login", "nodes": [{"id": "start", "label": "Start", "shape": "oval", "col": 0, "row": 0}, '
    '{"id": "ok", "label": "Password OK?", "shape": "diamond", "col": 0, "row": 1}, '
    '{"id": "in", "label": "Show profile", "shape": "box", "col": 0, "row": 2, "color": "green"}, '
    '{"id": "no", "label": "Show error", "shape": "box", "col": 1, "row": 1, "color": "red"}], '
    '"edges": [{"from": "start", "to": "ok"}, {"from": "ok", "to": "in", "label": "yes"}, '
    '{"from": "ok", "to": "no", "label": "no"}]}. '
    "col counts from 0 on the left, row from 0 at the top; every node needs its own cell (max 12 x 12, 40 nodes); "
    "leave an empty cell where a connector has to go around a shape. "
    "Shapes: box, rounded, oval (start/end), diamond (decision), parallelogram (input/output), circle (connector). "
    "Colors (optional): blue, green, orange, red, pink, sky, yellow, grey. Never write drawing code yourself.",
    {"file": {"type": "string", "description": "Path of the JSON data file, for example flows/login.json"}},
)
def flowchart(ws: Workspace, file: str) -> str:
    p, data = _load(ws, file)
    if p is None:
        return data
    out = p.with_suffix(".png")
    guarded = _write_guard(ws, out)
    if guarded:
        return guarded
    try:
        png = render_flow(validate_flow(data))
    except FlowError as e:
        return f"ERROR: {e}"
    out.write_bytes(png)
    return _done(ws.display(out), len(png), "the flow chart")


# ------------------------------------------------------------------ ask the user

MAX_QUESTIONS, MIN_OPTIONS, MAX_OPTIONS = 4, 2, 4


def _clean_questions(raw):
    """(questions, error). Each question becomes {"question", "options": [{"label", "description"}], "multi"}."""
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_QUESTIONS:
        return None, f'"questions" must be a list of 1 to {MAX_QUESTIONS} questions'
    out = []
    for n, q in enumerate(raw, start=1):
        if not isinstance(q, dict) or not str(q.get("question", "")).strip():
            return None, f'question {n} needs a "question" text'
        options = q.get("options")
        if not isinstance(options, list) or not MIN_OPTIONS <= len(options) <= MAX_OPTIONS:
            return None, f'question {n} needs "options": {MIN_OPTIONS} to {MAX_OPTIONS} short choices (the user can also type their own answer)'
        clean = []
        for o in options:
            label, desc = (o.get("label"), o.get("description", "")) if isinstance(o, dict) else (o, "")
            if isinstance(label, (dict, list)) or not str(label or "").strip():
                return None, f"question {n} has an empty option"
            clean.append({"label": " ".join(str(label).split())[:80], "description": " ".join(str(desc or "").split())[:120]})
        out.append({"question": " ".join(str(q["question"]).split())[:220], "options": clean, "multi": bool(q.get("multi"))})
    return out, ""


@tool(
    "ask_user",
    "Ask the USER 1 to 4 short clarifying questions in a dialog and wait for the answers. Use it when (a) the request "
    "lacks context you cannot find with your tools (which file, who is the audience, what data), (b) an optional extra "
    "feature is worth offering, or (c) the result depends on a preference (style, format, length). Give each question "
    "2 to 4 options (the user can also type their own answer). Do not ask what a tool can find out, do not ask the "
    "same thing twice, and do not ask for permission to do what the user already asked. Ask once, then continue with "
    'the answers. Example: {"questions": [{"question": "Which chart style?", "options": ["Bar", "Line"]}, '
    '{"question": "Extras?", "options": ["Add a legend", "Add data labels"], "multi": true}]}.',
    {"questions": {"type": "array", "description": "1 to 4 questions",
                   "items": {"type": "object",
                             "properties": {"question": {"type": "string"},
                                            "options": {"type": "array", "items": {"type": "string"}},
                                            "multi": {"type": "boolean", "description": "true = the user may pick several options"}},
                             "required": ["question", "options"]}}},
)
def ask_user(ws: Workspace, questions) -> str:
    cleaned, problem = _clean_questions(questions)
    if cleaned is None:
        return f"ERROR: {problem}"
    ask = getattr(ws, "ask_user", None)
    if ask is None:                            # a subagent, or a session with no screen to ask on
        return ("ERROR: the user cannot be asked right now. Choose sensible defaults, say what you assumed "
                "in your answer, and continue.")
    answers = ask(cleaned)
    if not answers:
        return ("OUTPUT: the user skipped the questions. Choose sensible defaults, say in one line what you "
                "assumed, and continue. Do not ask again.")
    lines = []
    for i, a in enumerate(answers, start=1):
        picked = "; ".join(a.get("answers") or [])
        if a.get("other"):
            picked = (picked + "; " if picked else "") + f'(in their own words) {a["other"]}'
        lines.append(f'{i}. {a.get("question", "")} -> {picked or "(no answer: use your best judgement)"}')
    return "OUTPUT: the user answered:\n" + "\n".join(lines) + "\nUse these answers and continue with the task. Do not ask the same questions again."
