"""
Shared fixtures for the agent/ test suite. Modules under agent/ are flat
imports (no package __init__.py — the real agent.py adds its own directory
to sys.path at process start), so tests do the same thing explicitly here.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
