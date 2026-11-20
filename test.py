from textual.app import App, ComposeResult
from textual.widgets import Input

class CenteredInputApp(App):
    CSS = """
    Screen {
        align: center middle;
    }

    Input {
        width: 50;  /* Or a percentage like 50% */
    }
    """

    def compose(self) -> ComposeResult:
        yield Input(placeholder="Type something here...")

if __name__ == "__main__":
    app = CenteredInputApp()
    app.run()