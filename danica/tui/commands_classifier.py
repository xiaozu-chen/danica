"""
commands_classifier.py - /classifier: are the model classifiers on, and do they answer?

    /classifier        status, then a quick test of both classifiers (a few seconds)

Imported at the bottom of commands.py, like commands_view.py and commands_vision.py.
"""

from danica import classifier, judge
from danica.config import CONFIG
from danica.thinking import ThinkingClassifier
from danica.tui.commands import DESCRIPTIONS, command

DESCRIPTIONS["/classifier"] = "model classifiers: status and test"

SAMPLES_JUDGE = ("Get-ChildItem", "pytest -q", "Remove-Item -Recurse -Force C:\\Users")
SAMPLES_THINK = ("hi there", "find why the parser crashes on empty input and fix it step by step")


@command("/classifier")
def _classifier(app, args):
    """/classifier   shows classifier status and quick tests."""
    routing_cfg = CONFIG.get("routing") or {}
    app.ui.log("info", f"routing classifier: {'ON' if routing_cfg.get('classifier_enabled') else 'OFF (agentic default)'}  "
                       f"server {routing_cfg.get('classifier_url', 'http://127.0.0.1:8080')}")
    ok, why = classifier.status(refresh=True)
    cfg = classifier.settings()
    n = classifier.slots()
    app.ui.log("info" if ok else "warning",
               f"classifier: {'ON' if ok else 'OFF (rules only)'}  server {classifier.base_url()}  "
               f"slots {n if n is not None else 'unknown'}  can approve safe project commands: "
               f"{'yes' if cfg['judge_can_approve'] else 'no'}" + ("" if ok else f"\n  why: {why}"))
    judge.clear_cache()
    lines = []
    if ok:
        for text in SAMPLES_THINK:
            d = ThinkingClassifier().classify(text)
            lines.append(f"  thinking  {('on ' if d.thinking else 'off')}  {d.source:<14} {classifier.LAST['ms']:>5} ms  {text[:50]}")
    else:
        app.ui.log("info", "Thinking/shell classifiers are unavailable or off; routing uses its independent endpoint and setting.")
    for command_text in SAMPLES_JUDGE:
        v = judge.judge_command(command_text, "normal")
        shown = f"{v.verdict:<14} {v.reason[:50]}" if v else "no answer"
        lines.append(f"  shell     {classifier.LAST['ms']:>5} ms  {command_text[:34]:<34} -> {shown}")
    if lines:
        app.ui.log("info", "tests:\n" + "\n".join(lines))
