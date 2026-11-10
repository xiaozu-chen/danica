from textual.app import App, ComposeResult
from textual_image.widget import Image

class SixelImageApp(App[None]):
    def compose(self) -> ComposeResult:
        # Pass the path to your image file
        yield Image("C:/Users/user/AppData/Roaming/Danica/sessions/20261110-094748/workspace/charts/scores.png")

if __name__ == "__main__":
    SixelImageApp().run()