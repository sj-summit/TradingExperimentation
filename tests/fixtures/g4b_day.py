"""G4b's recorded session, 2026-11-20 (DECISIONS D-072), kept as a golden replay of an A+B day.

``tests/golden/recordings/2026-11-20/`` holds the live session's recording, trimmed as G3's is
(``g3_day.trim_stream``: every trade, at most one quote per symbol every ``QUOTE_EVERY_S``
seconds). Beside Book A's broker it keeps Book B's, and ``llm.jsonl.gz``: each job's pack and each
attempt's answer, which the replay serves back instead of asking Claude. The news and the
opening-range scan's table stay out, since a replay reads neither. Next to the recording:
- ``config/`` holds the settings, risk and events files the session ran with (their SHA-256s
  matched ``boot.json`` when this was built);
- ``journal.json`` holds what the live journal recorded for the comparison ``trader replay``
  makes: signals, final decisions and trades, and Book B's calls, day mode and verdicts.

``python -m tests.fixtures.g4b_day <repo root>`` rebuilds all of it from that root's ``runtime/``.
"""

import hashlib
import json
import shutil
import sqlite3
import sys
from contextlib import closing
from datetime import date, datetime, time, timedelta
from pathlib import Path

from tests.fixtures.g3_day import CONFIG_FILES, RECORDINGS, trim_stream
from tests.helpers import REPO
from trader.config import RiskConfig, Settings
from trader.core.clock import ET
from trader.core.events import Event, load_events
from trader.core.yamlfile import load_yaml
from trader.orchestrator.replay import comparable

G4B_DAY = date(2026, 11, 20)
RECORDING = RECORDINGS / G4B_DAY.isoformat()
CONFIG = RECORDING / "config"
JOURNAL = RECORDING / "journal.json"
GOLDEN = REPO / "tests" / "golden" / "g4b_2026-11-20.json"
COPIED = (
    "boot.json",
    "universe.parquet",
    "rest.jsonl.gz",
    "broker-A.jsonl.gz",
    "broker-B.jsonl.gz",
    "llm.jsonl.gz",
)

# Where today's engine knowingly differs from the live session, as ``replay.diff`` lines, each
# with the DECISIONS entry that made the change. ``journal.json`` stays the live truth.
CODE_CHANGES: list[str] = []


def settings() -> Settings:
    return Settings.model_validate(load_yaml(CONFIG / "settings.yaml"))


def risk() -> RiskConfig:
    return RiskConfig.model_validate(load_yaml(CONFIG / "risk.yaml"))


def events() -> list[Event]:
    midnight = datetime.combine(G4B_DAY, time(0), ET)
    return load_events(CONFIG / "events.yaml").between(midnight, midnight + timedelta(days=1))


def recorded() -> dict[str, list[str]]:
    data: dict[str, list[str]] = json.loads(JOURNAL.read_text(encoding="utf-8"))
    return data


def build(root: Path) -> None:
    """Rebuild the fixture from ``root``'s recording, config and journal."""
    src = root / "runtime" / "recordings" / G4B_DAY.isoformat()
    boot = json.loads((src / "boot.json").read_text(encoding="utf-8"))
    if (src / "boot-2.json").exists():
        raise SystemExit("the engine restarted that day: a replay covers its first run only")
    for name, file in CONFIG_FILES.items():
        digest = hashlib.sha256((root / "config" / file).read_bytes()).hexdigest()
        if digest != boot[f"{name}_sha256"]:
            raise SystemExit(f"config/{file} has changed since the session ran")
    if RECORDING.exists():
        shutil.rmtree(RECORDING)
    CONFIG.mkdir(parents=True)
    for name in COPIED:
        shutil.copyfile(src / name, RECORDING / name)
    for file in CONFIG_FILES.values():
        shutil.copyfile(root / "config" / file, CONFIG / file)
    trim_stream(src / "stream.jsonl.gz", RECORDING / "stream.jsonl.gz")
    db = (root / "runtime" / "trader.db").as_posix()
    with closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        rows = comparable(conn, G4B_DAY)
    JOURNAL.write_text(json.dumps(rows, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    build(Path(sys.argv[1]))
