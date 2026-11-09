import difflib
from rich.console import Console
from rich.syntax import Syntax

console = Console()

# Sample text files to compare
text1 = """def hello():
    print("Hello, World!")
    
hello()
"""

text2 = """def hello():
    print("Hello, Rich!")
    # Added a comment
    
hello()
"""

# Generate a unified diff string
diff_lines = difflib.unified_diff(
    text1.splitlines(keepends=True),
    text2.splitlines(keepends=True),
    fromfile="file_v1.py",
    tofile="file_v2.py",
)
diff_text = "".join(diff_lines)

# Render with Rich syntax highlighting (using 'diff' as the lexer)
syntax = Syntax(diff_text, "diff", theme="monokai", line_numbers=True)
console.print(syntax)