from datetime import time
from decimal import Decimal
from pathlib import Path

import pytest

from tests.helpers import FAKE_SECRETS, REPO, set_yaml, write_env
from trader.config import ConfigError, Secrets, load_config
from trader.risk import caps

SETTINGS = Path("config/settings.yaml")
RISK = Path("config/risk.yaml")


def problems_of(root: Path) -> list[str]:
    with pytest.raises(ConfigError) as info:
        load_config(root)
    return info.value.problems


def has(problems: list[str], *fragments: str) -> bool:
    return any(all(f in p for f in fragments) for p in problems)


# ---------------------------------------------------------------- loading


def test_example_configs_load(repo: Path) -> None:
    cfg = load_config(repo)
    assert cfg.settings.mode == "paper"
    assert cfg.settings.schedule.flatten == time(15, 55)
    assert cfg.risk.entry_window_start == time(9, 35, 3)
    assert cfg.risk.max_risk_per_trade_pct == Decimal("1.0")
    assert cfg.risk.partial_fill_cancel_max_s == 20  # the human's R-020 choice (D-053)


def test_decimals_are_exact(repo: Path) -> None:
    cfg = load_config(repo)
    assert str(cfg.risk.min_stop_distance) == "0.03"
    assert str(cfg.risk.max_chase_pct) == "0.3"
    assert isinstance(cfg.settings.strategy.entry.trigger_offset, Decimal)
    assert cfg.settings.fees.finra_taf_per_share_sold == Decimal("0.000195")
    assert cfg.settings.fees.sec_fee_per_million_sold == Decimal("20.60")


def test_missing_settings_file_points_to_example(repo: Path) -> None:
    (repo / SETTINGS).unlink()
    assert has(problems_of(repo), "settings.example.yaml")


# ---------------------------------------------------------------- unknown keys


def test_unknown_top_level_key_rejected(repo: Path) -> None:
    set_yaml(repo / SETTINGS, "surprise", 1)
    assert has(problems_of(repo), "settings.surprise:")


def test_unknown_nested_key_rejected(repo: Path) -> None:
    set_yaml(repo / SETTINGS, "strategy.entry.typo_key", 1)
    assert has(problems_of(repo), "settings.strategy.entry.typo_key:")


def test_unknown_risk_key_rejected(repo: Path) -> None:
    set_yaml(repo / RISK, "max_risk_pct", 1.0)
    assert has(problems_of(repo), "risk.max_risk_pct:")


def test_duplicate_yaml_key_rejected(repo: Path) -> None:
    path = repo / RISK
    text = path.read_text(encoding="utf-8") + "max_risk_per_trade_pct: 1.5\n"
    path.write_text(text, encoding="utf-8")
    assert has(problems_of(repo), "duplicate key 'max_risk_per_trade_pct'")


# ---------------------------------------------------------------- hard caps (RISK.md §1)

CAP_CASES = [
    ("max_risk_per_trade_pct", 2.5, "MAX_RISK_PER_TRADE_PCT"),
    ("daily_loss_limit_pct", 5.5, "MAX_DAILY_LOSS_LIMIT_PCT"),
    ("max_position_pct", 150.0, "MAX_POSITION_PCT"),
    ("max_concurrent_positions", 4, "MAX_CONCURRENT_POSITIONS"),
    ("max_entries_per_day", 7, "MAX_ENTRIES_PER_DAY"),
    ("min_price", 0.5, "MIN_PRICE_FLOOR"),
]


@pytest.mark.parametrize(("key", "value", "cap_name"), CAP_CASES)
def test_cap_violation_fails_fast(repo: Path, key: str, value: float, cap_name: str) -> None:
    set_yaml(repo / RISK, key, value)
    assert has(problems_of(repo), f"risk.{key}", cap_name)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("max_risk_per_trade_pct", float(caps.MAX_RISK_PER_TRADE_PCT)),
        ("daily_loss_limit_pct", float(caps.MAX_DAILY_LOSS_LIMIT_PCT)),
        ("max_position_pct", float(caps.MAX_POSITION_PCT)),
        ("max_concurrent_positions", caps.MAX_CONCURRENT_POSITIONS),
        ("max_entries_per_day", caps.MAX_ENTRIES_PER_DAY),
        ("min_price", float(caps.MIN_PRICE_FLOOR)),
    ],
)
def test_values_exactly_at_caps_are_allowed(repo: Path, key: str, value: float) -> None:
    set_yaml(repo / RISK, key, value)
    load_config(repo)


def test_all_violations_reported_together(repo: Path) -> None:
    set_yaml(repo / RISK, "max_risk_per_trade_pct", 3.0)
    set_yaml(repo / RISK, "max_entries_per_day", 9)
    set_yaml(repo / SETTINGS, "schedule.flatten", "15:59")
    problems = problems_of(repo)
    assert has(problems, "MAX_RISK_PER_TRADE_PCT")
    assert has(problems, "MAX_ENTRIES_PER_DAY")
    assert has(problems, "FLATTEN_LATEST_BEFORE_CLOSE_MIN")


def test_flatten_no_later_than_close_minus_cap(repo: Path) -> None:
    set_yaml(repo / SETTINGS, "schedule.flatten", "15:57")
    load_config(repo)
    set_yaml(repo / SETTINGS, "schedule.flatten", "15:57:01")
    assert has(problems_of(repo), "settings.schedule.flatten", "FLATTEN_LATEST_BEFORE_CLOSE_MIN")


def test_entry_window_cannot_open_before_regular_session(repo: Path) -> None:
    set_yaml(repo / RISK, "entry_window_start", "09:29:59")
    assert has(problems_of(repo), "risk.entry_window_start", "ALLOW_EXTENDED_HOURS")


# ---------------------------------------------------------------- field types


def test_unquoted_time_rejected(repo: Path) -> None:
    # YAML 1.1 reads an unquoted 15:55 as the integer 955.
    path = repo / SETTINGS
    text = path.read_text(encoding="utf-8")
    assert 'flatten: "15:55"' in text
    path.write_text(text.replace('flatten: "15:55"', "flatten: 15:55"), encoding="utf-8")
    assert has(problems_of(repo), "settings.schedule.flatten:", "quoted")


def test_bool_is_not_an_int(repo: Path) -> None:
    set_yaml(repo / RISK, "max_entries_per_day", True)
    assert has(problems_of(repo), "risk.max_entries_per_day:")


def test_edgar_rate_stays_under_sec_fair_access_limit(repo: Path) -> None:
    set_yaml(repo / SETTINGS, "edgar.max_rps", 10)
    assert has(problems_of(repo), "settings.edgar.max_rps:")


# ---------------------------------------------------------------- consistency


def test_schedule_must_be_in_order(repo: Path) -> None:
    set_yaml(repo / SETTINGS, "schedule.last_entry", "15:56")
    assert has(problems_of(repo), "settings.schedule.last_entry", "settings.schedule.flatten")


def test_entry_timeout_defined_twice_must_match(repo: Path) -> None:
    set_yaml(repo / SETTINGS, "strategy.entry.entry_timeout_s", 20)
    assert has(problems_of(repo), "entry_timeout_s", "risk.yaml is authoritative")


def test_the_partial_fill_allowance_is_required(repo: Path) -> None:
    path = repo / RISK
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    path.write_text(
        "".join(line for line in lines if not line.startswith("partial_fill_cancel_max_s")),
        encoding="utf-8",
    )
    assert has(problems_of(repo), "risk.partial_fill_cancel_max_s")


def test_the_partial_fill_allowance_is_never_below_the_normal_one(repo: Path) -> None:
    set_yaml(repo / RISK, "partial_fill_cancel_max_s", 3)
    assert has(problems_of(repo), "partial_fill_cancel_max_s", "unprotected_position_max_s")


def test_book_a_is_rules_only(repo: Path) -> None:
    set_yaml(repo / SETTINGS, "books.A.policy", "llm_filtered")
    assert has(problems_of(repo), "settings.books.A.policy")


def test_books_use_separate_accounts(repo: Path) -> None:
    set_yaml(repo / SETTINGS, "books.B.account_env_prefix", "ALPACA_PAPER_A")
    assert has(problems_of(repo), "separate accounts")


def test_env_prefix_must_name_known_keys(repo: Path) -> None:
    set_yaml(repo / SETTINGS, "books.A.account_env_prefix", "ALPACA_PAPER_C")
    assert has(problems_of(repo), "ALPACA_PAPER_C_KEY_ID is not a known .env key")


# ---------------------------------------------------------------- .env


def test_secret_names_match_env_example() -> None:
    names = set()
    for raw in (REPO / "config" / "env.example").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            names.add(line.split("=", 1)[0])
    assert Secrets.env_names() == names


def test_unknown_env_key_reported_by_name_never_value(repo: Path) -> None:
    leaked = "value-that-must-never-be-printed-123"
    write_env(repo, {**FAKE_SECRETS, "ALPACA_PAPER_A_SECRET": leaked})
    with pytest.raises(ConfigError) as info:
        load_config(repo)
    assert has(info.value.problems, ".env.ALPACA_PAPER_A_SECRET")
    assert leaked not in str(info.value)
    assert leaked not in repr(info.value)


def test_empty_env_values_are_unset(repo: Path) -> None:
    write_env(repo, {**FAKE_SECRETS, "ANTHROPIC_API_KEY": ""})
    cfg = load_config(repo)
    assert cfg.secrets.anthropic_api_key is None
    assert not cfg.secrets.is_set("ANTHROPIC_API_KEY")


def test_missing_env_file_means_nothing_set(repo: Path) -> None:
    (repo / ".env").unlink()
    assert load_config(repo).secrets.redaction_map() == {}


def test_process_env_overrides_dotenv(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPACA_PAPER_A_KEY_ID", "PKFROMPROCESSENV0001")
    key, _ = load_config(repo).alpaca_credentials("A")
    assert key is not None
    assert key.get_secret_value() == "PKFROMPROCESSENV0001"


def test_alpaca_credentials_follow_book_prefix(repo: Path) -> None:
    key, secret = load_config(repo).alpaca_credentials("B")
    assert key is not None
    assert secret is not None
    assert key.get_secret_value() == FAKE_SECRETS["ALPACA_PAPER_B_KEY_ID"]
    assert secret.get_secret_value() == FAKE_SECRETS["ALPACA_PAPER_B_SECRET_KEY"]


def test_data_credentials_fall_back_to_book_b(repo: Path) -> None:
    key, secret = load_config(repo).data_credentials()
    assert key is not None
    assert secret is not None
    assert key.get_secret_value() == FAKE_SECRETS["ALPACA_PAPER_B_KEY_ID"]
    assert secret.get_secret_value() == FAKE_SECRETS["ALPACA_PAPER_B_SECRET_KEY"]


def test_dedicated_data_credentials_win(repo: Path) -> None:
    extra = {"ALPACA_DATA_KEY_ID": "PKDATAKEY000000001", "ALPACA_DATA_SECRET_KEY": "data-secret-1"}
    write_env(repo, {**FAKE_SECRETS, **extra})
    key, _ = load_config(repo).data_credentials()
    assert key is not None
    assert key.get_secret_value() == "PKDATAKEY000000001"


def test_redaction_map_lists_every_set_secret(repo: Path) -> None:
    assert load_config(repo).secrets.redaction_map() == FAKE_SECRETS


def test_secrets_never_in_repr_str_or_dump(repo: Path) -> None:
    cfg = load_config(repo)
    rendered = [repr(cfg), str(cfg), cfg.model_dump_json(), repr(cfg.secrets), str(cfg.secrets)]
    for value in FAKE_SECRETS.values():
        for text in rendered:
            assert value not in text
