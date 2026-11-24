"""Children that die with the engine: the kill-on-close job object (P6.2b; DECISIONS D-083)."""

import subprocess
import sys
import time

import pytest

from trader.ops.containment import KillOnCloseJob

WINDOWS = sys.platform == "win32"


@pytest.mark.skipif(not WINDOWS, reason="a Windows job object")
def test_closing_the_job_kills_its_processes() -> None:
    """What a killed engine's exit does: the OS closes the job's last handle."""
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        job = KillOnCloseJob()
        assert job.active
        assert job.assign(child.pid)
        started = time.monotonic()
        job.close()
        child.wait(timeout=15)  # killed, long before its minute
        assert time.monotonic() - started < 15
    finally:
        if child.poll() is None:
            child.kill()


@pytest.mark.skipif(not WINDOWS, reason="a Windows job object")
def test_a_process_left_alone_outlives_a_closed_job() -> None:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        job = KillOnCloseJob()
        job.close()  # never assigned
        with pytest.raises(subprocess.TimeoutExpired):
            child.wait(timeout=1)
    finally:
        child.kill()
        child.wait(timeout=15)


def test_a_process_that_isnt_there_isnt_assigned() -> None:
    job = KillOnCloseJob()
    try:
        assert not job.assign(2**31 - 1)  # no such process: logged, not raised
    finally:
        job.close()
    assert not job.active
    assert not job.assign(1)  # a closed job assigns nothing
