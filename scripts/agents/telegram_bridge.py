#!/usr/bin/env python3
"""Entrypoint for the local agent Telegram bridge (DX only; not SaaS runtime)."""

from __future__ import annotations

import sys
from pathlib import Path

_AGENTS_DIR = Path(__file__).resolve().parent
if str(_AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENTS_DIR))

from dx.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
