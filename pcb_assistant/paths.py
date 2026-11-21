"""Workspace paths shared by modules, independent of their package depth."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def python_executable(environment, root=ROOT):
    """Resolve a workspace virtualenv interpreter on Windows or Unix."""
    relative = "Scripts/python.exe" if os.name == "nt" else "bin/python"
    return str(Path(root) / environment / relative)
