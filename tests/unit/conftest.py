"""Shared fixtures for unit tests (deterministic; no network, no real Discord)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make sibling helper modules (fakes.py) importable regardless of pytest import mode.
sys.path.insert(0, str(Path(__file__).resolve().parent))


@pytest.fixture(autouse=True)
def _clean_secret_registry():
    from copilot.util.logging import clear_registered_secrets

    clear_registered_secrets()
    yield
    clear_registered_secrets()


@pytest.fixture
def tmp_settings(tmp_path):
    from fakes import fake_settings

    return fake_settings(tmp_path)


@pytest.fixture
async def tmp_db(tmp_path):
    from copilot.db.database import Database

    db = Database(tmp_path / "test.db")
    await db.connect()
    yield db
    await db.close()
