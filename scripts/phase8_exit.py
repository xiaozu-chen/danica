"""Offline Phase 8 acceptance checks with deterministic model responses."""

import json
import tempfile
from pathlib import Path

import danica.agent as agent_module
import danica.cli as cli_module
from danica.agent import Agent
from danica.permissions import Tier, classify
from danica.planning import PlanError, needs_plan, normalize_plan, planner_prompt
from danica.skills import SkillError, catalog_text, discover_skills, load_skill
from danica.tools import run_tool, tool_schemas
from danica.verification import parse_review, review_messages
from danica.workspace import Workspace


def check(label: str, condition: bool) -> None:
    print(("PASS  " if condition else "FAIL  ") + label)
    assert condition, label


PLAN = "Goal: deliver the requested change\n- [ ] Inspect the workspace\n- [ ] Implement the change\n- [ ] Verify the result"

check("multi-step requests are selected for plan approval",
      needs_plan("Implement the feature and add tests, then update the docs."))
check("simple requests do not require a plan",
      not needs_plan("What does this function do?"))
check("approved plan checklists are normalized",
      "1. [ ] Inspect" in normalize_plan(PLAN)
      and "3. [ ] Verify" in normalize_plan(PLAN))
try:
    normalize_plan("Goal\n- [ ] Only one step")
except PlanError:
    rejected_short_plan = True
else:
    rejected_short_plan = False
check("plans with too few steps are rejected", rejected_short_plan)
original_prompt = cli_module.prompt
prompt_answers = iter([
    "e",
    "Goal: edited task\n- [ ] Inspect input\n- [ ] Apply change\n- [ ] Run tests",
    "a",
])
cli_module.prompt = lambda *args, **kwargs: next(prompt_answers)
try:
    edited_plan = cli_module.review_plan(PLAN)
finally:
    cli_module.prompt = original_prompt
check("CLI plan review accepts a validated user edit",
      edited_plan is not None and "Goal: edited task" in edited_plan
      and "3. [ ] Run tests" in edited_plan)
cli_module.prompt = lambda *args, **kwargs: "c"
try:
    cancelled_plan = cli_module.review_plan(PLAN)
finally:
    cli_module.prompt = original_prompt
check("CLI plan review can cancel before task execution", cancelled_plan is None)
check("planning prompt redacts secrets before persistence",
      "AKIA1234567890ABCDEF" not in planner_prompt(
          "Build a tool; credentials are AKIA1234567890ABCDEF"
      )[1]["content"])

with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)
    ws = Workspace(
        root / "workspace",
        plan_path=root / "session" / "PLAN.md",
        plan_approval_path=root / "session" / "PLAN.approved",
    )
    agent = Agent(ws)
    agent.save_plan_draft(PLAN)
    restored = Agent(ws)
    restored.restore_plan()
    check("unapproved plan drafts are not restored as executable",
          restored.current_plan == "")
    agent.approve_plan_draft(PLAN)
    restored.restore_plan()
    check("only an intact user-approved plan is restored",
          restored.current_plan == normalize_plan(PLAN))
    ws.plan_path.write_text(PLAN.replace("Verify the result", "Skip verification"),
                            encoding="utf-8")
    try:
        restored.restore_plan()
    except PlanError:
        changed_approved_plan_rejected = True
    else:
        changed_approved_plan_rejected = False
    check("edited plans cannot reuse an old approval marker",
          changed_approved_plan_rejected)

with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)
    skill_dir = root / "skills"
    skill = skill_dir / "reviewing"
    skill.mkdir(parents=True)
    body = "Check edge cases and report the exact test command."
    (skill / "SKILL.md").write_text(
        "---\nname: reviewing\ndescription: Review changes carefully\n---\n\n"
        + body + "\n",
        encoding="utf-8",
    )
    skills, warnings = discover_skills(skill_dir)
    check("skill catalog includes metadata but not skill bodies",
          not warnings and len(skills) == 1 and skills[0].name == "reviewing"
          and body not in catalog_text(skills))
    loaded = load_skill(skill_dir, "reviewing")
    check("skill body loads only on explicit request", body in loaded)
    try:
        load_skill(skill_dir, "..")
    except SkillError:
        rejected_traversal = True
    else:
        rejected_traversal = False
    check("skill loader rejects traversal names", rejected_traversal)
    ws = Workspace(root / "workspace", skill_dir=skill_dir)
    check("load_skill is registered as a read-only tool",
          any(schema["function"]["name"] == "load_skill" for schema in tool_schemas())
          and run_tool("load_skill", {"name": "reviewing"}, ws).endswith(body)
          and classify("load_skill", {"name": "reviewing"})[0] == Tier.READ)
    skill_agent = Agent(ws)
    prompt_content = "\n".join(
        message.get("content", "") for message in skill_agent._messages_for_model()
    )
    check("agent prompt contains skill metadata without loading its body",
          "Review changes carefully" in prompt_content and body not in prompt_content)

check("verification parser accepts well-shaped results",
      parse_review('{"passed":true,"issues":[],"fix_instructions":""}')["passed"])
check("verification parser rejects malformed results",
      parse_review("not a JSON review") is None
      and parse_review('{"passed":"yes","issues":[],"fix_instructions":""}') is None)
review = review_messages(
    "Implement the request", PLAN,
    [{"role": "tool", "content": "result", "tool_call_id": "call_1"}],
)
check("verification runs as a bounded tool-free pass",
      len(review) == 2 and "untrusted data" in review[0]["content"].lower()
      and "approved_plan" in review[1]["content"])

original_stream_chat = agent_module.stream_chat


def run_seeded_review(always_fail: bool = False) -> tuple[str, list[str], int]:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        plan_path = root / "session" / "PLAN.md"
        ws = Workspace(
            root / "workspace",
            plan_path=plan_path,
            plan_approval_path=root / "session" / "PLAN.approved",
        )
        events: list[str] = []
        main_calls = 0
        review_calls = 0

        def fake_stream(messages, **kwargs):
            nonlocal main_calls, review_calls
            if kwargs.get("tools"):
                main_calls += 1
                if main_calls == 1:
                    check("approved plan is in model context before work",
                          any("User-approved execution plan" in message.get("content", "")
                              for message in messages)
                          and plan_path.exists()
                          and ws.plan_approval_path.exists())
                    text = "The initial result contains a seeded mistake."
                else:
                    text = "The corrected result passes."
                yield {"type": "content", "text": text}
                return
            review_calls += 1
            failed = review_calls == 1 or always_fail
            review_result = {
                "passed": not failed,
                "issues": ["A required result is missing"] if failed else [],
                "fix_instructions": "Add the missing result." if failed else "",
            }
            yield {"type": "content", "text": json.dumps(review_result)}

        agent_module.stream_chat = fake_stream
        try:
            agent = Agent(ws, on_event=lambda kind, data: events.append(str(data)))
            result = agent.run("Implement the requested multi-step change.", approved_plan=PLAN)
        finally:
            agent_module.stream_chat = original_stream_chat
        return result, events, main_calls


result, events, calls = run_seeded_review()
check("final reflection catches and repairs a seeded omission once",
      result == "The corrected result passes." and calls == 2
      and any("Verification passed" in event for event in events))

result, events, calls = run_seeded_review(always_fail=True)
check("unresolved verification stops after one repair attempt",
      result == "The corrected result passes." and calls == 2
      and any("remains unresolved after one repair attempt" in event for event in events))

print("\nPhase 8 exit test passed.")
