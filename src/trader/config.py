"""Load config/settings.yaml, config/risk.yaml and .env into validated, frozen pydantic models.

- Unknown keys are errors everywhere: nested YAML keys, duplicate YAML keys, unknown .env names.
- Hard caps from risk/caps.py are enforced at load (RISK.md §1). Every problem is reported at once.
- Secrets are ``SecretStr``: they never appear in repr, str, or error messages.
"""

import itertools
from datetime import time
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StrictBool,
    StrictInt,
    ValidationError,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from trader.core.clock import REGULAR_CLOSE, REGULAR_OPEN
from trader.core.models import ClockTime, Dec
from trader.core.yamlfile import load_yaml
from trader.risk import caps

SETTINGS_FILE = Path("config/settings.yaml")
RISK_FILE = Path("config/risk.yaml")
ENV_FILE = Path(".env")

BookName = Literal["A", "B"]


class ConfigError(Exception):
    """Configuration is missing or invalid. ``problems`` lists every issue found."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("invalid configuration:\n" + "\n".join(f"  - {p}" for p in problems))


# ---------------------------------------------------------------- field types

Symbol = Annotated[str, Field(pattern=r"^[A-Z]{1,6}$")]
EnvName = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]*$")]
ModelAlias = Literal["opus", "sonnet", "haiku"]
Effort = Literal["low", "medium", "high"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------- settings.yaml


class BookConfig(_Model):
    enabled: StrictBool
    policy: Literal["baseline", "llm_filtered"]
    account_env_prefix: EnvName


class BooksConfig(_Model):
    A: BookConfig
    B: BookConfig

    def get(self, book: BookName) -> BookConfig:
        return self.A if book == "A" else self.B


class DataConfig(_Model):
    feed: Literal["iex", "sip"]
    credentials_env_prefix: EnvName
    stream_symbol_limit: StrictInt = Field(gt=0)
    rest_rate_limit_per_min: StrictInt = Field(gt=0)
    rest_batch_symbols: StrictInt = Field(gt=0)
    regime_symbols: tuple[Symbol, ...]
    stale_warn_s: StrictInt = Field(gt=0)


class UniverseConfig(_Model):
    exchanges: tuple[str, ...]
    exclude_symbol_chars: tuple[str, ...]
    min_adv14: StrictInt = Field(ge=0)
    min_atr14: Dec = Field(ge=0)
    min_history_sessions: StrictInt = Field(gt=0)
    exclude_corporate_action_days: StrictInt = Field(ge=0)


class SelectionConfig(_Model):
    or_minutes: StrictInt = Field(gt=0)
    scan_at: ClockTime
    scan_deadline: ClockTime
    min_open_price: Dec = Field(gt=0)
    rel_vol_min: float = Field(ge=0)
    rel_vol_lookback_sessions: StrictInt = Field(gt=0)
    top_n: StrictInt = Field(gt=0)
    min_or_volume: StrictInt = Field(ge=0)
    min_or_vol_avg: StrictInt = Field(ge=0)
    record_shadow_shorts: StrictBool


class EntryConfig(_Model):
    trigger_offset: Dec = Field(ge=0)
    limit_buffer_pct: Dec = Field(ge=0)
    min_limit_buffer: Dec = Field(ge=0)
    entry_timeout_s: StrictInt = Field(gt=0)


class ExitConfig(_Model):
    stop_atr_fraction: Dec = Field(gt=0)
    take_profit_r: Dec | None = Field(default=None, gt=0)


class ShadowConfig(_Model):
    slippage_fixed: Dec = Field(ge=0)
    slippage_pct: Dec = Field(ge=0)


class StrategyConfig(_Model):
    name: Literal["orb5_sip_long"]
    universe: UniverseConfig
    selection: SelectionConfig
    entry: EntryConfig
    exit: ExitConfig
    shadow: ShadowConfig


class ScheduleConfig(_Model):
    universe_refresh: ClockTime
    boot: ClockTime
    premarket_scan: ClockTime
    premarket_brief: ClockTime
    premarket_brief_deadline: ClockTime
    last_entry: ClockTime
    flatten: ClockTime
    eod_reconcile: ClockTime
    shadow_outcomes: ClockTime
    postclose_review: ClockTime
    reports: ClockTime


class PremarketRoleConfig(_Model):
    model: ModelAlias
    effort: Effort
    timeout_s: StrictInt = Field(gt=0)
    max_turns: StrictInt = Field(gt=0)
    allow_web: StrictBool
    max_candidates: StrictInt = Field(gt=0)
    max_news_per_symbol: StrictInt = Field(ge=0)


class VetRoleConfig(_Model):
    model: ModelAlias
    effort: Effort
    timeout_s: StrictInt = Field(gt=0)
    max_turns: StrictInt = Field(gt=0)
    max_wait_s: StrictInt = Field(ge=0)
    revet_premarket: StrictBool
    anonymize_entities: StrictBool
    max_news_per_symbol: StrictInt = Field(ge=0)


class ReviewRoleConfig(_Model):
    model: ModelAlias
    effort: Effort
    timeout_s: StrictInt = Field(gt=0)
    max_turns: StrictInt = Field(gt=0)
    history_days: StrictInt = Field(ge=0)  # past daily summaries in the pack (STRATEGY.md §6.6)
    notable_untraded: StrictInt = Field(ge=0)  # largest-|shadow_r| untraded targets (§6.5)
    max_news_per_target: StrictInt = Field(ge=0)


class LlmRolesConfig(_Model):
    premarket: PremarketRoleConfig
    vet: VetRoleConfig
    review: ReviewRoleConfig


class LlmConfig(_Model):
    enabled: StrictBool
    provider: Literal["claude_cli", "anthropic_api"]
    claude_bin: str = Field(min_length=1)
    min_cli_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    config_dir: Path
    sandbox_dir: Path
    max_calls_per_day: StrictInt = Field(gt=0)
    max_concurrent: StrictInt = Field(gt=0)
    roles: LlmRolesConfig


class LessonsConfig(_Model):
    min_n_per_side: StrictInt = Field(gt=0)
    min_effect_r: float = Field(ge=0)
    ci_level: float = Field(gt=0, lt=1)
    bootstrap_resamples: StrictInt = Field(gt=0)
    bootstrap_seed: StrictInt
    max_playbook_lessons: StrictInt = Field(gt=0)


class NewsConfig(_Model):
    alpaca_stream: StrictBool
    premarket_lookback_hours: StrictInt = Field(gt=0)
    vet_lookback_hours: StrictInt = Field(gt=0)


class EdgarConfig(_Model):
    user_agent_env: EnvName
    # SEC fair access: stay well under 10 requests/second (RISK.md §10).
    max_rps: float = Field(gt=0, lt=10)
    lookback_days: StrictInt = Field(gt=0)
    flag_forms: tuple[str, ...]


class NotifyConfig(_Model):
    provider: Literal["none", "ntfy", "discord"]
    ntfy_server: str = Field(pattern=r"^https?://\S+$")
    ntfy_topic_env: EnvName
    discord_webhook_env: EnvName
    daily_summary: StrictBool


class FeesConfig(_Model):
    sec_fee_per_million_sold: Dec = Field(ge=0)
    finra_taf_per_share_sold: Dec = Field(ge=0)
    finra_taf_max_per_trade: Dec = Field(ge=0)


class StorageConfig(_Model):
    db_path: Path
    logs_dir: Path
    recordings_dir: Path
    reports_dir: Path
    llm_artifacts_dir: Path
    retention_days: StrictInt = Field(gt=0)
    db_backups_keep: StrictInt = Field(gt=0)


class LiveConfig(_Model):
    size_multiplier: Dec = Field(gt=0, le=1)
    first_sessions_at_half_size: StrictInt = Field(ge=0)


class Settings(_Model):
    """config/settings.yaml."""

    mode: Literal["paper", "live"]
    timezone: Literal["America/New_York"]
    books: BooksConfig
    data: DataConfig
    strategy: StrategyConfig
    schedule: ScheduleConfig
    llm: LlmConfig
    lessons: LessonsConfig
    news: NewsConfig
    edgar: EdgarConfig
    notify: NotifyConfig
    fees: FeesConfig
    storage: StorageConfig
    live: LiveConfig


# ---------------------------------------------------------------- risk.yaml


class RiskConfig(_Model):
    """config/risk.yaml (RISK.md §2)."""

    max_risk_per_trade_pct: Dec = Field(gt=0)
    daily_loss_limit_pct: Dec = Field(gt=0)
    max_position_pct: Dec = Field(gt=0)
    max_concurrent_positions: StrictInt = Field(ge=1)
    max_entries_per_day: StrictInt = Field(ge=1)
    min_price: Dec = Field(gt=0)
    max_price: Dec = Field(gt=0)
    min_stop_distance: Dec = Field(gt=0)
    max_stop_distance_pct: Dec = Field(gt=0)
    max_chase_pct: Dec = Field(ge=0)
    entry_timeout_s: StrictInt = Field(gt=0)
    unprotected_position_max_s: StrictInt = Field(gt=0)
    partial_fill_cancel_max_s: StrictInt = Field(gt=0)  # R-020 while an entry's cancel pends
    entry_window_start: ClockTime
    data_stale_halt_s: StrictInt = Field(gt=0)
    symbol_quote_max_age_s: StrictInt = Field(gt=0)
    drawdown_breaker_pct: Dec = Field(gt=0)
    consecutive_losing_days_breaker: StrictInt = Field(ge=1)
    reconcile_cash_tolerance: Dec = Field(ge=0)
    account_equity_band_pct: Dec = Field(gt=0)
    event_blackout_before_min: StrictInt = Field(ge=0)
    event_blackout_after_min: StrictInt = Field(ge=0)
    llm_failures_degrade_after: StrictInt = Field(ge=1)


# ---------------------------------------------------------------- .env


class Secrets(BaseSettings):
    """Secrets from ``<root>/.env``; a non-empty process env var of the same name overrides.

    Field names are the lower-cased names from config/env.example. Unknown names are rejected,
    empty values count as unset, and presence is checked by ``trader doctor``, not here.
    """

    model_config = SettingsConfigDict(
        extra="forbid", frozen=True, env_ignore_empty=True, env_file_encoding="utf-8"
    )

    alpaca_paper_a_key_id: SecretStr | None = None
    alpaca_paper_a_secret_key: SecretStr | None = None
    alpaca_paper_b_key_id: SecretStr | None = None
    alpaca_paper_b_secret_key: SecretStr | None = None
    alpaca_data_key_id: SecretStr | None = None
    alpaca_data_secret_key: SecretStr | None = None
    claude_code_oauth_token: SecretStr | None = None
    claude_token_created: str | None = None
    anthropic_api_key: SecretStr | None = None
    sec_edgar_user_agent: SecretStr | None = None
    ntfy_topic: SecretStr | None = None
    discord_webhook_url: SecretStr | None = None
    alpaca_live_key_id: SecretStr | None = None
    alpaca_live_secret_key: SecretStr | None = None
    trader_live_confirm: str | None = None

    @classmethod
    def env_names(cls) -> frozenset[str]:
        """Every accepted .env key, upper-case."""
        return frozenset(name.upper() for name in cls.model_fields)

    def secret(self, env_name: str) -> SecretStr | None:
        value = getattr(self, env_name.lower(), None)
        return value if isinstance(value, SecretStr) else None

    def is_set(self, env_name: str) -> bool:
        return getattr(self, env_name.lower(), None) is not None

    def redaction_map(self) -> dict[str, str]:
        """Env name -> raw value for every secret that is set (feeds log redaction)."""
        out: dict[str, str] = {}
        for name in type(self).model_fields:
            value = getattr(self, name)
            if isinstance(value, SecretStr) and value.get_secret_value():
                out[name.upper()] = value.get_secret_value()
        return out


# ---------------------------------------------------------------- the whole config


class AppConfig(_Model):
    root: Path
    settings: Settings
    risk: RiskConfig
    secrets: Secrets

    def path(self, p: Path) -> Path:
        """Resolve a configured path against the repo root."""
        return p if p.is_absolute() else self.root / p

    def alpaca_credentials(self, book: BookName) -> tuple[SecretStr | None, SecretStr | None]:
        prefix = self.settings.books.get(book).account_env_prefix
        return self.secrets.secret(f"{prefix}_KEY_ID"), self.secrets.secret(f"{prefix}_SECRET_KEY")

    def data_credentials(self) -> tuple[SecretStr | None, SecretStr | None]:
        """Market-data keys: ``data.credentials_env_prefix``, else Book B's (settings comment)."""
        prefix = self.settings.data.credentials_env_prefix
        key = self.secrets.secret(f"{prefix}_KEY_ID")
        secret = self.secrets.secret(f"{prefix}_SECRET_KEY")
        if key is None and secret is None:
            return self.alpaca_credentials("B")
        return key, secret


def load_config(root: Path) -> AppConfig:
    """Load and cross-check all configuration under ``root``.

    Raises ConfigError listing every problem found, never secret values.
    """
    problems: list[str] = []
    settings = _load_yaml_model(root / SETTINGS_FILE, Settings, "settings", problems)
    risk = _load_yaml_model(root / RISK_FILE, RiskConfig, "risk", problems)
    secrets = load_secrets(root / ENV_FILE, problems)
    if settings is not None:
        problems += env_name_problems(settings)
        if risk is not None:
            problems += cap_violations(settings, risk)
            problems += consistency_problems(settings, risk)
    if problems or settings is None or risk is None or secrets is None:
        raise ConfigError(problems)
    return AppConfig(root=root, settings=settings, risk=risk, secrets=secrets)


def load_secrets(env_file: Path, problems: list[str]) -> Secrets | None:
    try:
        return Secrets(_env_file=env_file if env_file.is_file() else None)
    except ValidationError as exc:
        problems.extend(_describe(".env", exc, upper=True))
        return None


# ---------------------------------------------------------------- cross-checks


def _minutes(t: time) -> int:
    return t.hour * 60 + t.minute


LATEST_FLATTEN = time(*divmod(_minutes(REGULAR_CLOSE) - caps.FLATTEN_LATEST_BEFORE_CLOSE_MIN, 60))


def cap_violations(settings: Settings, risk: RiskConfig) -> list[str]:
    """Every way the configuration exceeds a hard cap in risk/caps.py (RISK.md §1)."""
    out: list[str] = []
    ceilings: list[tuple[str, Decimal | int, str, Decimal | int]] = [
        ("max_risk_per_trade_pct", risk.max_risk_per_trade_pct, "MAX_RISK_PER_TRADE_PCT",
         caps.MAX_RISK_PER_TRADE_PCT),
        ("daily_loss_limit_pct", risk.daily_loss_limit_pct, "MAX_DAILY_LOSS_LIMIT_PCT",
         caps.MAX_DAILY_LOSS_LIMIT_PCT),
        ("max_position_pct", risk.max_position_pct, "MAX_POSITION_PCT", caps.MAX_POSITION_PCT),
        ("max_concurrent_positions", risk.max_concurrent_positions, "MAX_CONCURRENT_POSITIONS",
         caps.MAX_CONCURRENT_POSITIONS),
        ("max_entries_per_day", risk.max_entries_per_day, "MAX_ENTRIES_PER_DAY",
         caps.MAX_ENTRIES_PER_DAY),
    ]  # fmt: skip
    for key, value, cap_name, cap in ceilings:
        if value > cap:
            out.append(f"risk.{key} = {value} exceeds hard cap {cap_name} = {cap}")
    if risk.min_price < caps.MIN_PRICE_FLOOR:
        out.append(
            f"risk.min_price = {risk.min_price} is below hard floor "
            f"MIN_PRICE_FLOOR = {caps.MIN_PRICE_FLOOR}"
        )
    if settings.schedule.flatten > LATEST_FLATTEN:
        out.append(
            f"settings.schedule.flatten = {settings.schedule.flatten} is later than "
            f"close - FLATTEN_LATEST_BEFORE_CLOSE_MIN ({LATEST_FLATTEN})"
        )
    if not caps.ALLOW_EXTENDED_HOURS and risk.entry_window_start < REGULAR_OPEN:
        out.append(
            f"risk.entry_window_start = {risk.entry_window_start} is before the regular open "
            f"{REGULAR_OPEN} and ALLOW_EXTENDED_HOURS is False"
        )
    return out


def consistency_problems(settings: Settings, risk: RiskConfig) -> list[str]:
    """Settings that are individually valid but contradict each other."""
    out: list[str] = []
    s, sel = settings.schedule, settings.strategy.selection
    timeline = [
        ("settings.schedule.universe_refresh", s.universe_refresh),
        ("settings.schedule.boot", s.boot),
        ("settings.schedule.premarket_scan", s.premarket_scan),
        ("settings.schedule.premarket_brief", s.premarket_brief),
        ("settings.schedule.premarket_brief_deadline", s.premarket_brief_deadline),
        ("settings.strategy.selection.scan_at", sel.scan_at),
        ("settings.strategy.selection.scan_deadline", sel.scan_deadline),
        ("settings.schedule.last_entry", s.last_entry),
        ("settings.schedule.flatten", s.flatten),
        ("settings.schedule.eod_reconcile", s.eod_reconcile),
        ("settings.schedule.shadow_outcomes", s.shadow_outcomes),
        ("settings.schedule.postclose_review", s.postclose_review),
        ("settings.schedule.reports", s.reports),
    ]
    for (a_name, a), (b_name, b) in itertools.pairwise(timeline):
        if not a < b:
            out.append(f"{a_name} ({a}) must be earlier than {b_name} ({b})")
    if not risk.entry_window_start < s.last_entry:
        out.append(
            f"risk.entry_window_start ({risk.entry_window_start}) must be earlier than "
            f"settings.schedule.last_entry ({s.last_entry})"
        )
    if not risk.min_price < risk.max_price:
        out.append(f"risk.min_price ({risk.min_price}) must be below risk.max_price")
    if risk.partial_fill_cancel_max_s < risk.unprotected_position_max_s:
        out.append(
            f"risk.partial_fill_cancel_max_s ({risk.partial_fill_cancel_max_s}) must be at least "
            f"risk.unprotected_position_max_s ({risk.unprotected_position_max_s})"
        )
    entry_timeout = settings.strategy.entry.entry_timeout_s
    if entry_timeout != risk.entry_timeout_s:
        out.append(
            f"settings.strategy.entry.entry_timeout_s ({entry_timeout}) must equal "
            f"risk.entry_timeout_s ({risk.entry_timeout_s}); risk.yaml is authoritative"
        )
    if settings.books.A.policy != "baseline":
        out.append("settings.books.A.policy must be 'baseline' (Book A is rules only)")
    if settings.books.B.policy != "llm_filtered":
        out.append("settings.books.B.policy must be 'llm_filtered'")
    if settings.books.A.account_env_prefix == settings.books.B.account_env_prefix:
        out.append("settings.books: A and B must use separate accounts (account_env_prefix)")
    return out


def env_name_problems(settings: Settings) -> list[str]:
    """Env names referenced by settings.yaml must be keys that .env may define."""
    known = Secrets.env_names()
    out: list[str] = []
    prefixes = [
        ("settings.books.A.account_env_prefix", settings.books.A.account_env_prefix),
        ("settings.books.B.account_env_prefix", settings.books.B.account_env_prefix),
        ("settings.data.credentials_env_prefix", settings.data.credentials_env_prefix),
    ]
    for where, prefix in prefixes:
        for name in (f"{prefix}_KEY_ID", f"{prefix}_SECRET_KEY"):
            if name not in known:
                out.append(f"{where} = {prefix}: {name} is not a known .env key (env.example)")
    names = [
        ("settings.edgar.user_agent_env", settings.edgar.user_agent_env),
        ("settings.notify.ntfy_topic_env", settings.notify.ntfy_topic_env),
        ("settings.notify.discord_webhook_env", settings.notify.discord_webhook_env),
    ]
    for where, name in names:
        if name not in known:
            out.append(f"{where} = {name} is not a known .env key (env.example)")
    return out


# ---------------------------------------------------------------- loading helpers


def _load_yaml_model[M: BaseModel](
    path: Path, model: type[M], label: str, problems: list[str]
) -> M | None:
    if not path.is_file():
        problems.append(
            f"{label}: {path} not found (copy config/{path.stem}.example.yaml; PLAN.md H0.3)"
        )
        return None
    try:
        data = load_yaml(path)
    except yaml.YAMLError as exc:
        problems.append(f"{label}: invalid YAML in {path.name}: {exc}")
        return None
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        problems.extend(_describe(label, exc))
        return None


def _describe(label: str, exc: ValidationError, *, upper: bool = False) -> list[str]:
    """One line per validation error, without the offending input (it may be a secret)."""
    out: list[str] = []
    for err in exc.errors(include_url=False, include_context=False, include_input=False):
        loc = ".".join(str(part) for part in err["loc"])
        if upper:
            loc = loc.upper()
        out.append(f"{label}.{loc}: {err['msg']}" if loc else f"{label}: {err['msg']}")
    return out
