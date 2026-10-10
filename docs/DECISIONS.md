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

---

## Open questions to resolve during the build (record answers as new entries)

- **Q1 (P1.2):** Is Alpaca's `movers` (top market movers) endpoint available on the free plan? If not, use the IEX snapshot gap scan. **Partly answered: D-024** (the 09:00 scan uses snapshots either way); doctor's `data.movers` check records availability once keys exist.
- **Q2 (P1.5):** The concurrent market-data WebSocket connection limit on the free plan, and whether a trading-status/halts channel is available on the IEX feed.
- **Q3 (P2.1):** Exact alpaca-py request classes for OTO with a `stop_loss` leg; confirm that close-position cancels the related open orders.
- **Q4 (G4a):** Does `--tools ""` work with `--json-schema`? What are the envelope fields and the exit codes on usage-limit or auth errors?
- **Q5 (P0.2):** Current SEC Section 31 fee rate and FINRA TAF per share, for fee estimates (set in `fees:`). **Answered: D-019.**
- **Q6 (P1.4):** Typical count of eligible symbols, and the IEX OR-volume distribution, to sanity-check the liquidity guards.
- **Q7 (P1.2):** Can the human's Alpaca account type fetch SIP historical bars (older than 15 min)? If not: open a regular account, or record a new decision to fall back to IEX history with rescaled ADV thresholds and IEX-based shadow outcomes (noisier). **Doctor's `data.sip_history` check answers this once keys exist.**
- **Q8 (P2.3):** Should the cash ledger settle on Alpaca's `settlement_date`, which skips bank holidays (D-022), or on T+1 trading days as RISK.md §4 says? Alpaca's date matches real cash-account behavior; the human decides.
