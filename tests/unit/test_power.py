"""Keeping the PC awake while run-day runs (P6.1b; DECISIONS D-081). conftest stubs the real
SetThreadExecutionState; these tests replace the stub to see each path."""

import pytest

from trader.ops import power
from trader.ops.power import ES_CONTINUOUS, ES_SYSTEM_REQUIRED, SleepSettings, keep_awake


def test_keep_awake_holds_then_clears(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr(power, "set_execution_state", lambda f: calls.append(f) or ES_CONTINUOUS)
    with keep_awake() as held:
        assert held
        assert calls == [ES_CONTINUOUS | ES_SYSTEM_REQUIRED]
    assert calls == [ES_CONTINUOUS | ES_SYSTEM_REQUIRED, ES_CONTINUOUS]


def test_keep_awake_clears_even_when_the_day_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr(power, "set_execution_state", lambda f: calls.append(f) or ES_CONTINUOUS)
    with pytest.raises(RuntimeError), keep_awake():
        raise RuntimeError("the day failed")
    assert calls[-1] == ES_CONTINUOUS


def test_a_refusal_is_not_cleared(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    refused = 0  # what SetThreadExecutionState returns when it fails
    monkeypatch.setattr(power, "set_execution_state", lambda f: calls.append(f) or refused)
    with keep_awake() as held:
        assert not held
    assert calls == [ES_CONTINUOUS | ES_SYSTEM_REQUIRED]


def test_sleep_settings_reading() -> None:
    assert not SleepSettings(standby_s=0, wake_timers=1).sleeps
    assert SleepSettings(standby_s=900, wake_timers=1).sleeps
    assert SleepSettings(standby_s=900, wake_timers=1).wakes
    assert not SleepSettings(standby_s=900, wake_timers=2).wakes
