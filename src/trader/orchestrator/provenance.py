"""Which code a day ran: a hash of the source, made without git (DECISIONS D-055).

``boot.json`` names HEAD's commit, but a gate session may run an uncommitted tree (D-054). The
hash covers the engine's Python and SQL, the prompts and the committed schemas, with line
endings normalized so that a checkout on another system hashes the same.
"""

import hashlib
from pathlib import Path

SOURCES = ("src/trader/**/*.py", "src/trader/**/*.sql", "prompts/*.md", "schemas/*.json")


def source_sha256(root: Path) -> str | None:
    """SHA-256 over each source file's relative path and contents, in the order of their POSIX
    paths (Windows paths compare without case); None when ``root`` holds no source tree."""
    found = {path for pattern in SOURCES for path in root.glob(pattern) if path.is_file()}
    files = sorted(found, key=lambda path: path.relative_to(root).as_posix())
    if not files:
        return None
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0")
        digest.update(path.read_bytes().replace(b"\r\n", b"\n") + b"\0")
    return digest.hexdigest()
