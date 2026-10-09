"""Test helpers shared across test modules."""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[1]

# Distinctive fake values. Tests assert none of them ever reaches logs or command output.
FAKE_SECRETS: dict[str, str] = {
    "ALPACA_PAPER_A_KEY_ID": "PKTESTAAAAKEYID00001",
    "ALPACA_PAPER_A_SECRET_KEY": "test-secret-A-9f8e7d6c5b4a39281706",
    "ALPACA_PAPER_B_KEY_ID": "PKTESTBBBBKEYID00002",
    "ALPACA_PAPER_B_SECRET_KEY": "test-secret-B-0a1b2c3d4e5f60718293",
    "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-test-token-0123456789abcdef",
    "SEC_EDGAR_USER_AGENT": "Testy McTestface testy@mail.invalid",
}


def write_env(root: Path, values: Mapping[str, str]) -> None:
    lines = [f'{name}="{value}"' for name, value in values.items()]
    (root / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")


def set_yaml(path: Path, dotted_key: str, value: Any) -> None:
    """Set one (possibly new) dotted key in a YAML file, e.g. ``"schedule.flatten"``."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    *parents, last = dotted_key.split(".")
    node = data
    for part in parents:
        node = node[part]
    node[last] = value
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
