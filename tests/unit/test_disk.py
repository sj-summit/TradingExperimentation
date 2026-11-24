"""Disk space and drives (P6.3; DECISIONS D-084)."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from trader.doctor import MIN_FREE_DISK_BYTES
from trader.ops import disk
from trader.ops.disk import MIN_FREE_BYTES, free_bytes, is_network_drive, size_text


def test_doctor_and_run_day_share_one_threshold() -> None:
    assert MIN_FREE_BYTES == MIN_FREE_DISK_BYTES == 2 * 1024**3


def test_free_space_is_read_from_the_nearest_folder_that_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[Path] = []

    def usage(path: Path) -> SimpleNamespace:
        asked.append(Path(path))
        return SimpleNamespace(free=123)

    monkeypatch.setattr(disk.shutil, "disk_usage", usage)
    assert free_bytes(tmp_path / "not" / "made" / "yet") == 123
    assert asked == [tmp_path]


def test_sizes_read_in_binary_units() -> None:
    sizes = (0, 1, 1023, 1024, 1536 * 1024, 3 * 1024**3 // 2)
    assert [size_text(n) for n in sizes] == [
        "0 B",
        "1 B",
        "1023 B",
        "1.0 KiB",
        "1.5 MiB",
        "1.5 GiB",
    ]


@pytest.mark.parametrize("share", [r"\\nas\backups", "//nas/backups"])
def test_a_share_is_a_network_drive(share: str) -> None:
    assert is_network_drive(Path(share))


def test_this_pcs_own_disk_is_not(tmp_path: Path) -> None:
    assert not is_network_drive(tmp_path)
    assert not is_network_drive(Path("relative/folder"))
