# Phase 8: Planning, skills, and verification

Phase 8 adds three harness features for larger tasks: user-approved execution
plans, on-demand local skills, and a final review pass. Plans are written
outside the project workspace in the session directory; skill files are read
only from Danica's configured local skills folder.

## What changed

- Multi-step requests are identified using inexpensive request-shape signals:
  multiple action words, multiple list items or sentences, or a request of at
  least 320 characters. Other requests go directly to the agent.
- A planning-only model call drafts a short checklist without tools. The CLI
  saves and displays the draft, then lets the user approve, edit, or cancel.
  Edited plans are normalized and checked for size and secret-like content.
- An approved `PLAN.md` is injected as a harness reminder on each model call.
  It is saved outside the project workspace so a resumed interrupted session
  can restore it. A separate digest marker distinguishes approved plans from
  drafts and invalidates approval if the file changes. A completed plan is
  removed.
- `%APPDATA%/Danica/skills/<name>/SKILL.md` folders are discovered by their
  `name` and `description` frontmatter. Only metadata is added to the prompt;
  the model must call `load_skill` to retrieve a skill body. Skills are size
  limited, path checked, and rejected if they contain secret-like text.
- After a planned task, a tool-free final review compares the original request,
  approved plan, and a bounded slice of recent evidence. If it finds a concrete
  omission, Danica makes at most one focused repair attempt and checks again.
  Invalid or unavailable verifier output is reported as a warning, never
  treated as a successful verification.
- `/skills` lists available skill metadata. `load_skill` is read-only and
  follows the regular tool audit path.

## Skill file format

Create one folder per skill under `%APPDATA%/Danica/skills`:

```text
skills/
  reviewing/
    SKILL.md
```

Each `SKILL.md` starts with simple YAML frontmatter whose `name` matches the
folder and whose `description` fits on one line:

```markdown
---
name: reviewing
description: Review code changes and report actionable findings
---

Inspect the diff, run focused tests, and report only concrete issues.
```

Only the name and description are loaded at startup. The skill body is returned
as local guidance only when requested; it cannot override safety rules,
permissions, or user approval. Up to 64 skills are listed; each file is limited
to 64 KiB and each description to 300 characters.

## Limits

The large-task detector is intentionally inexpensive and heuristic, so it can
miss a complex request phrased as a short sentence. Users can make such a
request explicit by listing its steps. A model-generated review is a useful
quality check, not proof of correctness: inspect important outputs and test
them independently. There is only one automatic repair attempt.

## Verification

Run the offline Phase 8 acceptance test:

```powershell
.\.venv\Scripts\python.exe -m scripts.phase8_exit
```

It checks plan detection and validation, metadata-only skill discovery,
on-demand loading, and seeded verification outcomes including the single-repair
limit. It uses deterministic mock model responses and does not require a live
chat or embedding server.
