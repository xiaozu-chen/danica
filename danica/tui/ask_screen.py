"""
ask_screen.py - The dialog behind the ask_user tool: Danica asks 1 to 4 short questions, each with
a few options (pick one, or several for a "multi" question) and a box to type your own answer.

Result (what the waiting worker thread gets back):
    None                     you pressed Esc / Skip
    [{"question": str, "answers": [str, ...], "other": str}, ...]   one entry per question
"""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, RadioButton, RadioSet, SelectionList, Static
from textual.widgets.selection_list import Selection


class AskScreen(ModalScreen):
    """questions = [{"question": str, "options": [{"label": str, "description": str}], "multi": bool}]"""

    BINDINGS = [Binding("escape", "skip", "Skip")]

    DEFAULT_CSS = """
    AskScreen { align: center middle; background: black 60%; }
    AskScreen .ask-box { width: 80%; min-width: 60; max-width: 110; height: auto; max-height: 92%;
                         padding: 1 2; background: #313131; border: round #ffffff; }
    AskScreen .ask-scroll { height: auto; max-height: 26; margin-bottom: 1; }
    AskScreen .ask-q { text-style: bold; margin-top: 1; }
    AskScreen .ask-q:first-child { margin-top: 0; }
    AskScreen RadioSet, AskScreen SelectionList { height: auto; max-height: 9; background: #222222; border: none; padding: 0 1; }
    AskScreen Input { margin-top: 0; }
    AskScreen .ask-buttons { height: auto; }
    AskScreen .ask-buttons Button { width: 1fr; min-width: 0; margin-right: 2; background: #3f3f3f; }
    AskScreen .ask-buttons Button:last-of-type { margin-right: 0; }
    """

    def __init__(self, questions: list[dict]) -> None:
        super().__init__()
        self.questions = questions

    @staticmethod
    def _label(option: dict) -> str:
        return option["label"] + (f"  -  {option['description']}" if option.get("description") else "")

    def compose(self) -> ComposeResult:
        many = len(self.questions) > 1
        with Vertical(classes="ask-box"):
            yield Static(Text("Danica has " + ("some questions" if many else "a question")
                              + "  (a few words each is enough)", style="bold #ff9e64"), classes="dialog-title")
            with VerticalScroll(classes="ask-scroll"):
                for i, q in enumerate(self.questions):
                    yield Static(Text(f"{i + 1}. {q['question']}" + ("   (pick any)" if q.get("multi") else "")), classes="ask-q")
                    if q.get("multi"):
                        yield SelectionList(*[Selection(self._label(o), k) for k, o in enumerate(q["options"])], id=f"sel{i}")
                    else:
                        yield RadioSet(*[RadioButton(self._label(o)) for o in q["options"]], id=f"rad{i}")
                    yield Input(placeholder="...or type your own answer", id=f"oth{i}")
            with Horizontal(classes="ask-buttons"):
                yield Button("Send answers", id="send", variant="success")
                yield Button("Skip  (Esc)", id="skip")

    def on_mount(self) -> None:
        first = self.query("RadioSet, SelectionList")
        if first:
            first.first().focus()

    def answers(self) -> list[dict]:
        """What the user chose, one entry per question (an unanswered question has no answers)."""
        out = []
        for i, q in enumerate(self.questions):
            if q.get("multi"):
                picked = [q["options"][k]["label"] for k in self.query_one(f"#sel{i}", SelectionList).selected]
            else:
                index = self.query_one(f"#rad{i}", RadioSet).pressed_index
                picked = [q["options"][index]["label"]] if index is not None and index >= 0 else []
            out.append({"question": q["question"], "answers": picked,
                        "other": self.query_one(f"#oth{i}", Input).value.strip()[:300]})
        return out

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(self.answers() if event.button.id == "send" else None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()                       # Enter in a text box sends everything
        self.dismiss(self.answers())

    def action_skip(self) -> None:
        self.dismiss(None)
