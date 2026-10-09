from tests.helpers import REPO


def test_secrets_and_runtime_state_are_gitignored() -> None:
    lines = {
        line.strip()
        for line in (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    # No leading slash: these match at any depth, so config/.env is covered too.
    assert {".env", ".env.*", "runtime/"} <= lines
