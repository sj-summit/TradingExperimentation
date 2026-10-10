# DECISIONS.md — decision log

Append-only. Each entry records context, the decision, its consequences, and sources. To change a decision, add a new entry that supersedes the old one; never edit history. Claude Code appends entries for API surprises, spec deviations, and anything a future reader would ask "why?" about.

---

## D-001 — Broker: Alpaca paper accounts; never automate Fidelity
- **Date:** 2026-10-08 · **Status:** Accepted
- **Context:** The human's broker is Fidelity.
  - Fidelity offers no public trading API for retail customers.
  - Unofficial libraries exist that drive fidelity.com with Playwright.
  - Fidelity's Terms of Use (§14, "Prohibited Uses and Termination of Access", last updated July 1, 2026) prohibit third-party tools for automated access, explicitly including tools used to facilitate trading in a Fidelity account. Fidelity may suspend access for violations.
- **Decision:** All automated trading uses Alpaca's official API in **separate** accounts: two paper accounts at $1,000 each (Books A and B), and later, if approved, one live cash account. Fidelity is never automated.
- **Consequences:** The human's main brokerage relationship is never put at risk, and a bug can cost at most the bot account's balance.
- **Sources:** https://www.fidelity.com/terms-of-use · https://docs.alpaca.markets/us/docs/paper-trading
- **Revisit:** If Fidelity publishes an official retail trading API.

## D-002 — LLM access: Claude Code CLI headless with the Max subscription; isolated
- **Date:** 2026-10-08 · **Status:** Accepted
- **Context:**
  - The human has a Claude Max plan and prefers not to pay per API token.
  - The Claude Code legal page says subscription OAuth is intended for ordinary use of Claude Code and native Anthropic apps. Advertised limits assume ordinary, individual usage of Claude Code and the Agent SDK. Third parties may not route requests through subscription credentials.
  - Anthropic announced moving `claude -p` and Agent SDK usage to a separate monthly credit, then **paused** that change on June 15, 2026. For now, `claude -p` draws from normal subscription limits, and Anthropic says it will share any update before it takes effect.
  - The Anthropic consumer terms prohibit automated access except via an API key or where explicitly permitted. Scripting claude.ai is therefore out.
- **Decision:**
  - Invoke Anthropic's own `claude` CLI in print mode (`claude -p`) from the engine, for this personal project.
  - Authenticate with a one-year token from `claude setup-token`, passed as `CLAUDE_CODE_OAUTH_TOKEN`.
  - Isolate each call:
    - dedicated `CLAUDE_CONFIG_DIR`;
    - empty working directory;
    - API-key env vars stripped (they take precedence over the OAuth token);
    - no tools by default;
    - no MCP servers.
  - Don't use `--bare`; it doesn't read subscription credentials.
  - Keep an `AnthropicApiClient` path (API key) as a switchable fallback.
  - About 3–6 calls per trading day; hard cap 12.
- **Consequences:**
  - Claude usage counts against the human's Max limits (shared with their chat and Claude Code use; resets every five hours).
  - If the policy changes, switch `llm.provider` to `anthropic_api`.
- **Sources:**
  - https://code.claude.com/docs/en/legal-and-compliance
  - https://support.claude.com/en/articles/15036540
  - https://code.claude.com/docs/en/authentication
  - https://code.claude.com/docs/en/headless
  - https://code.claude.com/docs/en/cli-reference
  - https://www.anthropic.com/legal/consumer-terms
- **Revisit:** Quarterly, or whenever Anthropic updates the Agent SDK / `claude -p` policy.

## D-003 — Account semantics: cash-account rules, long only, no leverage
- **Date:** 2026-10-08 · **Status:** Accepted
- **Context:**
  - The SEC approved eliminating FINRA's pattern-day-trader rule ($25,000 minimum, trade counting) on April 14, 2026, effective June 4, 2026. Brokers may phase it in through Oct 20, 2027.
  - Fidelity has implemented it, but margin accounts still require $2,000 minimum equity.
  - Below $2,000 a margin account can't use leverage.
  - Cash accounts must buy with settled funds, or risk good-faith violations; three in 12 months restrict the account.
- **Decision:**
  - Simulate cash-account rules in our own ledger: T+1 settlement, and daily buys capped at start-of-day settled cash. This applies even though Alpaca paper accounts are margin-enabled.
  - Long only, no leverage, whole shares.
- **Consequences:** At most about 2 entries per day per book at 50% sizing; shorts appear only as shadow signals.
- **Sources:**
  - https://www.fidelity.com/learning-center/trading-investing/intraday-trading
  - https://www.finra.org/investors/insights/intraday-margin-requirements
  - https://advisorservices.schwab.com/story/introduction-to-pattern-day-trader-rules
  - https://www.fidelity.com/learning-center/trading-investing/trading/avoiding-cash-trading-violations
- **Revisit:** If capital grows past $2,000 and the human wants margin (a new decision is required).

## D-004 — Baseline strategy: 5-minute ORB on Stocks in Play, long-only adaptation
- **Date:** 2026-10-08 · **Status:** Accepted
- **Context:**
  - Zarattini, Barbon & Aziz (Feb 2024) tested a 5-minute ORB on US stocks, 2016–2023. The universe filters were price > $5, 14-day ADV ≥ 1M, 14-day ATR > $0.50.
  - It traded the top 20 by opening-range relative volume (≥ 100%), in the direction of the first 5-minute candle.
  - The stop was 10% of ATR from entry, with an exit at the close.
  - Sizing was 1% risk per position, up to 4× leverage.
  - The reported results were +1,637% net, Sharpe 2.81, versus +29% (Sharpe 0.48) for the same ORB without the relative-volume filter.
  - It is a backtest with leverage and shorts, and a working paper.
- **Decision:** Use it as the rules-only baseline, with the deviations listed in STRATEGY.md §3.6.
- **Consequences:**
  - Results will differ from the paper: no shorts, no leverage, IEX data, small capital.
  - The baseline mainly serves as a well-specified, testable signal generator for measuring the LLM.
- **Sources:** https://ssrn.com/abstract=4729284 · https://www.alexandria.unisg.ch/entities/publication/ab50f49b-4d5c-4aa8-aaa1-af7a4c131008
- **Revisit:** After the Phase 3 backtest (H4 sanity check), and at the Phase 7 evaluation.

## D-005 — Experiment design: two books, LLM can only subtract, shadow outcomes
- **Date:** 2026-10-08 · **Status:** Accepted
- **Context:** The goal is to know whether the LLM adds value, not just whether the system makes money.
- **Decision:**
  - Book A runs the rules only; Book B runs the rules plus Claude's vetoes. They use separate paper accounts and identical signals.
  - Claude can veto or reduce, never add.
  - Every triggered signal gets a simulated shadow outcome, so every veto can be scored regardless of cash or capacity limits.
  - Hypotheses and the go-live criteria are pre-registered (STRATEGY.md §8).
- **Consequences:** A clean ablation. The cost is that Book B can't express "discretionary trader" ideas; that is deferred to optional Book C.
- **Revisit:** Phase 7.

## D-006 — Market data: Alpaca free plan (IEX real-time, SIP history)
- **Date:** 2026-10-08 · **Status:** Accepted
- **Context:**
  - Alpaca's free plan streams real-time data from IEX only, with 30 WebSocket symbols.
  - Full consolidated real-time data costs $99/month, about 10% of the capital per month.
  - SIP data older than 15 minutes is available on the free plan.
  - Alpaca's paper-trading page says *paper-only* account holders are entitled to IEX data only, so SIP history likely needs a regular (KYC'd, possibly unfunded) Alpaca account with the paper accounts created under it. Verify in P1.2 (Q7).
- **Decision:**
  - Daily features (ATR14, ADV14) come from SIP daily bars.
  - Opening-range relative volume compares IEX to IEX (the same feed for baseline and today), with liquidity guards.
  - Shadow outcomes use SIP 1-minute bars after the close.
  - The SIP upgrade is a later experiment (E4).
- **Consequences:** Triggers can lag the consolidated tape, and relative-volume ranks are noisier. Stated in every report's limitations footer.
- **Sources:** https://docs.alpaca.markets/docs/market-data
- **Revisit:** E4.

## D-007 — Execution: client-side trigger, marketable-limit OTO with stop leg, whole shares
- **Date:** 2026-10-08 · **Status:** Accepted
- **Context:**
  - Alpaca bracket orders require both a take-profit and a stop-loss.
  - OTO orders attach just one exit leg, such as a stop-loss.
  - Advanced orders require `time_in_force` day or gtc, and no extended hours.
  - The stop must be at least $0.01 below the base price.
  - Open buy orders reserve buying power until filled or cancelled.
  - Paper fills ignore market impact and latency slippage, and randomly partial-fill about 10% of eligible orders.
- **Decision:**
  - The engine triggers on stream trades and checks cash and risk at trigger time.
  - It submits a marketable limit buy with `order_class: oto` and a `stop_loss` leg, `tif: day`.
  - No take-profit. Flatten at close − 5 min. Whole shares only.
- **Consequences:**
  - A stop exists at the broker from the moment of fill.
  - The stop is anchored to the trigger price rather than the fill price (documented deviation).
  - Unfilled entries are cancelled after 10 s.
- **Sources:** https://docs.alpaca.markets/us/docs/orders-at-alpaca · https://docs.alpaca.markets/us/docs/paper-trading

## D-008 — LLM output contract: `--json-schema` + pydantic; no tools; repair once
- **Date:** 2026-10-08 · **Status:** Accepted (verify in G4a)
- **Context:**
  - `claude -p --output-format json --json-schema '<schema>'` returns validated structured output in `structured_output`.
  - `--tools ""` disables built-in tools.
  - `--permission-prompts none` (v2.1.259+) denies prompts in unattended runs.
  - Stdin is capped at 10 MB.
- **Decision:**
  - Use the flags in ARCHITECTURE.md §7.2 and validate the output again with pydantic.
  - Make one repair call on failure, then fall back per STRATEGY.md §5.4.
  - Verify the combination of `--tools ""` and `--json-schema` empirically at G4a.
- **Sources:** https://code.claude.com/docs/en/cli-reference · https://code.claude.com/docs/en/headless

## D-009 — Memory: statistically gated reflection, not "learning from each trade"
- **Date:** 2026-10-08 · **Status:** Accepted
- **Context:**
  - Reflection-based trading agents exist (for example FinMem's immediate plus extended reflection; TradingAgents' multi-role debate).
  - Independent long-horizon re-tests of LLM trading strategies (FINSABER) found reported advantages deteriorate with broader and longer evaluation.
  - Single-trade outcomes are noise.
- **Decision:**
  - Claude proposes lesson hypotheses as machine-checkable evidence queries.
  - Code promotes a lesson only with n ≥ 20 per side, an effect of at least 0.10 R, and a 90% bootstrap CI that excludes 0.
  - Promoted lessons are soft guidance in prompts and can never change rules or risk limits.
- **Sources:** https://arxiv.org/abs/2311.13743 · https://arxiv.org/abs/2412.20138 · https://arxiv.org/abs/2505.07078

## D-010 — Forward-test the LLM only; backtest rules only
- **Date:** 2026-10-08 · **Status:** Accepted
- **Context:** LLM backtests are biased when the test period overlaps the model's training data. The model may know what followed, and its general knowledge of famous companies can distract from the news itself.
- **Decision:** Never report LLM performance on historical dates. Replays of recorded days re-use the **recorded** LLM outputs, never fresh calls. Backtests cover the rules-only strategy.
- **Sources:** https://arxiv.org/abs/2309.17322

## D-011 — Stack
- **Date:** 2026-10-08 · **Status:** Accepted
- **Decision:** Python 3.12, uv, alpaca-py, pydantic v2, typer, structlog, httpx, tenacity, pandas/pyarrow, and SQLite (WAL, single writer).
- **Note on the Alpaca MCP server:** It is not used inside the bot. Execution goes only through the engine and the risk gate. The human may use it interactively for manual inspection of the paper accounts.
- **Source:** https://github.com/alpacahq/alpaca-mcp-server

## D-012 — Base rates acknowledged
- **Date:** 2026-10-08 · **Status:** Accepted
- **Context:** Base rates are poor, for people and for models.
  - Of Brazilian futures day traders who persisted 300+ days, 97% lost money and only 1.1% earned more than minimum wage. There was no evidence of learning.
  - In live LLM trading competitions (Alpha Arena, 2025), most frontier models lost money.
- **Decision:** Treat this as an experiment with pre-registered success criteria. Book A and SPY buy-and-hold are the benchmarks Book B must beat.
- **Sources:** https://ssrn.com/abstract=3423101 · https://forklog.com/en/ai-model-grok-4-2-triumphs-in-trading-tournament/

## D-013 — Reviews: every trade reviewed, stored as rows, recalled the next day
- **Date:** 2026-10-09 · **Status:** Accepted (extends D-009; does not change it)
- **Context:**
  - The first draft of the post-close review graded only Book B vetoes and Book B allows that lost 1R or more. Wins and Book A trades never got a "what went well, and why".
  - Everything except lesson proposals lived only in the call's output file. Nobody could query "every bad decision, and why", and the next day's review never saw earlier ones.
  - Patterns too rare to reach a lesson's sample size were simply forgotten.
- **Decision:**
  - Code picks the review targets: every traded signal, every triggered Book B veto, and up to 5 notable untraded signals. Each gets what went well, what went badly, why, outcome factors from a fixed enum, whether the cause was knowable at decision time, and a Book B process grade (STRATEGY.md §6.5).
  - The pack gives Claude evidence for "why": path stats after the trigger, SPY's move over the same window, and news stamped before or after the decision.
  - Each part of the review goes to its own table. Process issues and human notes link to open ones and accumulate counts until the human closes them.
  - Each review sees the last 10 days' summaries, the epoch's factor tally, and the open issues and notes (§6.6).
- **Consequences:**
  - The review pack grows to about 50–150 KB, under the 300 KB cap. The review schema must still fit the 8 KB inline limit (checked in P4.1).
  - Factor tags are hindsight labels and can be biased by the outcome. They suggest hypotheses and inform the human, but they are never DSL fields and never promote a lesson.
  - This is a spec change before P4.5, so it has no epoch impact.

## D-014 — `risk/caps.py` is created in P0.2, not P2.3
- **Date:** 2026-10-09 · **Status:** Accepted (human approved in the Phase 0 plan)
- **Context:** P0.2 must enforce the hard caps at config load (RISK.md §1), but PLAN.md scheduled `caps.py` for P2.3.
- **Decision:** Create `caps.py` in P0.2 with exactly the R§1 table values and nothing else. P2.3 keeps the ledger work; the caps file needs no change there.
- **Consequences:**
  - Config loading fails fast from Phase 0 on.
  - Observed: the `.claude/settings.json` ask rule did not prompt when the file was created, because the session was started in the parent folder and that settings file was not loaded. The human approved the exact contents in the plan instead. Start Claude Code inside `ai-day-trader/` so the guardrails load.

## D-015 — Phase 0 "doctor all green" means no FAIL
- **Date:** 2026-10-09 · **Status:** Accepted
- **Context:** `config/events.yaml` stays empty until P1.8, so doctor's "no high-impact events in the next 14 days" check is a WARN for the whole of Phase 0.
- **Decision:** The Phase 0 done-criterion "doctor is all green" means no FAIL lines; the events WARN is expected until P1.8.

## D-016 — Tests block the network with `--allow-hosts`, not `--disable-socket`
- **Date:** 2026-10-09 · **Status:** Accepted
- **Context:** On Windows, asyncio's Proactor event loop creates a loopback `socketpair` for its self-pipe. pytest-socket's `--disable-socket` blocks socket creation, so every async test errored with `SocketBlockedError` (observed with pytest-socket 0.8.1, Python 3.12.15; `--allow-unix-socket` doesn't help).
- **Decision:** Use `--allow-hosts=127.0.0.1,::1`. Sockets can be created, but any connection to a non-loopback address raises. `tests/unit/test_network_guard.py` proves both behaviors.
- **Consequences:** Tests stay offline (CLAUDE.md invariant 9). A test may still talk to a loopback stub server.

## D-017 — `tzdata` is a runtime dependency
- **Date:** 2026-10-09 · **Status:** Accepted
- **Context:** Windows has no IANA timezone database, so `zoneinfo.ZoneInfo("America/New_York")` fails without the `tzdata` package. pandas happens to pull it in, but `core/clock.py` imports `zoneinfo` directly.
- **Decision:** Declare `tzdata` explicitly in `pyproject.toml`. It is pinned in `uv.lock` like everything else.
- **Consequences:** DST rules come from the locked `tzdata` version, not the OS. Update it with the lock file.

## D-018 — `entry_timeout_s` is defined twice; the values must match
- **Date:** 2026-10-09 · **Status:** Accepted
- **Context:** `entry_timeout_s` appears both in `settings.yaml` (`strategy.entry`) and in `risk.yaml`.
- **Decision:** Config loading fails if the two values differ. `risk.yaml` is authoritative and is what the execution code reads.
- **Consequences:** No silent disagreement between the two files. Removing the duplicate key is a later cleanup that would change the settings schema.

## D-019 — Fee estimates (answers Q5)
- **Date:** 2026-10-09 · **Status:** Accepted
- **Context:** Alpaca is commission-free but passes regulatory fees through on sells. Paper accounts don't charge them, so the bot estimates them.
  - **SEC Section 31:** $20.60 per $1,000,000 of sale proceeds, effective 2026-04-04 (FY2026 order, Release 34-104909). No FY2027 rate is announced yet. It can change only 60 days after a full-year SEC appropriation passes, and FY2027 is currently funded by a stopgap through 2026-12-11.
  - **FINRA TAF:** $0 for trades from 2026-10-01 through 2026-12-31 (SR-FINRA-2026-021). From 2027-01-01 it returns to $0.000195 per share, capped at $9.79 per trade, held flat through 2028 (SR-FINRA-2026-020).
  - **FINRA CAT (Alpaca schedule):** $0.000003 per share on buys and sells, under $0.001 for this bot's sizes.
  - **Alpaca's charging method:** fees are totalled per day and account, then rounded up to the cent.
- **Decision:**
  - `fees:` in `settings.example.yaml`: `sec_fee_per_million_sold: 20.60`, `finra_taf_per_share_sold: 0.000195`, `finra_taf_max_per_trade: 9.79`.
  - The 2027 TAF rate is used through the 2026 pause, so estimates slightly overstate costs rather than understate them.
  - CAT is ignored as negligible.
- **Consequences:**
  - Re-check both rates when FY2027 SEC appropriations pass, and before relying on January 2027 TAF reporting.
  - A rate change is a fee-estimate change only, never a strategy change.
- **Sources:**
  - https://www.sec.gov/rules-regulations/fee-rate-advisories/2026-2
  - https://www.sec.gov/files/rules/other/2026/34-104909.pdf
  - https://www.finra.org/sites/default/files/2026-09/SR-FINRA-2026-021.pdf
  - https://www.finra.org/sites/default/files/2026-09/SR-FINRA-2026-020-91-FR-60427.pdf
  - https://alpaca.markets/support/regulatory-fees
  - https://files.alpaca.markets/disclosures/library/BrokFeeSched.pdf
  - https://docs.alpaca.markets/us/docs/paper-trading

## D-020 — alpaca-py 0.44.0 behavior the code must work around
- **Date:** 2026-10-09 · **Status:** Accepted (re-check on every alpaca-py upgrade)
- **Context:** Verified against the installed SDK source and docs.alpaca.markets for `trader doctor`; most of it also matters for P2.1.
  - **Paper detection:** the account API has no `is_paper` field, and the "PA" account-number prefix is undocumented. Paper keys only authenticate against `paper-api.alpaca.markets`, which `TradingClient(paper=True)` targets, so a successful call proves the keys are paper keys.
  - **No timeout:** alpaca-py passes no timeout to `requests`, so a stalled call can hang forever. It retries 429/504 itself (3 tries, 3 s apart).
  - **HTTP library:** it uses `requests`, not `httpx`, so respx can't intercept it.
  - **Lagging enums:** the SDK's enums trail the API. `AccountStatus` lacks `ACCOUNT_CLOSED_PENDING`, and `AssetClass` lacks several newer classes. Such payloads raise `pydantic.ValidationError` inside the SDK.
  - **Order list limit:** `get_orders` defaults to 50 results (max 500).
  - **Return types:** every method returns `Model | dict`, so callers must narrow with `isinstance`.
  - **Rate limits:** the free market-data plan allows 200 historical calls/min (docs), so `rest_rate_limit_per_min: 180` fits. The trading-API figure of 200/min appears only in a 2022 support article and is unconfirmed.
- **Decision:**
  - Mount a timeout `HTTPAdapter` on the client's (private, typed) `_session`.
  - Translate `APIError` (401/403 means bad credentials), `requests` errors, and SDK `ValidationError` into one probe error type, so nothing crashes the caller.
  - Always pass `limit=500` when listing open orders.
  - Test Alpaca-facing code through fakes at our own interface, or against a loopback stub via `url_override`.
- **Sources:** https://docs.alpaca.markets/us/reference/getaccount-1 · https://docs.alpaca.markets/us/reference/legacyclock · https://docs.alpaca.markets/us/reference/getallorders-1 · https://docs.alpaca.markets/us/docs/about-market-data-api · alpaca-py 0.44.0 `common/rest.py`, `trading/client.py`, `trading/enums.py`

## D-021 — `claude auth status` contract and subprocess isolation (partly answers Q4)
- **Date:** 2026-10-09 · **Status:** Accepted
- **Context:** Verified in the Claude Code docs for `trader doctor`:
  - **Output:** `claude auth status` prints JSON by default and exits 0 when logged in, 1 when not.
  - **`authMethod` values:** `none`, `claude.ai`, `oauth_token`, `api_key`, `api_key_helper` or `third_party`.
  - **`configDirectory`:** included from 2.1.268, naming the config directory actually used.
  - **Auth precedence:** cloud-provider switches, then `ANTHROPIC_AUTH_TOKEN`, then `ANTHROPIC_API_KEY`, then `apiKeyHelper`, then `CLAUDE_CODE_OAUTH_TOKEN`, then profile or federation variables (`ANTHROPIC_PROFILE`, `ANTHROPIC_FEDERATION_RULE_ID`, `ANTHROPIC_ORGANIZATION_ID`), then the `/login` credentials.
  - **Other endpoint overrides:** `ANTHROPIC_*_BASE_URL` variables also redirect requests.
  - **Unverified:** the docs don't say whether `CLAUDE_CONFIG_DIR` also relocates `~/.claude.json`.
- **Decision:**
  - `llm/claude_cli.build_claude_env` starts from an allowlist of OS variables (plus proxy and CA-bundle settings) instead of deleting known-bad names. Any credential or provider variable is excluded by construction, including ones Anthropic adds later. It then sets `CLAUDE_CONFIG_DIR` and the token.
  - Doctor requires `authMethod == oauth_token`, and requires `configDirectory` (when present) to equal `runtime/claude-config`, which proves the isolation took effect.
- **Consequences:**
  - `apiKeyHelper` lives in settings files, so the isolated config dir keeps the human's settings from switching billing.
  - Check the `~/.claude.json` question at G4a, together with the rest of Q4.
- **Sources:** https://code.claude.com/docs/en/cli-reference · https://code.claude.com/docs/en/authentication · https://code.claude.com/docs/en/env-vars

## D-022 — The calendar keeps Alpaca's settlement date (raises Q8)
- **Date:** 2026-10-09 · **Status:** Accepted
- **Context:**
  - Alpaca's `GET /v2/calendar` returns `date`, `open`/`close` as Eastern wall-clock "HH:MM", `session_open`/`session_close`, and `settlement_date`. It lists trading days only, for 1970–2029.
  - Settlement skips bank holidays on which NYSE trades: Columbus Day and Veterans Day. Example: a Friday 2026-10-09 trade settles Tuesday 2026-10-13, but "T+1 trading days" (RISK.md §4, A§4.1) gives Monday 2026-10-12. On such days that rule would let the ledger reuse sale proceeds a day early, which is a good-faith violation in a real cash account.
  - The v2 calendar is labelled "Legacy" but carries no deprecation notice; alpaca-py still uses it. A v3 calendar exists with RFC-3339 offsets.
- **Decision:**
  - Migration `0002` adds `trading_days.settlement_date`, and A§6 is updated to match.
  - `TradingCalendar.settlement_date(d)` returns Alpaca's date. `add_trading_days` stays pure trading-day arithmetic.
  - Each sync reports how many sessions don't settle on the next trading day. A sudden jump would signal a settlement-cycle change.
  - Queries outside the synced range raise `CalendarRangeError`, so the calendar fails closed.
  - A day Alpaca stops listing (an unscheduled closure, like 2025-01-09) is deleted on resync, unless it already has a run status; that is refused as a data conflict.
- **Consequences:** Which settlement rule the ledger uses is a human decision at P2.3 (Q8). Nothing uses settlement dates yet.
- **Sources:** https://docs.alpaca.markets/us/reference/legacycalendar · https://docs.alpaca.markets/us/reference/calendar-2 · NYSE/ICE 2025–2027 holiday calendars · https://www.nyse.com/markets/hours-calendars

## D-023 — Market-data REST client: httpx, not alpaca-py
- **Date:** 2026-10-09 · **Status:** Accepted (human approved in the Phase 1 plan)
- **Context:**
  - A§4.2 says `AlpacaMarketData` wraps alpaca-py, but P1.2 asks for respx-mocked tests. alpaca-py uses blocking `requests` with no timeout (D-020).
  - Verified API behavior:
    - **Bars:** a bar's `t` is its start; daily bars are stamped 00:00 ET. `limit` (max 10,000) is a total across all symbols, so pagination follows `next_page_token`.
    - **SIP on the free plan:** an `end` within the last 15 minutes gets HTTP 403 "subscription does not permit querying recent SIP data". Omitting `end` defaults it to 15 minutes ago.
    - **Failed auth:** an **HTML** 401 page, not JSON.
    - **Rate limiting:** a 429 response carries `X-RateLimit-Reset` (UNIX epoch) and **no `Retry-After`**.
    - **Corporate actions:** come from `GET data/v1/corporate-actions`; the old trading-API endpoint is deprecated. Splits carry `ex_date` (effective) and `old_rate`/`new_rate`.
    - **Assets:** `GET /v2/assets` can't filter on tradability server-side.
- **Decision:**
  - **`data/alpaca_rest.py`** uses `httpx.AsyncClient`:
    - one `RateLimiter` per host, 180/min with a burst of 10 (at most 190 in any 60 s, against a limit of 200);
    - symbols in batches of `rest_batch_symbols`, at most 4 batches in flight;
    - prices parsed exactly with `json.loads(parse_float=Decimal)`;
    - retries: up to 3 with jittered backoff for 429, 5xx and transport errors. A 429 waits until `X-RateLimit-Reset` and pauses the whole host.
  - **Errors:** `AlpacaAuthError` (401), `AlpacaForbiddenError` (403), and its subclass `SipRecentError`, matched on the message text because the documented error codes disagree.
  - **SIP queries omit `end`.**
  - **The limiter treats float dust as a whole token** and never waits under 1 ms. A fake-clock test spun forever on 1e-17 s waits before this fix.
  - **`pytest-timeout`** is now a dev dependency (60 s per test), so a hang fails instead of stalling the suite.
- **Consequences:** alpaca-py remains only for trading (P2.1). Tests mock Alpaca at the HTTP layer with respx.
- **Sources:** https://docs.alpaca.markets/us/reference/stockbars · https://docs.alpaca.markets/us/docs/market-data-faq · https://docs.alpaca.markets/us/reference/corporateactions-1 · https://docs.alpaca.markets/us/reference/get-v2-assets-1 · https://docs.alpaca.markets/us/docs/about-market-data-api

## D-024 — The movers screener can't drive the 09:00 scan (bears on Q1)
- **Date:** 2026-10-09 · **Status:** Accepted
- **Context:**
  - **The screener lags until the open.** Alpaca's movers screener (`GET data/v1beta1/screener/stocks/movers`) "resets at market open and shows the previous day's movers until then". At the 09:00 pre-market scan (A§3) it would therefore list *yesterday's* movers.
  - **Plan access is undocumented:** whether the free plan includes the screener isn't stated.
  - **Snapshots behave similarly before the open:** in Alpaca's doc example, a snapshot's `dailyBar` is still the previous session's bar.
- **Decision:**
  - `AlpacaRest.movers()` exists and returns `None` on 403/404.
  - Doctor's `data.movers` check reports whether the plan has it.
  - The pre-market candidate scan (Phase 4) will rank gaps from snapshots (pre-market latest trade against the previous close), whatever the screener's availability. It must take the previous close from the right bar: `dailyBar` before the open, `prevDailyBar` after it.
- **Consequences:** A§3's "movers endpoint if available" is superseded for the 09:00 scan. Q1 now only needs doctor's answer for the record.
- **Sources:** https://docs.alpaca.markets/us/reference/movers-1 · https://docs.alpaca.markets/us/reference/stocksnapshots-1

## D-025 — Indicator definitions (ATR14 and the opening-range features)
- **Date:** 2026-10-09 · **Status:** Accepted (human chose the ATR definition in the Phase 1 plan)
- **Context:** S§3.1 says "Wilder ATR over the 14 sessions before t". Wilder's recursive smoothing depends on how much history it starts from, so the phrase is ambiguous. The feature formulas (A§5, A§8) were never written down.
- **Decision** (`data/indicators.py`; days t−14…t−1 are the 14 sessions before t):

  | Feature | Definition |
  |---|---|
  | `ATR14` | Mean of the 14 Wilder true ranges of t−14…t−1, each against the previous session's close (needs 15 daily bars). This equals Wilder's ATR at its seed. Quantized to $0.0001, half-even. |
  | `ADV14` | Floor of the mean daily volume of t−14…t−1 |
  | `or_vol_avg14` | Mean opening-range volume of t−14…t−1 (live feed) |
  | `rel_vol` | `or_volume / or_vol_avg14`; `None` when the baseline is 0 |
  | `or_range_atr` | `(or_high − or_low) / ATR14` |
  | `or_return_pct` | `(or_close − or_open) / or_open × 100` |
  | `gap_pct` | `(or_open − prev_close) / prev_close × 100` |
  | `atr_pct` | `ATR14 / prev_close × 100` |

- **Consequences:**
  - Values are reproducible from exactly 15 daily bars and can differ slightly from charting platforms, which smooth over longer history.
  - Changing the definition during an evaluation epoch would start a new epoch (S§9).

## D-026 — How the universe is built (S§3.1 details the spec left open)
- **Date:** 2026-10-09 · **Status:** Accepted (human approved in the Phase 1 plan)
- **Decision** (`data/universe.py`, `trader universe build|or-baseline`):
  - **Split-adjusted history.** Daily SIP bars and live-feed opening-range bars are requested with `adjustment=split`, so a split inside the window doesn't distort ATR14, ADV14 or relative volume.
    - Cached `or_bars` for a symbol with a split anywhere from t−15 to t are refetched on every build until the split leaves the window.
    - The S§3.1 exclusion still applies to splits on t or t−1, where t−1 is the previous session.
  - **No prints counts as zero.** A session with no live-feed 09:30–09:35 bar is stored with volume 0 and no prices. Bars only exist when trades happened, so that session's IEX opening-range volume really was 0, and it still counts as history.
  - **Exclusion order.** Asset-level failures are counted but not stored: asset class, not tradable, exchange, symbol characters. Each remaining candidate gets one row in `universe_daily` with its first failing reason, in this order:
    - `split`
    - `stale_prev_close` (no SIP bar on t−1)
    - `insufficient_history` (fewer than 15 daily bars)
    - `adv` (ADV14 < 1,000,000)
    - `atr` (ATR14 ≤ $0.50)
    - `insufficient_or_history`

    `stale_prev_close` comes before `insufficient_history` because it is the more specific cause.
  - **Baselines only where needed.** OR baselines are fetched only for symbols that passed the daily filters, with one 5Min request per missing session and batch, keeping the bar stamped 09:30.
  - **Calendar refresh.** `universe build` syncs the trading calendar first (one request).
- **Known risk (late corporate actions):** Alpaca says corporate actions "may show up late", and which date its filter uses is undocumented. The split query is widened a week past both ends of the window. Even so, a split taking effect on t may be missing at build time. The 09:35 scan (P3.1) should treat an implausible gap against `prev_close` as suspect rather than trust it.
- **Sources:** https://docs.alpaca.markets/us/reference/corporateactions-1 · https://docs.alpaca.markets/us/reference/stockbars · https://docs.alpaca.markets/us/docs/market-data-faq

## D-027 — Our own asyncio WebSocket client (partly answers Q2)
- **Date:** 2026-10-09 · **Status:** Accepted (human approved in the Phase 1 plan)
- **Context:**
  - **Problems with alpaca-py's stream classes (0.44.0):**
    - `run()` calls `asyncio.run()`, so it can't be used inside our event loop.
    - Subscribing from a coroutine deadlocks: it blocks on `run_coroutine_threadsafe(...).result()`.
    - Error messages that arrive mid-stream (e.g. 405) are logged but never surfaced.
    - Startup busy-waits on `asyncio.sleep(0)`.
    - It still uses the deprecated `websockets.legacy` client.
  - **Protocol, verified in Alpaca's docs:**
    - **Handshake:** the server greets with `connected`, and the client must authenticate within 10 s. Every message is a JSON array, and data messages are dispatched on `T`.
    - **Subscriptions:** an ack always echoes the full current set. A 405 rejects the whole request and keeps the old set.
    - **Error codes:** 400–410 and 500, including 402 (auth failed), 405 (symbol limit), 406 (connection limit) and 409 (wrong plan).
    - **Connections:** one connection per endpoint on most plans. Half-dead sessions are cleared after about 90 s, so a quick reconnect can hit 406.
    - **Bars:** a minute bar arrives just after its minute ends and is skipped when there were no trades. `updatedBars` correct earlier bars that had late trades.
    - **Test stream:** `/v2/test` streams the fake symbol `FAKEPACA` at all hours.
- **Decision:**
  - **`data/ws.py: AlpacaStream`** wraps `websockets` 17 (now a declared dependency). It handles:
    - authentication;
    - subscribe/unsubscribe that waits for the ack (unsubscribes first, so the set never briefly exceeds the plan limit);
    - replaying subscriptions after every reconnect;
    - backoff of 1, 2, 4 … 30 s, and at least 30 s after a 406;
    - stopping for good on 402 or 409;
    - a silence watchdog that reconnects after 60 s without a message while the market is open and symbols are subscribed, because sockets can stay open but stop delivering.
  - **`data/stream.py: MarketDataHub`** subscribes trades, quotes and bars for at most `stream_symbol_limit` symbols, checked locally first. It fans events out to consumer queues and keeps the latest trade and quote plus event ages for R-016. After a reconnect it backfills missed minute bars over REST, tagged `source="backfill"`.
  - **`trader stream --feed test`** makes the stream verifiable outside market hours.
- **Still unconfirmed (Q2):**
  - whether the 30-symbol limit counts trades and quotes separately;
  - whether IEX sends `statuses` (trading halts);
  - whether the news stream may run alongside the stock stream.

  The hub surfaces a 405 as an exception and the news hub falls back to polling on 406. Running `trader stream` with real keys answers the first question from the logged ack.
- **Sources:** https://docs.alpaca.markets/us/docs/streaming-market-data · https://docs.alpaca.markets/us/docs/real-time-stock-pricing-data · https://docs.alpaca.markets/us/docs/streaming-real-time-news · https://docs.alpaca.markets/us/docs/market-data-faq · alpaca-py issues #491, #618, #740, #762

## D-028 — News and SEC filings
- **Date:** 2026-10-09 · **Status:** Accepted (human approved in the Phase 1 plan)
- **Context:**
  - **Alpaca news** (Benzinga) has a REST endpoint (`limit` ≤ 50, paginated) and a WebSocket at `/v1beta1/news`. Whether that WebSocket may run alongside the stock stream on the free plan is unconfirmed (Q2).
  - **EDGAR's `company_tickers.json`** maps tickers to numeric CIKs and writes class shares with a hyphen (`BRK-B`). One CIK can have several tickers.
  - **Submissions JSON:** `acceptanceDateTime` in the submissions JSON is wrong by the UTC offset for many filings. The `.hdr.sgml` header's `<ACCEPTANCE-DATETIME>` is Eastern time and reliable.
  - **SEC fair access:** at most 10 requests/s per user and a declared User-Agent. SEC answers **403** both for rate blocks and for a missing or bad User-Agent, then expects about 10 minutes of quiet.
- **Decision:**
  - **`data/news.py: NewsHub`** has three modes:
    - **Startup:** backfills over REST, then streams `["*"]` and upserts each article into `news`. A correction updates the text but keeps the first `received_at`.
    - **Fallback:** after the stream is refused with 406 twice (one 30 s retry), it switches to REST polling every 60 s with a 5-minute overlap.
    - **Lookups:** `recent(symbols, since)` over-selects by 2 hours and filters exactly in Python. ISO strings with different UTC offsets don't sort chronologically across a DST change.
  - **`data/edgar.py: EdgarClient`**:
    - **Requests:** sends the User-Agent from `.env` and refuses the env.example placeholder. One limiter at `edgar.max_rps` covers both SEC hosts.
    - **Errors:** retries 5xx and transport errors. A 403 or 429 refuses every EDGAR call locally for 10 minutes.
    - **Ticker map:** cached once a day under `runtime/edgar/`.
    - **Filings:** keeps flagged forms whose `filingDate` is inside the lookback window. Acceptance times come from `.hdr.sgml`, fetched once per accession and cached in `filings`.
  - **Dev commands:** `trader news --symbols X` and `trader filings --symbol X` serve the done-when lookups. `trader stream --news` runs both streams at once and reports which mode news ended in, which answers part of Q2.
- **Sources:** https://docs.alpaca.markets/us/reference/news-3 · https://docs.alpaca.markets/us/docs/streaming-real-time-news · https://www.sec.gov/search-filings/edgar-application-programming-interfaces · https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data

## D-029 — Daily recordings: gzip JSON lines that survive a crash, Parquet tables
- **Date:** 2026-10-09 · **Status:** Accepted
- **Decision** (`data/recorder.py`, per A§4.2):
  - **Layout:** `runtime/recordings/YYYY-MM-DD/` holds four files:
    - `stream.jsonl.gz`: every hub event, tagged `kind`: trade, quote or bar;
    - `news.jsonl.gz`;
    - `or_scan.parquet` (written from P3.1);
    - `universe.parquet` (written by `trader universe build`).
  - **Encoding:** records are the models' JSON dumps, so Decimals and timestamps are exact strings and the models re-validate them unchanged.
  - **Feeding:** the recorder is fed as a hub consumer (`consume`), so the hubs don't know it exists.
  - **Appending:** JSON-lines files are opened in append mode, which adds one gzip member per open, e.g. after a restart. They are flushed with a sync point every 100 records and on close.
  - **Atomic tables:** Parquet files are written to `*.partial` and then renamed into place.
- **Finding:** Python's `gzip.open` cannot read a member that lacks its trailer, which is exactly what a crash leaves. Its buffered reader raises `EOFError` and discards output it had already decompressed. `read_jsonl` therefore decompresses with `zlib` (`wbits=31`) member by member. It keeps everything up to the last sync point and skips a torn final line. Tests cover an unclosed file and one with its tail cut off.
- **Consequences:** a crash loses at most the last 100 stream events. Replay (P3.8) reads with `read_jsonl` and `read_table`.

## D-030 — Trading API: httpx plus our own trade-updates stream (answers Q3)
- **Date:** 2026-10-09 · **Status:** Accepted (human chose it in the Phase 2 plan). Supersedes D-023's "alpaca-py remains only for trading" and A§4.6's alpaca-py note.
- **Context:**
  - **alpaca-py resends order submits.** It retries 429 and 504 responses on every method, order submits included (`common/rest.py`: 3 tries, 3 s apart). A 504 can arrive after Alpaca accepted the order, so it can submit twice; Alpaca says never to resend before confirming. It also sends prices as floats, and its trade stream has the event-loop problems in D-027.
  - **Verified Alpaca behavior** (docs fetched 2026-10-09):
    - **OTO:** a limit entry plus `stop_loss {stop_price}`; tif day or gtc; no extended hours. The stop must be at least $0.01 below the "base price", which means both the entry limit and the current price.
    - **Legs** get Alpaca-generated client_order_ids. `group_id` and `group_role` (`entry`, `stop_loss`) tie them to the entry; the stream doesn't fill in `legs`.
    - **The stop leg is inactive until the entry fills completely**, and cancelling any order in the group cancels the rest (D-032). OTO legs can't be replaced; we never move stops.
    - **Sub-penny:** at or above $1.00 two decimals, below $1.00 four; anything else gets 422 code 42210000.
    - **client_order_id:** up to 128 characters now (48 in older docs and the FIX spec; we keep 48). A duplicate gets 422 code 40010001. Lookup: `GET /v2/orders:by_client_order_id`.
    - **A 403 can mean** insufficient buying power or shares (code 40310000), a wash trade, or bad keys, so errors are classified by the body's code and message.
    - **Wash-trade protection** (paper too): an open buy blocks any market or stop sell on the symbol, and an open stop sell blocks any buy. OTO orders are exempt.
    - **A working stop holds its shares** (`qty_available`). Selling them fails with 403 "insufficient qty" until the stop is confirmed cancelled; a cancel's 204 only means requested.
    - **`DELETE /v2/positions/{symbol}` does not cancel open orders first** (Q3).
    - **After 16:00** day orders, stop legs included, have expired, and a new regular order is queued for the next open.
    - **Rate limit:** 200 requests/min per account. The trading API's rate headers are undocumented.
    - **Trade stream:** `wss://paper-api.alpaca.markets/stream`. Auth `{"action":"auth","key","secret"}` gets `{"stream":"authorization","data":{"status":"authorized"}}`; `{"action":"listen","data":{"streams":["trade_updates"]}}` gets a `listening` ack. Paper sends binary frames. Fill events carry `execution_id`, `qty` (this execution), `price` and `position_qty`. Errors are `{"action":"error"}` followed by a close. Nothing is replayed after a reconnect. alpaca-py sends an older auth form; G2 confirms the docs' form.
- **Decision:**
  - **`data/alpaca_rest.AlpacaHttp`** holds the request policy for both clients. GET and DELETE retry 429, 5xx and transport errors. A POST is resent only after a 429, which Alpaca refuses before acting. A timeout, transport error or 5xx on a POST raises `AlpacaUncertainError`.
  - **`execution/alpaca_broker.AlpacaBroker`**, one per book, paper host only, 180 requests/min per account.
    - Prices and quantities go out as exact strings and come back as Decimal. A fractional quantity is an anomaly (`BrokerDataError`).
    - Submit errors: `AlpacaUncertainError` → `SubmitUncertain`; 422 code 40010001 → `DuplicateClientOrderId` (look it up and adopt it); any other 4xx → `OrderRejected` with Alpaca's code; 401 → `BrokerAuthError`.
  - **`execution/trade_stream.AlpacaTradeStream`** follows the docs' protocol and accepts text or binary frames. It reconnects with 1…30 s backoff, stops for good on an `unauthorized` reply, and announces every connection, the first included, so the owner resyncs over REST. The broker turns an unreadable update into a resync rather than dropping it.
- **Consequences:**
  - alpaca-py is left only in doctor's read-only account probe (D-020). Tests mock Alpaca with respx and a fake trade-stream server.
  - `execution/sim_broker.SimBroker` (P2.2) models these rules for the scenario tests. It also treats a held OTO leg as an open stop sell for the wash-trade check, which the docs leave open. That choice is conservative and never matters in practice, because R-008 allows one entry per symbol per day.
- **Sources:** https://docs.alpaca.markets/us/docs/orders-at-alpaca · https://docs.alpaca.markets/us/reference/postorder · https://docs.alpaca.markets/us/reference/getallorders-1 · https://docs.alpaca.markets/us/reference/getallopenpositions · https://docs.alpaca.markets/us/docs/user-protection · https://docs.alpaca.markets/us/docs/websocket-streaming · https://alpaca.markets/learn/how-to-fix-common-trading-api-errors-at-alpaca · https://alpaca.markets/support/usage-limit-api-calls · alpaca-py 0.44.0 `common/rest.py`, `trading/stream.py`

## D-031 — No PDT limit; under $2,000 an account has 1x buying power; Alpaca has no cash accounts
- **Date:** 2026-10-09 · **Status:** Accepted (extends D-003)
- **Context:**
  - **PDT is gone.** FINRA replaced the pattern-day-trader rule with intraday margin standards, effective 2026-06-04 (Regulatory Notice 26-10; SEC approval 2026-04-14). Alpaca no longer rejects orders for PDT. On 2026-07-06 it removed `pattern_day_trader`, `daytrade_count`, `daytrading_buying_power` and the `pdt_check`/`dtbp_check` settings.
  - **Under $2,000 equity** an account gets 1x buying power (`buying_power = cash`) and can't short. Open buys reserve buying power; sells free it only once they fill.
  - **Alpaca offers no cash accounts.** Every account is a margin account, and `cash` rises as soon as a sale fills, so the broker lets unsettled proceeds be spent.
  - **Paper fills** are simulated against the NBBO, with random partial fills on about 10% of orders and no fees, slippage or queue position.
- **Decision:**
  - Nothing counts day trades or reads the removed fields.
  - Only our ledger enforces settlement (D-034), as D-003 intended.
  - G7's "live cash account" will really be an Alpaca limited-margin account under $2,000, with 1x buying power. Revisit the wording at G7.
- **Sources:** https://www.finra.org/rules-guidance/notices/26-10 · https://docs.alpaca.markets/us/docs/the-intraday-margin-rule · https://docs.alpaca.markets/us/docs/understanding-the-new-intraday-margin-rule · https://docs.alpaca.markets/us/changelog/2026-06-03-pdt-651df23 · https://docs.alpaca.markets/us/docs/margin-and-short-selling · https://docs.alpaca.markets/us/docs/account-plans · https://docs.alpaca.markets/us/docs/paper-trading

## D-032 — Partial fills: one protection routine for every position without a stop
- **Date:** 2026-10-09 · **Status:** Accepted (human approved in the Phase 2 plan; implemented in P2.6)
- **Context:**
  - An OTO stop leg protects nothing until its entry has filled completely. Partially filled shares are unprotected while the entry is open, and cancelling the entry cancels its legs. Paper partially fills about 10% of orders at random.
  - Wash-trade protection rejects a stop sell while the buy is open, so no stop can be added before the entry is final.
  - R-020 allows at most `unprotected_position_max_s` (5 s) without a stop; S§3.3's entry timeout is 10 s.
  - A leg's status after its entry fills is undocumented; Alpaca staff once said it "remains held".
- **Decision:**
  - **Protected** means our working sell stops cover the broker's position quantity. Working statuses are new, accepted, pending_new and partially_filled. A `held` leg counts only once its entry has completely filled (`execution/broker.stop_is_working`).
  - **One routine covers every unprotected position,** whatever cancelled its entry: timeout, late attempt, recovery, last_entry or a halt. Its clock starts at the broker's first-fill time, so after a restart it acts at once.
  - **Sequence:**
    1. Give a partially filled entry 1 s to finish, then cancel the remainder.
    2. Wait until every buy on the symbol is final, then re-read the position.
    3. If the last price is at or below the planned stop, sell now (R§5 step 1).
    4. Otherwise place a standalone stop for the position at the planned stop.
  - **Fallback:** if no stop is working 5 s after the first fill, cancel the symbol's orders, wait until they are final, re-read the position, and market-sell `qty_available`. Raise a P1, and keep trying until flat.
  - **Polling:** open entries are polled every 1 s, so a half-dead stream can't hide a fill.
  - **Severity:** a partial fill that ends up protected is a P3 note; P1 is for a protection failure.
- **Consequences:**
  - S§3.3's 10 s timeout is shortened for partially filled entries only.
  - G2's `--marketable` smoke run records a leg's real status after a fill.

## D-033 — Uncertain submits: an entry is never resent
- **Date:** 2026-10-09 · **Status:** Accepted (human approved in the Phase 2 plan). Supersedes A§10's resubmit rule for entries.
- **Context:** A§10 resubmits an uncertain order with `attempt+1` when a lookup finds nothing. But the slowdown that caused the timeout can also delay attempt 1, which then fills. With a marketable limit, cancelling it on sight can't undo that fill, and the book would hold the position twice, spending settled cash it doesn't have.
- **Decision:**
  - **An uncertain entry is never resubmitted.** It stays pending, holding its cash and counting in R-006, R-007 and R-008, until a lookup or a reconciliation resolves it.
    - Still unknown after `entry_timeout_s` plus one reconcile, it is marked `lost`.
    - If it appears later anyway, it is adopted, then cancelled or protected.
  - **Exits and stops are resubmitted** once two lookups, at once and 1 s later, find nothing. That is safe: the account can't short, and a working stop holds the shares, so a second sell is rejected.
  - **Exit and stop attempt numbers come from the journal.** `DuplicateClientOrderId` means "look it up and adopt it", never a rejection.

## D-034 — Ledger rules: Alpaca's settlement date, buys counted on their trade date (answers Q8)
- **Date:** 2026-10-09 · **Status:** Accepted (the human chose Alpaca's settlement date in the Phase 2 plan)
- **Context:**
  - **RISK.md §4 read literally** counts "settled entries up to and including that day" on a T+1 basis. A Friday buy would then count only once it settles. Across a bank holiday (Fri 10-09 settles Tue 10-13), Monday would show the whole $1,000 as available, though Friday's buy had already spent half of it.
  - **Paper charges no fees.** Broker cash therefore drifts away from a ledger that books fee estimates, past the $1 tolerance within a few weeks.
  - **Seeding on a trading morning** must still give that day a start-of-day equity.
- **Decision** (`risk/ledger.py`, migration 0003):
  - **Settlement dates** are Alpaca's `settlement_date` (D-022), which skips bank holidays.
  - **Buys** use up settled cash on their trade date.
  - **Sales** add their proceeds, net of estimated fees, on the settlement date. The fee row settles with the sale, which keeps R§4's own example exact.
  - **Deposits and adjustments** count from the start of their own day.
  - **available** = start-of-day settled cash − today's buys − the unfilled part of open entries at their limit.
  - **Start-of-day equity E** = earlier trades, plus deposits and adjustments up to today. The book is flat at the open, so that is its cash.
  - **Fee estimates** are SEC plus TAF, rounded up to the cent per sale, and carry a `fee-est:` ref. Paper's EOD cash check leaves them out; a live check would compare everything.
  - **Seeding:** a book's ledger is seeded once, from a flat broker account with no open orders, and refuses to answer until then.
  - **Booking a fill twice is impossible:** a unique `(book, ref)` index enforces it.
- **Consequences:**
  - Around Columbus Day and Veterans Day, a book waits a day longer for its proceeds.
  - A hypothesis test across Oct–Nov 2026 holidays shows that available cash never goes negative, and that a day's buys never exceed that day's start-of-day settled cash.

## D-035 — Risk-gate conventions (details RISK.md §3 left open)
- **Date:** 2026-10-09 · **Status:** Accepted (human approved in the Phase 2 plan)
- **Decision** (`risk/gate.py`):
  - **One snapshot, every rule.** `RiskGate.check(intent, state)` judges one frozen `GateState` and lists every violation in rule order.
    - Only R-009 changes anything: it lowers the quantity to fit the settled cash, and the later rules judge the lowered quantity.
    - If not even one share fits, R-009 rejects alone, and the other rules judge the requested quantity.
  - **Atomic per book.** The caller builds the state, runs the check, and journals the check and the pending order under one per-book lock (P2.6). R-005 reads halts fresh from the journal. Two signals in the same second therefore can't both pass.
  - **What counts as an entry:**
    - R-006: distinct symbols with a position or an open or uncertain entry.
    - R-007: today's entries that filled, are open, or are uncertain. An entry cancelled without any fill doesn't count.
    - R-008: any entry the broker accepted today, or an uncertain one, refuses the symbol. This backs up S§3.3's one-shot trigger.
    - Smoke-test entries (`smoke-…` signals) are left out of R-007, R-008 and every statistic.
  - **R-002** needs `OrderIntent.side` (default buy). Exits never pass through the gate.
  - **R-015:**
    - realized loss is today's net realized P&L, fee estimates included, when it is negative;
    - open risk is each position's quantity × (entry − stop), plus each open entry's unfilled quantity × (limit − stop).
  - **R-019:** ledger equity is ledger cash before fee estimates plus marked positions, which is comparable to broker equity. An unknown broker equity fails.
  - **R-001** passes paper. Live needs a live-gate flag that nothing sets before Phase 7.
  - **Defense in depth:** every limit is `min(config, cap)` from risk/caps.py, and the day's size multiplier is clamped to at most 1.
  - **R-016** fails when the symbol has no trade or quote at all. **R-017** passes an unknown halt status, since there is no status feed yet (Q2).
  - **Day times** (`core/calendar.day_times`): last entry = min(15:30, close − 30 min); flatten = min(15:55, close − 5 min); flat by close − 2 min.
  - **Coverage:** pytest-cov (a dev dependency) enforces 100% branch coverage of `gate.py`:
    `uv run pytest tests/unit/test_gate.py --cov=trader.risk.gate --cov-branch --cov-fail-under=100`

## D-036 — One flatten routine, halts as rows, and the kill switch
- **Date:** 2026-10-09 · **Status:** Accepted (human approved in the Phase 2 plan)
- **Context:**
  - **Alpaca's behavior (D-030):** a stop holds its shares; a cancel's 204 only means requested; `DELETE /v2/positions` doesn't cancel orders; and after 16:00 day orders have expired and new sells only queue for the next open.
  - **Flatten logic in three places** (the adapter's `close_position`, the CLI, the engine) would drift apart. The CLI and a running engine could also fight: the engine's protection loop would re-place a stop the CLI had just cancelled.
  - **A single `book_state.halt_reason` loses halts.** A manual halt overwriting a daily-loss halt would let `resume` lift both.
- **Decision:**
  - **`execution/service.flatten_account` is the only flatten.** It replaces A§4.6's adapter-level `close_position`. The kill switch, `trader flatten`, `trader halt --flatten`, and later the engine and the watchdog all use it. It works over REST alone, so it runs without the engine:
    1. Cancel every open order and wait until each is final. Open orders are listed with `nested=false`, so a working leg of a filled entry shows up.
    2. Re-read the positions and market-sell `qty_available` with the next `exit` id from the journal. A rejection is journaled, and the next loop tries again with a new id.
    3. Journal every order it touched until each is final, since there is no stream. A stop that fills during a slow cancel is booked this way.
    4. Repeat until flat or the deadline.
  - **Outside regular hours it sells nothing,** because a sell would only queue, and it cancels nothing that protects. Each position keeps or gets a GTC stop at its planned stop, the caller raises a P1, and the next boot flattens at 09:30:05 (A§3). This keeps invariant 3 overnight.
  - **Halts are rows** in a `halts` table (migration 0003), each with a scope:
    - `until_resume`;
    - `session`: daily loss and LLM degradation, which expire at the next open;
    - `marker`: `flattening`, a 30 s flag.
    Any active halt rejects entries (R-005). `book_state.halted` and `halt_reason` become a summary.
  - **`trader resume`** lifts only the `until_resume` halts. It needs typed confirmation (`RESUME A`), refuses while `runtime/KILL` exists, and needs `--ack-drawdown` for a drawdown halt.
  - **`trader flatten`** halts the book (manual) before flattening, and keeps a `flattening` marker alive while it works. A running engine therefore stands down its protection loop (P2.6) instead of fighting. If the command dies, the marker lapses within 30 s. A book still not flat raises a P1 incident and the command exits 1.
  - **The kill switch** (`risk/killswitch.py`) checks for `runtime/KILL` every second. When it appears, every book gets a `kill_switch` halt and the flatten runs, retried every 30 s while any book isn't flat. Deleting the file doesn't resume trading.
  - **Incidents** (`memory/incidents.py`) are journaled and logged. Push alerts come with `notify.py` (P3.7).
  - **The breaker checks** (daily loss, drawdown, losing streak, staleness) are pure functions in `risk/breakers.py`. The EOD drawdown and losing-streak wiring moves to Phase 3, which writes `daily_stats`.

## D-037 — ExecutionService: protection, reconciliation and incidents
- **Date:** 2026-10-09 · **Status:** Accepted (human approved in the Phase 2 plan)
- **Decision** (`execution/service.py`, `execution/reconcile.py`):
  - **One monitoring loop per book,** on the injected clock. It:
    - polls open entries every second over REST, whatever the stream says;
    - re-reads positions, then open orders, every second while anything is in flux and every 5 s otherwise;
    - runs the protection routine (D-032) and checks the daily loss;
    - reconciles every 60 s and after every stream resync.
    It logs any error and keeps going, so one bad payload can't end protection.
  - **The stream journals only our orders:** those the journal already knows by id, and their legs by group. Someone else's order is never adopted; it triggers a reconcile instead.
  - **Reconciliation:**
    1. Catch up on the day's orders through `records.py`, ours only.
    2. Compare positions and open orders with the journal.
    3. Act only if the difference shows again on a pass 5 s later; a resync can't shortcut this.
    4. Then halt both books. Positions that differ are a P1; differing orders alone are a P2.
  - **Every P1** (a protection failure, a position mismatch) records an incident, halts the book until resumed (`p1_incident`), and flattens it, then keeps flattening while shares remain.
  - **The daily-loss breaker** halts the book until the next open (`daily_loss`), records a P2, and flattens.
  - **After a restart** the service adopts journaled entries the broker knows and keeps chasing the uncertain ones. Shares never seen under a stop count as bare since their first fill, so a naked partial found after a restart is handled at once: here, past the 5 s limit, a P1 and a flatten.
  - **Moved to Phase 3** (noted under P3.4 and P3.5 in PLAN.md):
    - `trades` rows;
    - the EOD drawdown and losing-streak wiring;
    - a `broker_rejected` reason code for the books.
- **Consequences:**
  - Scenario tests run the real journal, SimBroker and the gate under SimClock.
  - The test harness moves simulated time only once journal reads and writes are idle and the stream queue is empty. That made the scenarios deterministic and about 50× faster than fixed real-time waits, which cost about 15 ms each on Windows.

## D-038 — `trader smoke paper` goes through the real gate and service
- **Date:** 2026-10-09 · **Status:** Accepted (human approved in the Phase 2 plan)
- **Decision** (`execution/smoke.py`):
  - **The real path.** The 1-share order in Book A's account goes through `RiskGate` and `ExecutionService`, like any entry, so invariant 1 holds literally and G2 tests the whole path. The command only reads from the broker directly.
  - **Prices:**
    - default: a limit at 95% of the bid, which rests;
    - `--marketable`: the ask + 0.2%;
    - the stop: 2% under the limit;
    - the symbol must trade under $50 (default `F`).
  - **When it runs:** inside the entry window (09:35:03–15:30 ET) on a trading day.
  - **Ids:** the entry's client order id uses a `smoke` leg (`A-YYYYMMDD-SYM-smoke-N`), so it can never take a strategy entry's id. Its signal id starts `smoke-`, so it never counts as a strategy entry or in stats (D-035).
  - **First use** seeds Book A's ledger from the broker's cash, and only from a flat account (D-034).
  - **What G2 records:** the stop leg's status after a fill (`held` or `new`), the stream's frames, and the auth reply.
- **Consequences:** tests drive both flows against SimBroker. The real command is never run by Claude Code.

---

## Open questions to resolve during the build (record answers as new entries)

- **Q1 (P1.2):** Is Alpaca's `movers` (top market movers) endpoint available on the free plan? If not, use the IEX snapshot gap scan. **Partly answered: D-024** (the 09:00 scan uses snapshots either way); doctor's `data.movers` check records availability once keys exist.
- **Q2 (P1.5):** The concurrent market-data WebSocket connection limit on the free plan, and whether a trading-status/halts channel is available on the IEX feed.
- **Q3 (P2.1):** Exact alpaca-py request classes for OTO with a `stop_loss` leg; confirm that close-position cancels the related open orders. **Answered: D-030** (the trading client uses httpx, not alpaca-py; `DELETE /v2/positions/{symbol}` does not cancel open orders, so flattening cancels first).
- **Q4 (G4a):** Does `--tools ""` work with `--json-schema`? What are the envelope fields and the exit codes on usage-limit or auth errors?
- **Q5 (P0.2):** Current SEC Section 31 fee rate and FINRA TAF per share, for fee estimates (set in `fees:`). **Answered: D-019.**
- **Q6 (P1.4):** Typical count of eligible symbols, and the IEX OR-volume distribution, to sanity-check the liquidity guards.
- **Q7 (P1.2):** Can the human's Alpaca account type fetch SIP historical bars (older than 15 min)? If not: open a regular account, or record a new decision to fall back to IEX history with rescaled ADV thresholds and IEX-based shadow outcomes (noisier). **Doctor's `data.sip_history` check answers this once keys exist.**
- **Q8 (P2.3):** Should the cash ledger settle on Alpaca's `settlement_date`, which skips bank holidays (D-022), or on T+1 trading days as RISK.md §4 says? Alpaca's date matches real cash-account behavior; the human decides. **Answered: D-034** (the human chose Alpaca's settlement date).
