"""
plan_flow.py - The plan dialog, with no terminal code in it.

Ported from cli.py (show_plan, plan_flow, ask_plan_first, run_plan_mode). All questions go
through the `ui` object (ui.choose / ui.ask_text / ui.confirm / ui.log), so it works under the
Textual TUI and the classic CLI. Everything here BLOCKS on the user, so call it from a worker
thread when the frontend owns the terminal.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from danica.config import CONFIG
from danica.planner import PlanError, PlanFormatError, looks_big, reload_plan
from danica.tools.todo import open_items, read_todo

MAX_DRAFTS = 3          # plans drafted for one request (the first, plus replans)


def open_editor(path: Path) -> None:
    """Open a file in Notepad (a fixed, predictable editor). Tests replace this."""
    subprocess.Popen(["notepad.exe", str(path)])


def plan_text(plan) -> str:
    lines = [f"Goal: {plan.goal}", ""]
    for i, step in enumerate(plan.steps, start=1):
        lines.append(f"  {i}. {step}")
    if plan.notes:
        lines += ["", "Notes:"] + [f"  - {note}" for note in plan.notes]
    if plan.path:
        lines += ["", f"(file: {plan.path})"]
    return "\n".join(lines)


def plan_flow(request: str, agent, audit, ui, edit=open_editor) -> tuple[str, object]:
    """Draft a plan (read-only), show it, and let the user approve, edit, replan or cancel.
    Returns ("run", plan) to run the request with the approved plan, ("run", None) to run it
    without one (drafting failed and the user said go ahead), or ("cancel", None)."""
    plan, feedback, drafts = None, "", 0
    while True:
        drafts += 1
        ui.log("warning", "[plan] drafting a plan (read-only: nothing is changed)...")
        try:
            plan = agent.draft_plan(request, feedback, plan)
        except KeyboardInterrupt:
            ui.log("warning", "(planning stopped)")
            return "cancel", None
        except Exception as e:                        # PlanError, or llama-server not answering
            why = str(e) if isinstance(e, PlanError) else type(e).__name__
            ui.log("error", f"Could not make a plan: {why}")
            go = ui.confirm("Run the request without a plan?", yes="Run it", no="Cancel")
            return ("run", None) if go else ("cancel", None)

        agent.save_plan(plan)
        audit.append("plan", action="proposed", steps=len(plan.steps), draft=drafts)
        while True:
            answer = ui.choose(
                f"Plan ({len(plan.steps)} steps)", plan_text(plan),
                [("y", "Approve"), ("e", "Edit"), ("r", "Replan with feedback"), ("n", "Cancel")])
            if answer == "y":                          # only an explicit yes approves
                agent.install_plan(plan)
                audit.append("plan", action="approved", steps=len(plan.steps))
                ui.log("ok", f"Plan approved: {len(plan.steps)} steps. It is now your TODO (see /todo).")
                return "run", plan
            if answer in (None, "n"):                  # Esc counts as cancel
                audit.append("plan", action="cancelled")
                return "cancel", None
            if answer == "e":
                if plan.path is None:
                    ui.log("warning", "this plan has no file to edit")
                    continue
                try:
                    edit(plan.path)
                    again = ui.choose("Edit the plan file",
                                      f"Edit and save this file, then reload it:\n{plan.path}",
                                      [("r", "Reload"), ("b", "Keep the current plan")])
                    if again == "r":
                        plan = reload_plan(plan)
                        audit.append("plan", action="edited", steps=len(plan.steps))
                except PlanFormatError as e:
                    ui.log("error", f"Problem with the edited plan: {e} (the plan shown is the last "
                                    f"valid version; choose Edit to fix the file)")
                except OSError as e:
                    ui.log("error", f"could not open the editor ({e}); edit this file yourself: {plan.path}")
                continue
            if answer == "r":
                if drafts >= MAX_DRAFTS:
                    ui.log("warning", f"That is {MAX_DRAFTS} drafts: edit this one, approve it or cancel.")
                    continue
                feedback = (ui.ask_text("What should change?") or "").strip()
                if feedback:
                    audit.append("plan", action="replan")
                    break                              # draft again with the feedback


def ask_plan_first(why: str, ui) -> str:
    """The offer for a big-looking request: "yes", "no" or "never" (for this session)."""
    answer = ui.choose(f"This looks like a multi-step task ({why})", "Draft a plan first?",
                       [("y", "Draft a plan"), ("n", "Just do it"), ("x", "Don't ask again this session")])
    if answer in ("y",):
        return "yes"
    return "never" if answer == "x" else "no"


def run_plan_mode(text: str, forced: bool, plan_auto: str, agent, audit, ui) -> tuple[str, object, str]:
    """Decide whether this request gets a plan, and if so run the dialog.
    forced = the user asked (/plan). Returns (decision, plan, plan_auto): decision is "run" or
    "cancel", plan is None when the request runs without one, plan_auto may become "off"."""
    if not forced and plan_auto == "ask":
        big, why, score = looks_big(text, CONFIG["planning"]["min_score"])
        # An unfinished TODO means the user is continuing a plan: don't interrupt with an offer
        if big and not open_items(read_todo(agent.ws)):
            answer = ask_plan_first(why, ui)
            audit.append("plan_offer", score=score, answer=answer)      # the text itself is never logged
            if answer == "never":
                plan_auto = "off"
            forced = answer == "yes"
    if not forced:
        return "run", None, plan_auto
    decision, plan = plan_flow(text, agent, audit, ui)
    return decision, plan, plan_auto