"""Shared pytest setup for agent-loop unit tests."""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _secure_umask_for_tests() -> None:
    """Match production entrypoints: Path.write_text creates owner-only files."""
    previous = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(previous)
