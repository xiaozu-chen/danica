"""
commands_classifier.py - /classifier: are the model classifiers on, and do they answer?

    /classifier        status, then a quick test of both classifiers (a few seconds)

Imported at the bottom of commands.py, like commands_view.py and commands_vision.py.
"""

from danica import classifier, judge
from danica.thinking import ThinkingClassifier
from danica.tui.commands import DESCRIPTIONS, command

DESCRIPTIONS["/classifier"] = "model classifiers: status and test"

SAMPLES_JUDGE = ("Get-ChildItem", "pytest -q", "Remove-Item -Recurse -Force C:\\Users")
SAMPLES_THINK = ("hi there", "find why the parser crashes on empty input and fix it step by step")


@command("/classifier")
def _classifier(app, args):
    """/classifier   shows slots, status and two quick tests."""
    ok, why = classifier.status(refresh=True)
    cfg = classifier.settings()
    n = classifier.slots()
    app.ui.log("info" if ok else "warning",
               f"classifier: {'ON' if ok else 'OFF (rules only)'}  server {classifier.base_url()}  "
               f"slots {n if n is not None else 'unknown'}  can approve safe project commands: "
               f"{'yes' if cfg['judge_can_approve'] else 'no'}" + ("" if ok else f"\n  why: {why}"))
    if not ok:
        app.ui.log("info", "To turn it on, restart llama-server with two slots, for example:\n"
                           "  llama-server -m model.gguf -c 131072 -np 2 --jinja --port 8080\n"
                           "(the context is shared between the slots: -c 131072 -np 2 gives each 65536).")
        return
    judge.clear_cache()
    lines = []
    for text in SAMPLES_THINK:
        d = ThinkingClassifier().classify(text)
        lines.append(f"  thinking  {('on ' if d.thinking else 'off')}  {d.source:<14} {classifier.LAST['ms']:>5} ms  {text[:50]}")
    for command_text in SAMPLES_JUDGE:
        v = judge.judge_command(command_text, "normal")
        shown = f"{v.verdict:<14} {v.reason[:50]}" if v else "no answer"
        lines.append(f"  shell     {classifier.LAST['ms']:>5} ms  {command_text[:34]:<34} -> {shown}")
    app.ui.log("info", "tests:\n" + "\n".join(lines))
