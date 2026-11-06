"""Focused tests for global skills and final self-reflection."""

import json
import tempfile
from pathlib import Path

import danica.agent as agent_mod
import danica.tools.skills as skills_mod
from danica.agent import Agent
from danica.planner import Plan
from danica.permissions import Tier, classify
from danica.tools.registry import run_tool, tool_schemas
from danica.tools.skills import skill_body, skill_catalog, skills_prompt
from danica.workspace import Workspace


def check(label: str, condition: bool) -> None:
    print(("PASS  " if condition else "FAIL  ") + label)
    assert condition, label


with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
    root = Path(tmp) / "global"
    skill_dir = root / "python-tests"
    skill_dir.mkdir(parents=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        "---\nname: python-tests\ndescription: Run focused Python checks.\n---\n"
        "Use the smallest relevant test command.\n",
        encoding="utf-8",
    )
    (root / "bad").mkdir()
    (root / "bad" / "SKILL.md").write_text("Not frontmatter", encoding="utf-8")

    skills, errors = skill_catalog(root)
    check("catalog contains only valid metadata", len(skills) == 1
          and skills[0].name == "python-tests" and skills[0].description == "Run focused Python checks.")
    check("malformed global skills are reported", len(errors) == 1 and errors[0].startswith("bad:"))
    prompt, _ = skills_prompt(root)
    check("context catalog contains the description, not the body",
          "Run focused Python checks" in prompt and "smallest relevant test command" not in prompt)
    check("body loads by catalog name", "smallest relevant test command" in skill_body("python-tests", root))
    project = Path(tmp) / "project"
    project_skill = project / "SKILL.md"
    project.mkdir()
    project_skill.write_text("project-only instruction", encoding="utf-8")
    old_skills_dir = skills_mod.SKILLS_DIR
    skills_mod.SKILLS_DIR = root
    try:
        loaded = run_tool("load_skill", {"name": "python-tests"}, Workspace(project))
        denied = run_tool("load_skill", {"name": "project"}, Workspace(project))
    finally:
        skills_mod.SKILLS_DIR = old_skills_dir
    check("only skills from the configured global folder can be loaded",
          "smallest relevant test command" in loaded and denied.startswith("ERROR:"))
    check("the global skill loader is exposed as a tool",
          "load_skill" in {schema["function"]["name"] for schema in tool_schemas()})
    check("loading an approved global skill is a read-only permission",
          classify("load_skill", {"name": "python-tests"})[0] == Tier.READ)
    try:
        skill_body("../python-tests", root)
        path_rejected = False
    except ValueError:
        path_rejected = True
    check("model-supplied paths cannot escape the skill catalog", path_rejected)


real_stream = agent_mod.stream_chat
try:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        agent = Agent(Workspace(Path(tmp) / "ws"))
        plan = Plan("Complete the task", ["Do the requested work", "Verify the result"])
        calls: list[dict] = []
        model_answers = iter(["initial answer", "repaired answer"])
        reviews = iter([
            {"status": "fix", "issues": ["The result does not verify the requested behavior."]},
            {"status": "pass", "issues": []},
        ])

        def fake_stream(messages, **kwargs):
            calls.append(kwargs)
            if kwargs.get("tools") is None:
                yield {"type": "content", "text": json.dumps(next(reviews))}
            else:
                yield {"type": "content", "text": next(model_answers)}

        agent_mod.stream_chat = fake_stream
        answer = agent.run("Implement and verify it", plan=plan)
        repair_notes = [m["content"] for m in agent.messages if
                        "only automatic repair attempt" in str(m.get("content"))]
        check("a failed final check triggers exactly one repair round",
              answer == "repaired answer" and len(calls) == 4 and len(repair_notes) == 1)
        check("reflection requests no tools and has a small token budget",
              all(call.get("tools") is None and call.get("max_tokens") == 512
                  for call in (calls[1], calls[3])))

        calls.clear()
        model_answers = iter(["answer with a gap", "answer after one repair"])
        reviews = iter([
            {"status": "fix", "issues": ["A requirement is still missing."]},
            {"status": "fix", "issues": ["The requirement remains missing."]},
        ])
        answer = agent.run("Implement the remaining requirement", plan=plan)
        check("a second failed check is reported without a second repair",
              answer == "answer after one repair" and len(calls) == 4
              and sum("only automatic repair attempt" in str(m.get("content"))
                      for m in agent.messages) == 2)
finally:
    agent_mod.stream_chat = real_stream

print("\nAll good.")
