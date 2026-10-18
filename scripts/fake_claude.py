"""A stand-in for the ``claude`` CLI (ARCHITECTURE.md §7.4; DECISIONS D-064).

Tests run it as ``[python, scripts/fake_claude.py, --scenario NAME, <claude's argv>]``. It first
checks what a real call must look like, and exits 2 naming each problem:

- the argv: the instruction right after ``-p``, every A§7.2 flag with its value, ``--safe-mode``,
  no ``--bare``, and a valid JSON schema under 8 KB;
- the environment: ``CLAUDE_CONFIG_DIR``, ``DISABLE_AUTOUPDATER=1``, exactly one credential, and
  no other ``ANTHROPIC_*`` or ``CLAUDE_*`` variable (each could switch the provider, the endpoint
  or the mode), nor ``NODE_OPTIONS``;
- the working directory: empty.

Then it reads the pack on stdin and answers as the scenario says. It never touches the network.
"""

import json
import os
import signal
import sys
import time
from pathlib import Path

SCHEMA_LIMIT = 8 * 1024
VALUED = {
    "--output-format", "--json-schema", "--model", "--effort", "--system-prompt-file", "--tools",
    "--disallowedTools", "--mcp-config", "--setting-sources", "--permission-mode",
    "--permission-prompts", "--max-turns",
}  # fmt: skip
SWITCHES = {"--strict-mcp-config", "--no-session-persistence", "--safe-mode"}
EXPECTED = {
    "--output-format": "json",
    "--mcp-config": '{"mcpServers":{}}',
    "--setting-sources": "user",
    "--permission-mode": "dontAsk",
    "--permission-prompts": "none",
}
CREDENTIALS = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")
OURS = frozenset({*CREDENTIALS, "CLAUDE_CONFIG_DIR"})  # the only ANTHROPIC_* or CLAUDE_* allowed
SCENARIOS = {
    "valid", "invalid_json", "schema_invalid", "repair_ok", "no_structured_output",
    "structured_retries", "is_error", "max_turns", "nonzero_exit", "nonzero_with_envelope",
    "slow", "slow_stubborn", "wrong_pack_id", "huge", "leaky", "stdin_missing",
}  # fmt: skip


def problems(argv: list[str], env: dict[str, str], cwd: Path) -> list[str]:
    found: list[str] = []
    if len(argv) < 2 or argv[0] != "-p" or argv[1].startswith("-"):
        return ["the instruction must come right after -p"]
    values: dict[str, str] = {}
    switches: set[str] = set()
    i = 2
    while i < len(argv):
        flag = argv[i]
        if flag in VALUED and i + 1 < len(argv):
            values[flag] = argv[i + 1]
            i += 2
        elif flag in SWITCHES:
            switches.add(flag)
            i += 1
        else:
            found.append(f"unexpected argument {flag!r}")
            i += 1
    for flag, value in EXPECTED.items():
        if values.get(flag) != value:
            found.append(f"{flag} must be {value!r}")
    found += [f"{flag} is missing" for flag in sorted(SWITCHES - switches)]
    tools, denied = values.get("--tools"), values.get("--disallowedTools")
    if (tools, denied) not in (("", "mcp__*"), (None, "*")):
        found.append("tools must be --tools '' with --disallowedTools 'mcp__*', or 'deny all'")
    schema = values.get("--json-schema", "")
    if len(schema.encode()) >= SCHEMA_LIMIT:
        found.append(f"the schema is {len(schema.encode())} bytes, over 8 KB")
    try:
        json.loads(schema)
    except ValueError:
        found.append("the schema is not JSON")
    if not Path(values.get("--system-prompt-file", "")).is_file():
        found.append("the system prompt file does not exist")
    if values.get("--model") not in ("opus", "sonnet", "haiku"):
        found.append("--model must be an alias")
    if values.get("--effort") not in ("low", "medium", "high"):
        found.append("--effort must be low, medium or high")
    if not values.get("--max-turns", "").isdigit():
        found.append("--max-turns must be a whole number")
    if not env.get("CLAUDE_CONFIG_DIR"):
        found.append("CLAUDE_CONFIG_DIR is not set")
    if env.get("DISABLE_AUTOUPDATER") != "1":
        found.append("DISABLE_AUTOUPDATER is not 1")
    credentials = [k for k in CREDENTIALS if env.get(k)]
    if len(credentials) != 1:
        found.append(f"expected one credential, found {credentials}")
    found += [
        f"{name} leaked into the environment"
        for name in sorted(env)
        if (name.upper().startswith(("ANTHROPIC_", "CLAUDE_")) and name.upper() not in OURS)
        or name.upper() == "NODE_OPTIONS"
    ]
    if any(cwd.iterdir()):
        found.append(f"the working directory {cwd} is not empty")
    return found


def say(data: object, code: int = 0) -> None:
    sys.stdout.write(json.dumps(data))
    sys.stdout.flush()
    sys.exit(code)


INTERRUPTED: list[bool] = []


def handle_interrupts(scenario: str) -> None:
    """The slow scenarios' interrupt handling, set before anything slow runs: an interrupt that
    came first would end the process the default way, where the CLI ends its turn."""
    interrupt = getattr(signal, "SIGBREAK", signal.SIGINT)
    if scenario == "slow_stubborn":
        signal.signal(interrupt, signal.SIG_IGN)
    elif scenario == "slow":
        signal.signal(interrupt, lambda *_: INTERRUPTED.append(True))


def wait() -> None:
    end = time.monotonic() + 120
    while time.monotonic() < end and not INTERRUPTED:
        time.sleep(0.02)
    sys.stderr.write("fake_claude: interrupted\n")
    sys.exit(130)


def main() -> None:
    if len(sys.argv) < 3 or sys.argv[1] != "--scenario" or sys.argv[2] not in SCENARIOS:
        sys.stderr.write(f"usage: fake_claude.py --scenario {{{','.join(sorted(SCENARIOS))}}}\n")
        sys.exit(64)
    scenario, argv = sys.argv[2], sys.argv[3:]
    handle_interrupts(scenario)
    from trader.llm.fake import REPAIR_MARK, answer_for, envelope  # slow: after the handler

    found = problems(argv, dict(os.environ), Path.cwd())
    if found:
        sys.stderr.write("fake_claude: " + "; ".join(found) + "\n")
        sys.exit(2)
    sys.stderr.write(f"fake_claude: {scenario}: the argv, environment and cwd are as required\n")
    if scenario == "stdin_missing":  # never reads stdin, so it can't copy the pack's id
        say(envelope({"pack_id": "vet-20990101-0123456789abcdef", "assessments": []}))
    pack = json.loads(sys.stdin.buffer.read())
    answer = answer_for(pack)
    repair = REPAIR_MARK in argv[1]
    if scenario == "valid" or (scenario == "repair_ok" and repair):
        say(envelope(answer))
    if scenario in ("schema_invalid", "repair_ok"):
        say(envelope({**answer, "size_multiplier": 2}))  # a key the model may not add
    if scenario == "invalid_json":
        sys.stdout.write("Here is my answer: it is not JSON")
        sys.exit(0)
    if scenario == "no_structured_output":
        say(envelope(None, result="I could not decide."))
    if scenario == "structured_retries":
        say(envelope(None, subtype="error_max_structured_output_retries", is_error=True), 1)
    if scenario == "is_error":
        say(envelope(None, is_error=True, result="Invalid API key · Please run /login"), 1)
    if scenario == "max_turns":
        say(envelope(None, subtype="error_max_turns", is_error=True, errors=["max turns"]), 1)
    if scenario == "nonzero_exit":
        sys.stderr.write("Error: the CLI crashed\n")
        sys.exit(1)
    if scenario == "nonzero_with_envelope":
        say(envelope(answer), 1)  # a good answer, but the run says it failed
    if scenario in ("slow", "slow_stubborn"):
        wait()
    if scenario == "wrong_pack_id":
        say(envelope({**answer, "pack_id": "vet-20990101-0123456789abcdef"}))
    if scenario == "huge":
        chunk = b"x" * 65536
        for _ in range(200):  # 12.5 MB: past any cap
            sys.stdout.buffer.write(chunk)
        sys.exit(0)
    if scenario == "leaky":
        token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
        sys.stderr.write(f"debug: token={token} key=sk-ant-api03-abcdefghijklmnop\n")
        say(envelope(answer, result=f"token {token}"))


if __name__ == "__main__":
    main()
