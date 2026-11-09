Create a complete, advanced chess game in Python using Pygame and Python-Chess (`python-chess` library for move validation and game state management). The application should feature a clean graphical user interface and robust functionality.

### Core Requirements

1. **Architecture & Libraries:**
* Use `pygame` for rendering the graphical user interface, event handling, and user interactions.
* Use the `python-chess` library (`import chess`) to handle board state, move generation, legality, check/checkmate detection, and stalemate rules.


2. **Graphical User Interface (GUI):**
* A classic 8x8 chessboard with a clean, modern visual style, wood or minimalist flat textures, and clear coordinate labels (A-H, 1-8).
* High-contrast highlighting for:
* The currently selected piece.
* Legal move destinations for the selected piece.
* The origin and destination squares of the last played move.
* Check alerts (highlighting the king's square in red when in check).


* Smooth drag-and-drop piece movement as well as click-to-select/click-to-move mechanics.
* Render captured pieces on the side panels along with material advantage tracking (e.g., White +1).
* A game status bar showing whose turn it is, checkmate/draw states, and a move timer or move history log.


3. **Advanced Gameplay Features:**
* **Promotion Dialog:** A popup or overlay menu when a pawn reaches the final rank, allowing the player to choose between Queen, Rook, Bishop, or Knight.
* **Castling & En Passant:** Fully supported via `python-chess`.
* **Move History Panel:** A scrollable text box or side panel listing previous moves in standard algebraic notation (SAN).
* **Undo/Redo Functionality:** Buttons or hotkeys (`Ctrl+Z`) to step backward and forward through move history.
* **Game Controls:** Buttons for "New Game", "Resign", and "Flip Board" (to view from Black's perspective).


4. **AI Opponent (Optional Toggle):**
* Implement a simple AI opponent using Minimax with Alpha-Beta pruning and a basic piece-square evaluation table, or allow an easy integration hook for a UCI-compliant chess engine (like Stockfish).


5. **Code Quality:**
* Well-structured, object-oriented Python code split logically into classes (e.g., `ChessGame`, `BoardRenderer`, `AIPlayer`).
* Clean comments, docstrings, and instructions on how to install required dependencies (`pip install pygame python-chess`).