import sys
from pathlib import Path

try:
    import hermes_constants  # noqa: F401  (real Hermes venv)
except ImportError:
    sys.path.insert(0, str(Path(__file__).parent / "tests" / "stubs"))
