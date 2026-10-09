import shutil
from pathlib import Path

import pytest

from tests.helpers import FAKE_SECRETS, REPO, write_env
from trader.config import Secrets


@pytest.fixture(autouse=True)
def _isolate_process_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Secrets in the developer's own environment must never reach a test."""
    for name in Secrets.env_names():
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("TRADER_ROOT", raising=False)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A throw-away repo root: config copied from the committed examples plus a fake .env."""
    config = tmp_path / "config"
    config.mkdir()
    shutil.copy(REPO / "config" / "settings.example.yaml", config / "settings.yaml")
    shutil.copy(REPO / "config" / "risk.example.yaml", config / "risk.yaml")
    shutil.copy(REPO / "config" / "events.yaml", config / "events.yaml")
    write_env(tmp_path, {**FAKE_SECRETS, "CLAUDE_TOKEN_CREATED": "2026-10-01"})
    return tmp_path
