"""Shared environment path handling for CLI and installer-facing commands."""

from __future__ import annotations

import os
from pathlib import Path


def configured_path(name: str, default: str | Path) -> Path:
    """Resolve a path override with shell `${NAME:-default}` semantics."""
    return Path(os.environ.get(name) or str(default))
