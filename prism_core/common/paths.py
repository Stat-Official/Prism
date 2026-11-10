"""Project paths independent of the process working directory."""
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
