"""
plan_test.py - Checks plan parsing, the "is it big?" score, plan files, read-only drafting
(with a fake model), running against an approved plan, and the CLI dialog. No model needed.
Run from the project root:  python -m scripts.plan_test
"""

import io
import tempfile
from pathlib import Path

from rich.console import Console

import danica.agent as agent_mod
import danica._cli as _cli
from danica.agent import PLAN_TOOLS, Agent
from danica.compaction import harvest
from danica.config import CONFIG
from danica.planner import (Plan, PlanError, PlanFormatError, looks_big, parse_plan, plan_note, planning_prompt,
                            reload_plan, render_plan, todo_text, write_plan_file)
from danica.tools.todo import counts, normalize, read_todo
from danica.workspace import Workspace


def check(label: str, condition: bool) -> None:
    print(("PASS  " if condition else "FAIL  ") + label)
    assert condition, label


def error_of(text: str) -> str:
    try:
        parse_plan(text)
        return ""
    except PlanFormatError as e:
        return str(e)


FAKE_AWS = "AKIA" + "IOSFODNN7EXAMPLE"   # split so this file stays clean
GOOD = "GOAL: Build the calc package\nSTEPS:\n1. Write add.py\n2. Write sub.py\n3. Run the tests\nNOTES:\n- assumes Python 3.11"

# --- parsing ---
plan = parse_plan(GOOD, "make calc")
check("the model's template parses", plan.goal == "Build the calc package" and len(plan.steps) == 3
      and plan.notes == ["assumes Python 3.11"] and plan.request == "make calc")
check("the Markdown file we write parses back to the same plan",
      (lambda p: (p.goal, p.steps, p.notes) == (plan.goal, plan.steps, plan.notes))(parse_plan(render_plan(plan))))
loose = parse_plan("**Goal:** Ship it\n\n## Steps\n\n1) First thing\n- [ ] Second thing that is\n  wrapped onto a line\n* [x] Third\n\n## Notes\nPlain note line")
check("bold goal, 1) numbering, checkboxes, wrapped steps and plain notes all parse",
      loose.goal == "Ship it" and loose.steps == ["First thing", "Second thing that is wrapped onto a line", "Third"]
      and loose.notes == ["Plain note line"])
check("invisible characters are removed", parse_plan("GOAL: a\u200bb\nSTEPS:\n1. x\n2. y").goal == "ab")
check("no goal", "GOAL" in error_of("STEPS:\n1. a\n2. b"))
check("no steps", "STEPS" in error_of("GOAL: g"))
check("one step is not a plan", "at least 2" in error_of("GOAL: g\nSTEPS:\n1. only one"))
check("too many steps", "at most 10" in error_of("GOAL: g\nSTEPS:\n" + "\n".join(f"{i}. s" for i in range(1, 12))))
check("a long step names its number", "step 2 is too long" in error_of("GOAL: g\nSTEPS:\n1. ok\n2. " + "x" * 161))
check("a secret in a step is refused", "secret" in error_of(f"GOAL: g\nSTEPS:\n1. use {FAKE_AWS}\n2. b"))
check("notes are capped at 8", len(parse_plan(GOOD + "".join(f"\n- n{i}" for i in range(12))).notes) == 8)
check("empty text", error_of("") != "")

# --- is it big? ---
BIG1 = ("Search the web for what pelicans eat, fetch two good pages, make a bar chart of three fish species, "
        "and then write a short report as a Word document that cites your sources and includes the chart.")
BIG2 = "Please do the following in the workspace:\n1. create a package\n2. add a README\n3. write tests\n4. run the tests"
for label, text in [("a research, chart and report request", BIG1), ("a numbered list of jobs", BIG2)]:
    big, why, score = looks_big(text)
    check(f"big: {label}", big and score >= 3 and why != "")
for label, text in [("a typo fix", "Fix the typo in README.md"),
                    ("one function with a test", "Add a function mul(a, b) to math_utils.py with a unittest, then run the tests."),
                    ("a refactor", "Refactor the storage module so that it uses pathlib instead of os.path, and keep the public functions the same."),
                    ("a question", "What does the compaction module do and how does it choose where to cut the history?"),
                    ("a slash command", "/plan " + BIG1)]:
    check(f"not big: {label}", not looks_big(text)[0])
check("the threshold can be changed", looks_big("Add a function mul(a, b) to math_utils.py with a unittest, then run the tests.", 0)[0])

# --- builders ---
check("the planning prompt carries the request, the old plan and the feedback",
      all(s in planning_prompt("do X", "no pandas", plan) for s in ("PLAN MODE", "do X", "no pandas", "Write add.py")))
check("todo_text is exactly the TODO.md format", normalize(todo_text(plan)) == todo_text(plan) and counts(todo_text(plan)) == (3, 0))
check("the plan note is marked as not from the user and has the goal",
      plan_note(plan).startswith("[harness reminder, not from the user]") and "Build the calc package" in plan_note(plan))
check("steps are in the note only when there is no TODO file", "Write add.py" not in plan_note(plan) and "Write add.py" in plan_note(plan, True))

with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
    tmp = Path(tmp)

    # --- plan files ---
    p1 = write_plan_file(tmp / "plans", plan)
    p2 = write_plan_file(tmp / "plans", plan)
    check("plan files are numbered", (p1.name, p2.name) == ("001.md", "002.md") and not list((tmp / "plans").glob("*.tmp")))
    plan.path = p1
    p1.write_text(render_plan(plan).replace("Write sub.py", "Write sub.py and mul.py"), encoding="utf-8")
    check("an edited file reloads", "Write sub.py and mul.py" in reload_plan(plan).steps and reload_plan(plan).path == p1)
    p1.write_text("nothing useful", encoding="utf-8")
    try:
        reload_plan(plan)
        bad_edit = False
    except PlanFormatError:
        bad_edit = True
    check("a broken edit is reported", bad_edit)
    p1.write_text(render_plan(plan), encoding="utf-8")

    # --- drafting with a fake model ---
    ws = Workspace(tmp / "ws", todo_path=tmp / "TODO.md")
    (ws.root / "a.txt").write_text("hello\n", encoding="utf-8")
    agent = Agent(ws)
    agent.messages += [{"role": "user", "content": "earlier question"}, {"role": "assistant", "content": "earlier answer"},
                       {"role": "assistant", "content": "", "tool_calls": [{"id": "x", "type": "function",
                                                                          "function": {"name": "read_file", "arguments": "{}"}}]},
                       {"role": "tool", "tool_call_id": "x", "content": "OLD TOOL OUTPUT"}]
    before = len(agent.messages)
    queue: list[dict] = []
    seen: list[tuple] = []

    def fake_stream(messages, **kwargs):
        seen.append(([dict(message) for message in messages], kwargs))
        item = queue.pop(0)
        yield item if item["type"] != "content" else {"type": "content", "text": item["text"]}

    def tool_call(name: str, **args):
        import json
        return {"type": "tool_calls", "calls": [{"id": f"c{len(seen)}", "name": name, "arguments": json.dumps(args)}]}

    ran: list[str] = []
    real_run, real_stream, real_think = agent_mod.run_tool, agent_mod.stream_chat, CONFIG["planning"]["thinking"]
    agent_mod.stream_chat = fake_stream
    agent_mod.run_tool = lambda name, args, w: (ran.append(name), real_run(name, args, w))[1]
    CONFIG["planning"]["thinking"] = False
    try:
        queue[:] = [tool_call("read_file", path="a.txt"), {"type": "content", "text": GOOD}]
        drafted = agent.draft_plan("make calc")
        first_messages, first_kwargs = seen[0]
        check("a plan comes back after a read-only look", drafted.goal == "Build the calc package" and ran == ["read_file"])
        check("only the five read-only tools are offered",
              {s["function"]["name"] for s in first_kwargs["tools"]} == set(PLAN_TOOLS))
        check("the thinking setting is passed through", first_kwargs["thinking"] is False)
        check("it sees earlier plain-text messages but no old tool output",
              any(m.get("content") == "earlier answer" for m in first_messages)
              and not any("OLD TOOL OUTPUT" in str(m.get("content")) for m in first_messages))
        check("the planning instruction comes last", "PLAN MODE" in first_messages[-1]["content"] and "make calc" in first_messages[-1]["content"])
        check("the tool result reaches the next model call", any("hello" in str(m.get("content")) for m in seen[1][0] if m["role"] == "tool"))
        check("drafting does not change the real conversation", len(agent.messages) == before)

        seen.clear()
        ran.clear()
        queue[:] = [tool_call("run_shell", command="echo hi"), {"type": "content", "text": GOOD}]
        agent.draft_plan("make calc")
        check("a shell call while planning is refused and never runs", ran == []
              and any("not available while planning" in str(m.get("content")) for m in seen[1][0] if m["role"] == "tool"))

        seen.clear()
        queue[:] = [{"type": "content", "text": "Sure, I will just do it."}, {"type": "content", "text": GOOD}]
        agent.draft_plan("make calc")
        check("a badly formatted reply gets one retry with the reason",
              len(seen) == 2 and "was rejected" in seen[1][0][-1]["content"])
        queue[:] = [{"type": "content", "text": "nope"}, {"type": "content", "text": "still nope"}]
        try:
            agent.draft_plan("make calc")
            failed = ""
        except PlanError as e:
            failed = str(e)
        check("two bad replies in a row fail clearly", "plan format" in failed)
        queue[:] = [tool_call("read_file", path="a.txt")] * 10
        try:
            agent.draft_plan("make calc")
            failed = ""
        except PlanError as e:
            failed = str(e)
        check("endless looking-around is stopped", "too many steps" in failed)

        # --- running against an approved plan ---
        agent2 = Agent(Workspace(tmp / "ws2", todo_path=tmp / "TODO2.md"))
        agent2.plans_dir = tmp / "plans2"
        agent2.save_plan(plan)
        check("save_plan writes a file and remembers the plan", plan.path.exists() and agent2.last_plan is plan)
        agent2.install_plan(plan)
        check("an approved plan becomes TODO.md with open items", counts(read_todo(agent2.ws)) == (3, 0))
        seen.clear()
        agent_mod.stream_chat = lambda messages, **kw: iter([{"type": "content", "text": "ok"}])
        agent2.run("make calc", plan=plan)
        notes = [m["content"] for m in agent2.messages if str(m.get("content")).startswith("[harness reminder")]
        check("the request runs with a harness note saying the plan was approved", any("approved this plan" in n for n in notes))
        check("the note is not counted as something the user said", harvest(agent2.messages)[0] == ["make calc"])
        check("the plan's TODO survives into the run", counts(read_todo(agent2.ws))[0] == 3)
    finally:
        agent_mod.run_tool, agent_mod.stream_chat, CONFIG["planning"]["thinking"] = real_run, real_stream, real_think

    # --- the CLI dialog ---
    class Audit:
        def __init__(self):
            self.events: list[tuple] = []

        def append(self, event, **data):
            self.events.append((event, data))

    answers: list = []

    def scripted(message=""):
        a = answers.pop(0)
        if isinstance(a, BaseException):
            raise a
        return a

    editor_content: list[str] = []

    def fake_editor(path):
        Path(path).write_text(editor_content.pop(0), encoding="utf-8")

    real_cli = (_cli.prompt, _cli.console, _cli.open_editor)
    buf = io.StringIO()
    _cli.prompt, _cli.console, _cli.open_editor = scripted, Console(file=buf, width=100, force_terminal=False), fake_editor
    try:
        def make_agent(name: str) -> Agent:
            a = Agent(Workspace(tmp / name, todo_path=tmp / f"{name}.todo.md"))
            a.plans_dir = tmp / f"{name}.plans"
            return a

        plan_a = Plan("Goal A", ["Do one", "Do two", "Verify"], [], "req")
        plan_b = Plan("Goal B", ["Only local", "Verify it"], [], "req")

        # approve (Enter and nonsense just ask again)
        ag, au = make_agent("w1"), Audit()
        ag.draft_plan = lambda request, feedback="", previous=None: Plan("Goal A", ["Do one", "Do two", "Verify"], [], request)
        answers[:] = ["", "maybe", "y"]
        decision, got = _cli.plan_flow("make calc", ag, au)
        check("only an explicit y approves", decision == "run" and got.goal == "Goal A" and not answers)
        check("approval installs the TODO", counts(read_todo(ag.ws)) == (3, 0))
        check("the audit log records counts only", [e for e, _ in au.events] == ["plan", "plan"]
              and all("Do one" not in str(d) for _, d in au.events))

        # cancel, Ctrl+C and Ctrl+D at the prompt
        for ans in (["n"], [KeyboardInterrupt()], [EOFError()]):
            ag, au = make_agent("w2"), Audit()
            ag.draft_plan = lambda request, feedback="", previous=None: Plan("Goal A", ["Do one", "Do two"], [], request)
            answers[:] = ans
            check(f"cancel via {type(ans[0]).__name__ if not isinstance(ans[0], str) else 'n'}",
                  _cli.plan_flow("x", ag, au) == ("cancel", None) and read_todo(ag.ws) == "")

        # edit: a broken save first, then a good one
        ag, au = make_agent("w3"), Audit()
        ag.draft_plan = lambda request, feedback="", previous=None: Plan("Goal A", ["Do one", "Do two", "Verify"], [], request)
        edited = Plan("Goal A", ["Do one", "Do two", "Update the docs", "Verify"], [], "x")
        editor_content[:] = ["garbage with no goal", render_plan(edited)]
        answers[:] = ["e", "", "e", "", "y"]
        decision, got = _cli.plan_flow("x", ag, au)
        check("a broken edit is reported and the last valid plan stays", "Problem with the edited plan" in buf.getvalue())
        check("a good edit is used", decision == "run" and got.steps[2] == "Update the docs" and counts(read_todo(ag.ws)) == (4, 0))

        # replan with feedback
        ag, au = make_agent("w4"), Audit()
        calls: list[tuple] = []

        def drafter(request, feedback="", previous=None):
            calls.append((feedback, previous))
            return plan_a if len(calls) == 1 else plan_b

        ag.draft_plan = drafter
        answers[:] = ["r", "use only local files", "y"]
        decision, got = _cli.plan_flow("x", ag, au)
        check("replanning passes the feedback and the previous plan", calls[1] == ("use only local files", plan_a))
        check("the replanned plan is the one approved", got.goal == "Goal B")

        # the draft limit
        ag, au = make_agent("w5"), Audit()
        calls.clear()
        ag.draft_plan = drafter
        answers[:] = ["r", "a", "r", "b", "r", "n"]
        result = _cli.plan_flow("x", ag, au)
        check("at most 3 drafts per request", len(calls) == 3 and result == ("cancel", None) and "3 drafts" in buf.getvalue())

        # drafting fails
        def broken(request, feedback="", previous=None):
            raise PlanError("the model did not follow the plan format (x)")

        ag, au = make_agent("w6"), Audit()
        ag.draft_plan = broken
        answers[:] = ["y"]
        check("a failed draft can run without a plan", _cli.plan_flow("x", ag, au) == ("run", None))
        answers[:] = [""]
        check("...or be cancelled (Enter cancels)", _cli.plan_flow("x", ag, au) == ("cancel", None))

        def interrupted(request, feedback="", previous=None):
            raise KeyboardInterrupt()

        ag.draft_plan = interrupted
        check("Ctrl+C while drafting cancels", _cli.plan_flow("x", ag, au) == ("cancel", None))

        # --- the hybrid decision ---
        flow_calls: list[str] = []
        asked: list[str] = []
        reply = ["yes"]
        real_flow, real_ask = _cli.plan_flow, _cli.ask_plan_first
        _cli.plan_flow = lambda text, agent, audit: (flow_calls.append(text), ("run", plan_a))[1]
        _cli.ask_plan_first = lambda why: (asked.append(why), reply[0])[1]
        try:
            ag, au = make_agent("w7"), Audit()

            def decide(text, forced=False, auto="ask"):
                flow_calls.clear()
                asked.clear()
                return _cli.run_plan_mode(text, forced, auto, ag, au)

            check("a big request is offered a plan, and yes plans it", decide(BIG1)[:2] == ("run", plan_a)
                  and len(asked) == 1 and flow_calls == [BIG1])
            check("the offer says why", "deliverables" in asked[0] or "actions" in asked[0])
            check("the offer is logged with its score, not its text",
                  au.events[-1][0] == "plan_offer" and au.events[-1][1]["score"] >= 3 and BIG1 not in str(au.events[-1]))
            reply[0] = "no"
            check("no means run it without a plan", decide(BIG1) == ("run", None, "ask") and flow_calls == [])
            reply[0] = "never"
            result = decide(BIG1)
            check("never switches the offers off for the session", result == ("run", None, "off") and flow_calls == [])
            reply[0] = "yes"
            check("a small request is never offered a plan", decide("Fix the typo in README.md")[1] is None and asked == [])
            check("/plan forces a plan without asking", decide("Fix the typo", forced=True)[1] is plan_a and asked == [] and len(flow_calls) == 1)
            check("with offers off, big requests are not offered", decide(BIG1, auto="off")[1] is None and asked == [])
            ag.ws.todo_path.write_text("Goal: g\n1. [ ] open item\n", encoding="utf-8")
            check("an unfinished TODO means no interruption", decide(BIG1)[1] is None and asked == [])
            ag.ws.todo_path.unlink()
            _cli.plan_flow = lambda text, agent, audit: ("cancel", None)
            check("a cancelled plan cancels the request", decide(BIG1, forced=True)[:2] == ("cancel", None))
        finally:
            _cli.plan_flow, _cli.ask_plan_first = real_flow, real_ask
    finally:
        _cli.prompt, _cli.console, _cli.open_editor = real_cli

print("\nAll good.")