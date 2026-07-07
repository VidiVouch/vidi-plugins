import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# The html-report scripts are standalone (no package); import them by path.
sys.path.insert(0, str(REPO / "skills" / "html-report" / "scripts"))
