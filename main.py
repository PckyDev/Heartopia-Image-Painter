import sys
from pathlib import Path


_ROOT = Path(__file__).resolve().parent
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


try:
    from heartopia_painter.app import run
except ModuleNotFoundError as e:
    missing = getattr(e, "name", None)
    if missing in {"PySide6", "pillow", "PIL", "mss", "pynput", "pyautogui"} or (
        isinstance(missing, str) and missing.startswith("PySide6")
    ):
        sys.stderr.write(
            "Missing Python dependencies.\n\n"
            "This usually means you're running with the wrong Python interpreter (not the project's venv).\n\n"
            "Fix:\n"
            "  1) Activate the venv: .\\.venv\\Scripts\\Activate.ps1\n"
            "  2) Install deps:     python -m pip install -r requirements.txt\n"
            "  3) Run:              python main.py\n\n"
            "Or run directly with the venv python:\n"
            "  .\\.venv\\Scripts\\python.exe main.py\n"
        )
        raise SystemExit(1)
    raise


if __name__ == "__main__":
    run()
