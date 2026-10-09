"""Claude Code CLI integration (ARCHITECTURE.md §7).

Phase 0 holds only the isolated subprocess environment; the ``claude -p`` client lands in P4.4.
"""

from collections.abc import Mapping
from pathlib import Path

# Variables copied from the parent environment; everything else is dropped. Credentials and
# provider switches (ANTHROPIC_*, CLAUDE_CODE_USE_*, ...) outrank the subscription token, so they
# must never leak into a call (A§7.1, D-002, code.claude.com/docs/en/authentication).
_PASSTHROUGH = frozenset(
    {
        # process basics (Windows and POSIX)
        "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC",
        "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
        "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
        "COMPUTERNAME", "USERNAME", "USER", "LOGNAME", "LANG", "LC_ALL", "TZ",
        # network plumbing: proxies and corporate CA bundles, never credentials
        "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE",
    }
)  # fmt: skip


def build_claude_env(
    *,
    parent: Mapping[str, str],
    config_dir: Path,
    oauth_token: str | None,
    api_key: str | None = None,
) -> dict[str, str]:
    """Environment for a ``claude`` subprocess: an OS-basics allowlist plus our own settings.

    ``CLAUDE_CONFIG_DIR`` isolates the bot from the human's ``~/.claude`` (settings, hooks, MCP
    servers, memory). Pass the subscription ``oauth_token`` or, only when ``llm.provider`` is
    ``anthropic_api``, an ``api_key`` — never both.
    """
    if oauth_token and api_key:
        raise ValueError("pass the OAuth token or an API key, not both")
    env = {name: value for name, value in parent.items() if name.upper() in _PASSTHROUGH}
    env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    if oauth_token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_token
    if api_key:
        env["ANTHROPIC_API_KEY"] = api_key
    return env
