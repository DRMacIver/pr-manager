"""Shared test configuration.

Every test runs with the pr-manager data directories redirected into
pytest's tmp_path, so no test — present or future — can read or write the
real ~/.local/share/pr-manager, even if it forgets to request a fixture.
"""
from __future__ import annotations

import pytest

from pr_manager import constants, git, state


@pytest.fixture(autouse=True)
def isolated_data_dirs(tmp_path, monkeypatch):
    """Redirect STATE_PATH / REPOS_DIR / LOGS_DIR into tmp_path.

    Patches both the constants module and the modules that imported the
    values by name.
    """
    base = tmp_path / "pr-manager-data"
    monkeypatch.setattr(constants, "BASE_DIR", base)
    monkeypatch.setattr(constants, "STATE_PATH", base / "state.json")
    monkeypatch.setattr(constants, "REPOS_DIR", base / "repos")
    monkeypatch.setattr(constants, "LOGS_DIR", base / "logs")
    monkeypatch.setattr(state, "STATE_PATH", base / "state.json")
    monkeypatch.setattr(git, "REPOS_DIR", base / "repos")
    monkeypatch.setattr(git, "LOGS_DIR", base / "logs")
    return base


@pytest.fixture
def state_path(isolated_data_dirs):
    return isolated_data_dirs / "state.json"


@pytest.fixture
def repos_dir(isolated_data_dirs):
    d = isolated_data_dirs / "repos"
    d.mkdir(parents=True, exist_ok=True)
    return d
