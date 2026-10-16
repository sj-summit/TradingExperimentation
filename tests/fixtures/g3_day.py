"""G3 run 2's recorded session, 2026-10-14 (DECISIONS D-054), kept as a golden replay.

``tests/golden/recordings/2026-10-14/`` holds the live session's recording with one change: the
market-data stream keeps every trade but at most one quote per symbol every ``QUOTE_EVERY_S``
seconds of receive time, which shrinks it from 81 MB to about 2 MB. The engine reads quotes only
for R-016's quote age, which the trades keep fresh, and the trimmed day replays exactly as the
full one does (D-054). Next to the recording:
- ``config/`` holds the settings, risk and events files the session ran with (their SHA-256s
  matched ``boot.json`` when this was built);
- ``journal.json`` holds what the live journal recorded for the comparison ``trader replay``
  makes: signals, final decisions and trades.

``python -m tests.fixtures.g3_day <repo root>`` rebuilds all of it from that root's ``runtime/``.
"""

import gzip
import hashlib
import io
import json
import shutil
import sqlite3
import sys
from contextlib import closing
from datetime import date, datetime, time, timedelta
from pathlib import Path

from tests.helpers import REPO
from trader.config import RiskConfig, Settings
from trader.core.clock import ET
from trader.core.events import Event, load_events
from trader.core.yamlfile import load_yaml
from trader.orchestrator.replay import comparable

G3_DAY = date(2026, 10, 14)
RECORDINGS = REPO / "tests" / "golden" / "recordings"
RECORDING = RECORDINGS / G3_DAY.isoformat()
CONFIG = RECORDING / "config"
JOURNAL = RECORDING / "journal.json"
GOLDEN = REPO / "tests" / "golden" / "g3_2026-10-14.json"
QUOTE_EVERY_S = 10.0
COPIED = ("boot.json", "universe.parquet", "rest.jsonl.gz", "broker-A.jsonl.gz")
CONFIG_FILES = {"settings": "settings.yaml", "risk": "risk.yaml", "events": "events.yaml"}

# Where today's engine knowingly differs from the live session, as ``replay.diff`` lines, each
# with the DECISIONS entry that made the change. ``journal.json`` stays the live truth.
CODE_CHANGES = [
    # D-056 (D-054 #5): STT's size skip is named by R-007, which also bound, not by its price.
    "decision recorded but not replayed: 20261014-STT-orb5 A 0 insufficient_settled_cash",
    "decision replayed but not recorded: 20261014-STT-orb5 A 0 capacity_entries",
]


def settings() -> Settings:
    return Settings.model_validate(load_yaml(CONFIG / "settings.yaml"))


def risk() -> RiskConfig:
    return RiskConfig.model_validate(load_yaml(CONFIG / "risk.yaml"))


def events() -> list[Event]:
    midnight = datetime.combine(G3_DAY, time(0), ET)
    return load_events(CONFIG / "events.yaml").between(midnight, midnight + timedelta(days=1))


def recorded() -> dict[str, list[str]]:
    data: dict[str, list[str]] = json.loads(JOURNAL.read_text(encoding="utf-8"))
    return data


def build(root: Path) -> None:
    """Rebuild the fixture from ``root``'s recording, config and journal."""
    src = root / "runtime" / "recordings" / G3_DAY.isoformat()
    boot = json.loads((src / "boot.json").read_text(encoding="utf-8"))
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
        rows = comparable(conn, G3_DAY)
    JOURNAL.write_text(json.dumps(rows, indent=1) + "\n", encoding="utf-8")


def trim_stream(src: Path, dst: Path) -> None:
    """Copy every record except quotes that come within ``QUOTE_EVERY_S`` of the last kept quote
    for the same symbol. The gzip header carries no timestamp, so a rebuild is byte-identical."""
    kept: dict[str, datetime] = {}
    with (
        gzip.open(src, "rt", encoding="utf-8") as lines,
        dst.open("wb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as packed,
        io.TextIOWrapper(packed, encoding="utf-8", newline="") as out,
    ):
        for line in lines:
            record = json.loads(line)
            if record.get("kind") == "quote":
                rx = datetime.fromisoformat(record["rx"])
                last = kept.get(record["symbol"])
                if last is not None and (rx - last).total_seconds() < QUOTE_EVERY_S:
                    continue
                kept[record["symbol"]] = rx
            out.write(line)


if __name__ == "__main__":
    build(Path(sys.argv[1]))
