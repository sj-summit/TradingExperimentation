from pathlib import Path

import pytest

from trader.llm.claude_cli import build_claude_env

CONFIG_DIR = Path("C:/repo/runtime/claude-config")
PARENT = {
    "PATH": "C:/bin",
    "SYSTEMROOT": "C:/Windows",
    "USERPROFILE": "C:/Users/me",
    # everything below must be dropped
    "ANTHROPIC_API_KEY": "sk-ant-api-from-parent",
    "ANTHROPIC_AUTH_TOKEN": "bearer",
    "ANTHROPIC_BASE_URL": "https://proxy.invalid",
    "ANTHROPIC_PROFILE": "work",
    "ANTHROPIC_FEDERATION_RULE_ID": "rule",
    "ANTHROPIC_ORGANIZATION_ID": "org",
    "CLAUDE_CODE_USE_BEDROCK": "1",
    "CLAUDE_CODE_USE_VERTEX": "1",
    "CLAUDE_CODE_USE_FOUNDRY": "1",
    "CLAUDE_CONFIG_DIR": "C:/Users/me/.claude",
    "CLAUDE_CODE_OAUTH_TOKEN": "parent-token",
    "AWS_SECRET_ACCESS_KEY": "aws",
    "ALPACA_PAPER_A_SECRET_KEY": "alpaca",
    "SOMETHING_ELSE": "x",
}


def test_only_os_basics_and_our_settings_pass_through() -> None:
    env = build_claude_env(parent=PARENT, config_dir=CONFIG_DIR, oauth_token="bot-token")
    assert set(env) == {
        "PATH",
        "SYSTEMROOT",
        "USERPROFILE",
        "CLAUDE_CONFIG_DIR",
        "CLAUDE_CODE_OAUTH_TOKEN",
    }
    assert env["CLAUDE_CONFIG_DIR"] == str(CONFIG_DIR)
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "bot-token"


def test_no_credential_or_provider_switch_leaks() -> None:
    env = build_claude_env(parent=PARENT, config_dir=CONFIG_DIR, oauth_token="bot-token")
    leaked = [k for k in env if k.startswith(("ANTHROPIC_", "CLAUDE_CODE_USE_", "AWS_", "ALPACA_"))]
    assert leaked == []


def test_api_key_only_for_the_api_provider() -> None:
    env = build_claude_env(
        parent=PARENT, config_dir=CONFIG_DIR, oauth_token=None, api_key="sk-ant-api-mine"
    )
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-api-mine"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env


def test_token_and_api_key_are_exclusive() -> None:
    with pytest.raises(ValueError, match="not both"):
        build_claude_env(parent={}, config_dir=CONFIG_DIR, oauth_token="a", api_key="b")
