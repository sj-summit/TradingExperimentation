"""Scrub secret values from text before it reaches logs or output (CLAUDE.md invariant 6)."""

from collections.abc import Mapping

# Shorter values can't be real credentials and would mangle ordinary text.
MIN_SECRET_LEN = 4


def redact_text(text: str, secrets: Mapping[str, str]) -> str:
    """Replace every occurrence of each secret value with ``***NAME***``.

    Longest values go first so a secret that contains another is replaced whole.
    """
    for name, value in sorted(secrets.items(), key=lambda kv: len(kv[1]), reverse=True):
        if len(value) >= MIN_SECRET_LEN and value in text:
            text = text.replace(value, f"***{name}***")
    return text
