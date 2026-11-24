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

## D-039 — G2 results: Alpaca paper behaves as D-030/D-032 assumed
- **Date:** 2026-10-12 · **Status:** Accepted (G2 passed; the human ran both smoke tests at 13:02–13:03 ET)
- **Observed:**
  - **The stop leg's status:**
    - while the entry rests, the leg is `held`;
    - when the entry is cancelled, the leg goes to `canceled` with it;
    - once the entry has completely filled, the leg is **`new`**.
  - **D-032 stays as it is.** It treats a `held` leg as working only once its entry is `filled`, and the `new` case needs nothing extra.
  - **The trade stream:**
    - the auth handshake succeeded (`trade stream connected`, `authorized`, then `listening`);
    - the marketable run journaled the fill within about 35 ms of Alpaca's `filled_at` (13:03:33.742), before any REST call could have shown it, so the stream delivered and parsed the fill event;
    - the log shows no `unreadable trade update`.
    - The frame type isn't logged. The code accepts both text and binary.
  - **The flatten:**
    - it cancelled the leg, waited for the cancel to be confirmed, then sold at market without a 403;
    - the account was flat about 4 s after the fill.
  - **The ledger:**
    - the buy (−12.00), sell (+11.99) and fee estimate (−0.01) all settle on Tue 2026-10-13, Alpaca's settlement date for a trade on Columbus Day;
    - the calendar sync reported 7 days whose settlement date differs from T+1.
  - **Submits:** a POST returns `pending_new`.
- **Not needed:** the stream-update counter proposed on 2026-10-10, since the fill timing already shows the stream works.
- **Follow-up (Phase 3, cosmetic):** our own clean shutdown logs `trade stream disconnected` as a warning (close code 1000). It should log at info.

## D-040 — Phase 3 plan and the 09:35 scan (P3.1)
- **Date:** 2026-10-12 · **Status:** Accepted (the human approved the Phase 3 plan; a reviewer agent's 21 findings are folded in)
- **Task order:** the human chose to build the backtest early. The order is P3.1, P3.3, P3.6a (pure shadow and regime functions), P3.9, P3.2, P3.4, P3.6b, P3.7, P3.8a, P3.5, P3.8b, then G3.
- **Migration 0004 holds all of Phase 3's schema,** written in P3.1, because the runner refuses a migration changed after it was applied. Later needs go into 0005 or later. It adds:
  - `regime_daily`;
  - `day_runs`, a state log plus EOD step markers, unique per day and step;
  - `orders.purpose`;
  - `incidents.alerted_at`, with existing incidents counted as alerted;
  - `shadow_outcomes.trigger_ts` and `sip_only_trigger`;
  - `book_state.streak_through`;
  - the `final_decisions` view.
- **Signal rows (S§3.2, A§5):**
  - **One row per top-20 symbol,** id `YYYYMMDD-SYM-orb5`.
  - **Direction:** `long`, `shadow_short`, or **`none` for a doji**. A§5's enum had no value for it.
  - **Status:** a long starts `armed`; shorts and dojis are `not_armed`.
  - **Prices** are valid ticks:
    - the trigger rounds up;
    - the stop and `max_chase = floor(trigger × (1 + max_chase_pct/100))` round down, so a limit capped at the chase price stays valid;
    - the nominal `entry_limit` is S§3.3's formula with last = trigger.
  - **Shadow shorts** get a trigger (`or_low − 0.01`) and a stop (`ceil(trigger + 0.10 × ATR14)`), but no chase or limit, since they're never traded.
  - **Ties** on rel_vol go to the bigger OR volume, then the symbol.
  - **Features** hold pre-decision values only.
  - The pure pricing lives in `strategy/pricing.py`, shared with the books and the backtest. The tick helpers moved to `core/prices.py`.
- **The scan (S§3.1–3.2):**
  - **Readiness:** before fetching, it polls SPY's IEX 1-minute bar for 09:34 every 0.5 s, up to 09:35:08. When a 5-minute bar is complete isn't documented, and without the 09:34 minute the OR volume would run about 20% low. The wait is recorded. A past session needs no wait.
  - **Fetch:** `5Min`, 09:30–09:34:59, on the live feed, with split adjustment. Only the bar stamped 09:30 is kept, as `or_baseline` does.
  - **`or_bars`:** the scan doesn't write it. The next universe build fetches today's final bar.
  - **Late corporate actions (D-026):**
    - splits are re-queried for t−7…t+7, keeping those with `ex_date` ∈ {t−1, t};
    - an `or_open/prev_close` within 3% of a split ratio (1/2, 1/3, 1/4, 1/5, 1/10, 5, 10, 15, 20, 25, 30, 40, 50) is a suspect gap;
    - both are dropped **before ranking**, so they never take a slot.
  - **Order of checks:** each eligible symbol gets the first outcome that applies:
    1. `split`, `no_bar`, `suspect_gap`;
    2. the IEX guards: `or_volume` < 1,000, `or_vol_avg` < 200. These are off on `sip`;
    3. `price` (`or_open` ≤ $5);
    4. `rel_vol` (< 1.0, or no baseline);
    5. otherwise `selected`, or `rank` past the top 20.
  - **Saved:** the top 20 go to `signals` with the epoch, and every eligible symbol goes to `or_scan.parquet`.
  - **Restarts:** a restart loads the saved signals instead of refetching.
  - **Late scans:** a scan finishing after the 09:36:30 deadline (flag `scan_late`), or one the caller marks `late_start`, still saves its rows, all `not_armed`, so shadow outcomes and H4 keep the day.
- **`trader scan --date D`:** a dev command that prints the top 20 and saves nothing. It needs that session's universe.
- **Consequences:** 23 new tests cover:
  - every filter at its boundary;
  - ties and the top-20 cut;
  - split and suspect gaps;
  - long, short and doji prices;
  - the readiness wait and its give-up;
  - persistence and restart;
  - late flags;
  - the CLI.

## D-041 — Sizing, shadow outcomes and regime tags (P3.3, P3.6a)
- **Date:** 2026-10-12 · **Status:** Accepted (follows the approved Phase 3 plan, resolutions 5 and 6)
- **Sizing** (`strategy/sizing.py`, S§3.5):
  - **Formula:** `size_position` is A§4.3's pure signature: `qty = floor(min(risk_budget/(limit − stop), notional_cap/limit, S/limit))`. Percentages are as in risk.yaml (1.0 means 1%).
  - **`sizing()` also names the binding term.** On a tie, cash comes before notional and risk, so a book out of settled cash reports `insufficient_settled_cash`.
  - **Degenerate inputs:** a stop at or above the limit sizes nothing. Negative cash or a negative multiplier count as 0.
  - **Hypothesis property:** the quantity never breaks a cap, and one more share always would.
- **Shadow simulation** (`memory/shadow.py`, S§7.2): pure, shared with the backtest.
  - **Trigger minute:** the first SIP 1-minute bar with 09:35 ≤ start < `last_entry` whose high reaches the trigger (a short: whose low does).
  - **Entry:** trigger + slippage ($0.01 + 0.02% of the trigger). The backtest may use `entry_mode="bar_open"` to enter at a trigger bar that opened past the trigger.
  - **Clarification of S§7.2.3: in the trigger minute a stop touch exits at stop − $0.01.** The bar's open came before the entry, so "min(open, stop)" there would book a loss worse than −1R whenever the bar opened under the stop and rallied through the trigger. From the next bar on, the exit is `min(open, stop) − $0.01`.
  - **Exit without a stop:** the flatten minute's open; if that bar is missing, the next bar's open, else the last close before it.
  - **R:** `(exit − entry)/(entry − stop)`.
  - **MFE/MAE:** these span the trigger bar through the last bar held, plus the exit price. They're review evidence only, never a lesson feature.
  - **Shorts** mirror every rule. An empty bar list is `no_data`.
- **Regime tags** (`memory/regime.py`):
  - **Trend:** `spy_trend` is `up` only when SPY closes strictly above its 20-session SMA.
  - **Volatility:** `vol_bucket` compares that day's ATR% (D-025's ATR14 ÷ close × 100) with the terciles (`statistics.quantiles`, n = 3) of the same measure over up to 252 sessions. It's `low` below the lower tercile, `high` above the upper one, else `mid`.
  - **Minimum history:** at least 60 ATR% values, about 75 daily bars; fewer raises `RegimeHistoryError`.
  - **Calendar tags:** `day_of_week` is "Mon"…"Fri", and `event_day` is any high-impact event that date.
  - **Use:** signals on day t carry t−1's tags, under the DSL names `regime_spy_trend` and `regime_vol_bucket` (S§6.2), plus t's calendar tags. P3.6b computes them at boot.

## D-042 — The rules-only backtest (P3.9)
- **Date:** 2026-10-12 · **Status:** Accepted (follows the approved Phase 3 plan, resolution 19). Results are recorded below once the 6-month run is done.
- **Same code as live.** Each historical session runs:
  1. `UniverseBuilder` (S§3.1);
  2. `select()` (S§3.2);
  3. `simulate_long`/`simulate_short` (S§7.2);
  4. a Book A long-only leg, sized by `size_position` and judged by the real `RiskGate` against an in-memory settlement ledger that uses Alpaca's settlement dates.

  The LLM is never involved (D-010). `books/reasons.py` maps gate rejections to reason codes, and the books will share it in P3.4.
- **Data as of each day (look-ahead guards):**
  - **Daily bars:** raw SIP bars, restated only for splits with `ex_date` ≤ t. Alpaca's `adjustment=split` uses today's share basis, so a later 1:10 reverse split would lift a $0.80 stock past the $5 filter.
  - **Splits** are visible up to t, and opening-range bars are served only for sessions up to t. Asking for a later session raises `LookAheadError`.
  - **Tickers** are queried with `asof="-"`, as they traded.
  - **Inactive assets** are included through `UniverseBuilder(include_inactive=True)`, to reduce survivorship bias. `AlpacaRest.assets(status=None)` and `bars(asof=…)` were added for this.
- **Cache:** `runtime/backtest/cache.db` holds the journal schema plus `bt_*` tables:
  - assets;
  - raw daily bars, fetched in chunks of 400 symbols;
  - splits;
  - raw 09:30 bars per feed;
  - 1-minute bars of each day's priced top-20 symbols.

  Day t's scan bars become day t+1's baseline without another request, and a rerun over the same range makes none.
- **Book A leg:**
  - **Order:** triggered armed longs, in trigger-minute order, then by rank.
  - **Entry:** at max(trigger, trigger-bar open) + slippage, skipped as `chased` beyond the chase cap. The limit is S§3.3's formula with last = that base.
  - **R-015** counts as realized only the trades that closed before each decision.
  - **No event blackouts:** there's no historical event calendar.
- **Feed:** `--feed iex` (default) mirrors live relative volume; `sip` compares with the paper.
- **Market hours:** it refuses to run 09:00–16:30 ET on trading days (`--force` overrides), because it would share the data key's rate limit with the live scan. `--end` must be before today.
- **Report:** `runtime/reports/backtest-<start>-<end>-<feed>.md`, plus one Parquet row per signal under `runtime/backtest/`. It covers:
  - the long-only leg: trades, win rate, gross mean R, expectancy net of fee estimates, profit factor, equity, max drawdown, daily Sharpe;
  - SPY buy-and-hold;
  - shadow stats for longs, shorts and both;
  - mean shadow R by rank bucket (1–5 … 16–20) for H4;
  - the limitations.

- **Results** (run 2026-10-12; 2026-04-01 to 2026-10-09, iex feed; `runtime/reports/backtest-2026-04-01-2026-10-09-iex.md`):
  - **Coverage:** 133 sessions, about 1,675 eligible symbols a day (Q6). 2,660 signals: 1,322 long, 1,280 shadow short, 58 doji. 2,260 triggered. 2,166 data requests, about 20 minutes.
    - The first attempt stopped at its first request on Alpaca's CUSIP-like placeholders for delisted assets (D-050).
  - **Book A long-only leg:**
    - 265 trades, 11.7% winners, mean gross R −0.295;
    - expectancy −$0.76 a trade net of fee estimates; profit factor 0.68;
    - $1,000 → $798.31 (−20.2%), max drawdown 28.2%, daily Sharpe −2.44;
    - SPY returned +19.7% over the same days.
    - Triggered longs it couldn't take: settled cash 360, the day's entries 326, open positions 97, chased 94.
  - **Shadow R:** longs −0.307 (n 1,142), shorts −0.176 (n 1,118), both −0.242.
  - **H4 under D-041's rule:**
    - longs: not consistent; ranks 16–20 (−0.205) did as well as ranks 1–5 (−0.213).
    - long + short: consistent but not monotone; ranks 1–5 were best (−0.071).
  - **The verdict hangs on one assumption 1-minute bars can't settle.** Under D-041's trigger-minute rule, 39% of triggered longs (36% of shorts) are stopped in the very minute they triggered. That minute reached both the trigger and the stop, which sits only 0.1 ATR below it, and the order of prices inside the minute is unknown.
    - **An optimistic bound:** a trigger minute that closes on the safe side of the stop doesn't stop the trade. Re-simulated offline from the same cache (it reproduces every stored pessimistic R exactly), it gives:
      - longs +0.135R (15.5% winners), both +0.125R;
      - ranks 1–5: longs +0.869R, both +0.704R. H4 then holds for the top bucket, though not monotonically.
    - **The truth lies between the bounds.** The data support neither "no edge" nor "an edge".
    - **What this affects:** live Book A trades on real fills and is unaffected. Shadow outcomes, and with them H4 and later Book B's veto value, use the same rule.
  - **Proposed, for the human to decide:** resolve each ambiguous trigger minute from the SIP trade tape (Alpaca's historical trades; about 850 small requests for this window), in the backtest and in the live shadow job. Until then, quote both bounds.

## D-043 — Entry triggers (P3.2)
- **Date:** 2026-10-12 · **Status:** Accepted (follows the approved Phase 3 plan, resolution 3)
- **Which trades count.** A trade triggers, or sets the last price, only if none of its conditions is one that Alpaca's market-data FAQ excludes from a minute bar's high and low: B, C, G, H, I (odd lot), M, N, P, Q, R, U, V, W, Z, 4, 7, 9. The strictest rule wins, so one excluded code excludes the trade.
  - This keeps triggers consistent with `or_high` and with the SIP-bar shadow outcomes, since odd lots never set a bar's high.
  - **Unverified:** Alpaca doesn't document the IEX feed's trade conditions; the streaming page's only example is `["@","I"]`. Unknown codes therefore count. G3's recording will show the real codes.
- **Window.** A trade stamped before the opening range ends (09:35:00) is a late print from inside the range and never triggers. Nothing triggers once the receive time reaches `last_entry`. The trigger time is the receive time; the event carries the trade's own timestamp, price, size and conditions.
- **Arming.** The caller subscribes the armed symbols first, so their trades start queuing, and then the engine takes one snapshot of the armed longs:
  - last at or above the trigger and inside the chase cap → an immediate `arm_check` trigger;
  - last past the cap → `missed_at_arm`;
  - otherwise the signal stays armed.

  The snapshot's trade must also count and be stamped at or after 09:35:00.
- **One-shot.** A signal leaves the armed set before any await, then `signals.status`/`triggered_at` are journaled.
- **Dispatch.** Each trigger goes to one queue per book, so Book B waiting for a verdict (Phase 4) can't delay Book A (S§5.4). `next_batch` serves everything already waiting in rank order. That generalizes S§3.5's same-second tie-break to "whatever queued while the book was busy".
- **Restart (A§3):**
  - `armed` signals with no orders are re-armed, with a fresh arm check. `armed_at` keeps its original time.
  - A `triggered` signal with no orders that a book never decided is served again for that book (source `restart`) before `last_entry`, and gets the new reason code `restart_lost` after it.
  - A signal that has orders is never re-triggered.

## D-044 — Book A, the decision log, trades, and why every sell exists (P3.4)
- **Date:** 2026-10-12 · **Status:** Accepted (follows the approved Phase 3 plan, resolutions 4, 6, 7 and 12)
- **Book A** (`books/baseline.py`, S§4):
  1. The orchestrator's `EntryInputs.prepare` gathers every network input **before** the book's lock: last price, quote age, cached broker equity, the asset, and the corporate-action flag.
  2. Pricing. The last price is the event's own price for an `arm_check` trigger, else the hub's. Past the chase cap the signal is skipped as `chased`. Otherwise the S§3.3 limit applies.
  3. Sizing from `service.exposure()`. A quantity under 1 is `insufficient_settled_cash` when cash alone can't buy a share, else `qty_zero`.
  4. `try_enter` with the builder, which only assembles values under the lock.
  5. The decision, whose detail carries the trigger trade (source, price, size, conditions, time), the last price, the limit, the stop, E, S and the quantity.

  The entry id is always `entry-1`.
- **Reason codes:**
  - **Gate rejections** map to A§4.4's codes in priority order:
    1. R-005 → `book_halted`
    2. R-006 → `capacity_positions`
    3. R-007 → `capacity_entries`
    4. R-009 → `insufficient_settled_cash`
    5. anything else → `risk_gate:<first>`

    `detail` lists every violation. The mapping lives in `books/reasons.py`, which the backtest shares.
  - **Other outcomes:**
    - an uncertain submit is `taken` (`submit_uncertain`);
    - a broker refusal is `broker_rejected` (new code);
    - a service that already flattened is `book_halted`. It takes no more entries that day, even after a resume.
- **Append-only decisions:**
  - `insert_decision` adds a row only when the latest one for (signal, book) differs, so reruns can't duplicate.
  - The `final_decisions` view is the latest row per signal and book.
  - At EOD, `finalize_decisions` gives a taken entry that ended with no fill `entry_unfilled`, or `broker_rejected` if the broker rejected it after accepting the submit. Running it twice changes nothing.
  - `record_skips` writes `missed_at_arm` and `restart_lost` for every book.
- **Every sell says why it exists** (`orders.purpose`, migration 0004). The values are:
  - `stop_leg`: set on a leg's upsert;
  - `protective_stop`: D-032's standalone stop, and the overnight GTC stop;
  - `stop_breached`: sold because the last price was already through the stop;
  - `eod_flatten`, `daily_loss`, `incident` (P1s and reconcile mismatches), `kill_switch`;
  - `manual`: the CLI and the smoke test.

  `flatten_account`, `ExecutionService.flatten` and `Records.pending_sell` now require a `purpose`. A sell without a `signal_id` takes the signal of the book's latest entry in the symbol (the one that bought the shares).
- **One flatten at a time:** `ExecutionService.flatten` holds a lock. A second caller waits, then runs its own pass, which finds the book flat at once if the first succeeded. Before this, the first to finish reset `_flattening` while the other was still working.
- **Trades** (`memory/trades.py`) are rebuilt at EOD with an idempotent upsert, id `{book}-{signal_id}`:
  - entry and exit VWAPs over the entry's filled quantity;
  - the exit reason comes from the purpose of the sell that executed the most shares (a tie goes to the last); `stop_leg` and `protective_stop` both read `stop`;
  - gross `pnl` and `r_multiple` against the planned stop, with `fees_est` from the fee-estimate rows;
  - `entry_slippage` against the trigger, and `entry_delay_ms` from the live trigger to the first fill.

  A position still open keeps NULL exit fields. Smoke-test entries and unfilled entries make no trade.

## D-045 — The shadow and regime jobs (P3.6b)
- **Date:** 2026-10-12 · **Status:** Accepted (follows the approved Phase 3 plan, resolutions 5–7)
- **Shadow outcomes** (`compute_shadow`, at close + 12 min):
  - **Timing:** the job waits until the flatten minute's SIP bar can exist (flatten + 16 min, since the free plan's SIP data lags 15 min). It then makes one 1-minute request for every priced signal's symbol, with `end` omitted.
  - **Retries:** an `AlpacaError` such as "too recent SIP data" is retried every 30 s for up to 5 min. After that the run reports `ok=False`, so the orchestrator can raise a P3, and writes nothing.
  - **Rows:** one per priced signal, longs and shadow shorts, with `INSERT OR REPLACE`, so a rerun replaces. A doji has no prices and gets no row. An untriggered signal is `no_trigger` with NULL R; a symbol with no bars is `no_data`. `path_source` is `sip_1min`.
  - **`sip_only_trigger`** is 1 for a long the SIP bars triggered but the live feed never did, 0 for other longs, and NULL for shorts, which are never armed.
- **Regime** (`ensure_regime`, at boot for t−1):
  - It computes the tags from SPY's SIP daily bars (split-adjusted, about 400 calendar days) and stores them once in `regime_daily`. Later calls read the table.
  - When the data is missing or too short it returns None and logs. Signals then carry no regime tags rather than wrong ones.
- **Finding:** `statistics.quantiles` interpolates in floats. A flat series of 0.8 gives terciles of 0.8000000000000002, which put a day lying exactly on a tercile in the wrong bucket. Cut points and the day's ATR% are now compared at 9 decimals.

## D-046 — The daily report, notifications and the alert pump (P3.7)
- **Date:** 2026-10-12 · **Status:** Accepted (follows the approved Phase 3 plan, resolution 10). Provider details verified against docs.ntfy.sh/publish and Discord's webhook and rate-limit pages.
- **Daily report** (`reports/daily.py`): one data model, rendered to Markdown and to HTML (with `html.escape`, no template engine) in `runtime/reports/YYYY-MM-DD.{md,html}`. It contains:
  - a header with the session, epoch, mode, books, the regime in force, halts in force, and "LLM calls: none" until Phase 4;
  - the signals table: rank, rel_vol, direction, status, trigger, live trigger time, shadow R and exit, and each book's final decision with its trade's R and net P&L. Book B shows `n/a` until Phase 4;
  - the books table, from `daily_stats` (or from the trades when stats are missing);
  - each book's trades and the day's incidents;
  - S§10's limitations.

  `summary()` is the one-line end-of-day notification. `trader report daily [--date D]` regenerates the report offline.
- **Notifications** (`notify.py`). Messages never carry secrets or account numbers, a failure is logged without the URL or topic, and a send never stops the day.
  - **`none`.**
  - **`ntfy`:**
    - a plain-text POST to `{server}/{topic}` with `X-Title` (ASCII), `X-Priority` (1–5) and `X-Tags`;
    - the body is cut to 4,000 bytes on a character boundary, since ntfy turns anything over 4,096 into an attachment;
    - the topic is a secret, because ntfy treats it as the password.
  - **`discord`:**
    - `{"content": ≤ 2,000 chars, "allowed_mentions": {"parse": []}}`, and success is 204;
    - a 429 waits `retry_after` (capped at 30 s) and retries once;
    - a 404 disables the provider for the run, as Discord asks;
    - the webhook URL is a secret.

  A configured provider whose secret is missing falls back to `none` with a warning. `trader notify test` sends one message so you can check your channel (H0.5).
- **The alert pump** (`pump_alerts`) pushes every P1 (`urgent`) and P2 (`high`) incident whose `alerted_at` is NULL, then stamps it. Incidents raised anywhere reach the human without coupling those modules to notify. A failed send stays unstamped and is retried on the next pump. P3s appear only in the report. Run-day runs the pump (P3.5).

## D-047 — Recording and replaying a day (P3.8a)
- **Date:** 2026-10-12 · **Status:** Accepted (follows the approved Phase 3 plan, resolutions 15–17). `ReplayBroker` and `trader replay --date` moved to P3.8b, which builds on these pieces.
- **What a live day records,** under `runtime/recordings/<date>/`:
  - **`stream.jsonl.gz`:** every hub event, with its receive time `rx`.
  - **`rest.jsonl.gz`:** every market-data REST call after boot (Alpaca's clock, assets, bars, snapshots, splits), with its arguments, its result as our parsed models or its error, and its start and end `rx`.
  - **`broker-A.jsonl.gz`:** Book A's broker calls and trade updates, results as our parsed models. Alpaca's raw payloads are left out, which keeps account details out of the file.
  - **`boot.json`** (`boot-2.json` after a restart): each book's ledger entries, halts, `book_state`, broker cash and E; a calendar slice of ±45 days; the settings, risk and events file hashes; the git SHA and the version. Never a secret.
- **Not recorded:** the calendar sync and the universe build. Both run over the unrecorded client. Replay takes the calendar from `boot.json` and the universe from `universe.parquet`, instead of thousands of recorded bars.
- **Replay pieces:**
  - **`ReplayMarketData`** answers a call by method and canonical arguments, and repeated identical calls by occurrence. A surplus call reuses the last answer and counts as a divergence; a call never recorded raises `ReplayMissError`. Each answer arrives after the recorded duration on the injected clock, so a replayed scan takes as long as the live one. Recorded errors are raised again by type.
  - **`ReplayHub`** plays `stream.jsonl.gz` by `rx` through the same `HubState` as the live hub. It honors the subscribed symbols and channels, and, like a live hub after a restart, skips events received before it started.
- **Hub taps:** taps run synchronously at publish time. The recorder stamps `rx` there, and a SimBroker sees each event the instant it arrives.
- **`last_price()`** uses only trades that may set a price (D-043; odd lots never do). It returns None once the trade is older than 60 s, so callers fall back to the broker's mark rather than a stale price.
- **Determinism:**
  - **Inline journal:** `Journal.open(path, inline=True)` runs every transaction on the event loop's thread, with no writer or reader threads. Replay and tests are deterministic and faster.
  - **`Journal.backup(dest)`:** SQLite's online backup, from the reader connection.
  - **`simdrive.drive`:** moves the SimClock to the next wake-up only when the loop is quiet, meaning nothing ready for three turns and every watched part idle. Time never moves while work is pending.
- **Test harness:**
  - **The synthetic day** (`tests/fixtures/days.py`) builds each symbol's minute path from keyframes. The REST answers, the stream events and the universe rows all derive from it, so they always agree. Flat minutes stream only every tenth minute, as a thin feed would.
  - **`dayrun.py`** runs the real orchestrator over it. Each SimBroker is fed by an always-on "exchange" feed rather than the engine's hub, so, like Alpaca, it keeps filling stops while the engine is down.
  - **`SimBroker.hold(symbol, qty, price)`** seeds a position carried in from earlier. Replay will reuse it.

## D-048 — The day orchestrator and `trader run-day` (P3.5)
- **Date:** 2026-10-12 · **Status:** Accepted (follows the approved Phase 3 plan, resolutions 2–4 and 9–18). The human runs `run-day` only at G3.
- **Modules** (`orchestrator/`), each small with injected dependencies:
  - `plan.py`: the day's timeline;
  - `lock.py`: one engine per runtime folder;
  - `inputs.py`: the gate's network inputs;
  - `tasks.py`: the heartbeat, the staleness watch, the subscriptions, and a loop that survives its failures;
  - `eod.py`: end-of-day bookkeeping and breakers;
  - `day.py`: the state machine;
  - `live.py`: the wiring for `run-day`;
  - `ports.py`: the hub and market protocols.
- **Timeline** (A§3): morning times are fixed (subscribe at open − 1 min, scan at 09:35:03, deadline 09:36:30). After the close, EOD, shadow outcomes and the report run at close + (schedule − 16:00): 16:05, 16:12, 16:30, or 13:05, 13:12, 13:30 on an early close. Last entry and flatten follow `day_times`.
- **The states:**
  - **BOOT:**
    1. **Clock check:** compares with `/v2/clock`. More than 2 s off, or no answer at all, is a P2 `clock_skew` and nothing is armed (R§9). The scan still runs and saves every row `not_armed`, with the new flag `clock_skew`.
    2. **Universe and regime:** builds the universe if missing, then computes the t−1 regime.
    3. **Assets:** reads the asset list once, as R-003's input.
    4. **Each book, in order:**
       - **Boot reconciliation (resolution 11):** today's journal explains a restart, and the service adopts it. Anything else (a position carried overnight, someone else's shares or orders, our orders from an earlier day) is a P1 `boot_unexplained` and a `p1_incident` halt, with a flatten at max(now, open + 5 s).
       - **Adoption:** shares the journal never heard of are journaled first as an adoption fill at the broker's cost, with no ledger entry. Without it, selling them would leave the journal "short" and the mismatch would come back every day.
       - **Ledger:** seeded on first use, from a flat account only; otherwise boot fails with an error.
       - **Peak and service:** `raise_peak(E)`, then the service starts.
    5. **Background loops:**
       - the kill switch every 1 s, its failures retried;
       - the heartbeat every 10 s;
       - the alert pump every 5 s;
       - the staleness watch every 5 s.
    6. **Stream and record:** the hub, then `boot.json`.
  - **PREMARKET:** subscribes the regime symbols plus everything held.
  - **OR_SCAN:**
    - **Late scans:** a late start is a P2 `late_start`, and a scan past its deadline is a P2 `scan_deadline_missed`. In both cases the rows are saved `not_armed`, so H4 keeps the day. A scan inside its deadline that took over 30 s is a P3 `scan_slow`. A scan that fails is a P2 `scan_failed`, with no signals.
    - **Order:** the armed symbols are subscribed (by rank) before the snapshot. Arming, or re-arming after a restart, comes next. Trigger processing starts only after arming, so trades buffered meanwhile are judged against armed signals.
    - **Skips:** signals missed at arming are `missed_at_arm`; triggers undecided after a restart are `restart_lost`.
  - **TRADING:**
    - one worker per book; a trigger that raises is a P2 `trigger_failed`;
    - at last entry the engine expires its armed signals, and any signal still `armed` (a skewed clock, say) is swept to `expired`;
    - workers are stopped only between decisions, never mid-submit.
  - **FLATTEN:** retries until flat or the close. A book still not flat at close − 2 min is a P1 `not_flat` with a `p1_incident` halt, raised once. At the close the `at_close` hooks run (SimBroker's day-order expiry), then one more pass leaves GTC stops on what's left (D-036).
  - **EOD** (close + 5): per book, `reconcile`, `decisions` (`finalize_decisions`, plus `restart_lost` for triggers never decided), `trades`, `stats` (`daily_stats`) and `breakers`. Each step is marked in `day_runs` and a restart skips it. Then the services stop.
    - **Breakers** (RISK.md §4–5), each a P2 and a halt until `trader resume`:
      - a cash mismatch over `reconcile_cash_tolerance`;
      - drawdown ≥ 10% from the peak;
      - five losing sessions in a row.
  - **REVIEW:** shadow outcomes at close + 12; a failure is a P3 `shadow_unavailable`. Claude's review joins here in Phase 5.
  - **REPORT** (close + 30): one last alert pump; the report; the summary notification; the database backup (`db_backups_keep` copies); the recordings closed. Then `DONE`, and a finished session is never run again.
  - **A state that fails before the flatten** is a P2 `state_failed` and falls through to FLATTEN and EOD: closed for entries, open for exits.
- **Three behaviors the scenario tests changed:**
  1. **A halted book declines first.** `book_halted` (from any halt in force, the kill file, or a service that already flattened) now comes before the chase and sizing checks, because it is the reason that matters. `PreparedInputs.halts` and `ExecutionService.accepting` carry it. Before this, a kill-switched book reported `insufficient_settled_cash`.
  2. **A persisting reconcile mismatch alerts once.** A foreign position raised a P1 on every pass, 368 in a day. Now the same mismatch reports once (incident and halt) until a clean reconcile. A position mismatch is still flattened on every pass.
  3. **Losing streak:** a session counts once (`book_state.streak_through`). A session that traded and lost extends the streak; any other session, one without trades included, ends it. `BookStats.record_day(book, day, pnl, traded=...)`.
- **The stream (resolution 13):**
  - **Channels:** run-day streams trades and quotes only.
  - **405 fallback:** a 405 first drops quotes (trades only), then armed symbols from the lowest rank. Held and regime symbols are never dropped. One P2 a day: `subscription_limited`, or `subscription_refused` for other errors.
  - **Bad input:** the WebSocket client skips unreadable frames and messages, reconnects on any unexpected error, and on an ack timeout keeps the target and reconnects, raising `StreamError(408)` (our code).
  - **Server errors:** errors no request waited for reach `on_error`; run-day makes them a P2.
  - **Clean closes:** our own close of either stream logs at info (D-039).
- **Staleness:** the clock starts at the later of the open and the first subscription. 30 s without an event logs a warning. 120 s is one P2 `data_stale` per episode and sets `hub_stale`, so R-016 rejects entries until data returns.
- **Heartbeat** (`heartbeat.json`): `{ts, state, books: {A: {halted, halts, positions}}, hub_age_s}`, written atomically. Each beat also samples the books' marked equity for `daily_stats.max_intraday_dd` ($). That value restarts with a restart.
- **`run-day`:**
  - **Books:** `--books A`; B is refused until Phase 4.
  - **`--sim`:** live data with a $1,000 SimBroker per book, fed by the hub. Each day gets its own folder, `runtime/sim/<date>/`, starting from the main database's universe for the day. It watches the real kill file and prefixes alerts with `[SIM]`.
  - **`--dry-run`:** boot's checks, read-only, plus the timeline. It takes no lock and records nothing.
  - **Lock:** `run-day.lock` is an OS file lock (`msvcrt` or `flock`), released by the OS however the process ends. There is no PID probing, because `os.kill(pid, 0)` terminates the process on Windows.
  - **Paper only:** live mode is refused.
- **Known limits (accepted for Phase 3):**
  - When a book carries its own position overnight (its flatten failed), that day's realized P&L and `daily_stats.pnl` overstate the gain by the shares' cost basis, because today's fills have no buy. The book is P1-halted that day anyway.
  - The intraday drawdown is sampled every 10 s.
- **Tests:** 17 whole-day scenarios on SimClock, about 1–2 s each:
  - a normal day, every artifact checked;
  - a finished day not rerun;
  - an early close; a holiday;
  - a late start; a start after the flatten time;
  - a mid-day crash and restart; a restart after the flatten time; an EOD rerun;
  - a foreign overnight position; a first boot on an account that isn't flat;
  - clock skew; a missed scan deadline;
  - the kill file through a broker error; the kill switch and the flatten together;
  - not flat by the close;
  - the 405 fallback.

  Unit tests cover EOD, the inputs, the background tasks and the CLI.

## D-049 — `trader replay`, the golden day, and how a replay matches broker answers (P3.8b)
- **Date:** 2026-10-12 · **Status:** Accepted (follows the approved Phase 3 plan, resolutions 16–17)
- **`trader replay --date D`** (`orchestrator/replay.py`):
  - **Seeding:** a fresh `runtime/replay/<date>/` database, seeded from the recording's `boot.json` (the calendar slice, each book's ledger, halts and `book_state`, and the regime tags) and `universe.parquet`. `boot.json` gained `started_at`, where the replay's SimClock starts, and `regime`.
  - **The run:** the real orchestrator runs on recorded REST answers (`ReplayMarketData`), the recorded stream (`ReplayHub`) and each book's recorded broker (`ReplayBroker`), with no notifications, no recording and its own kill file.
  - **The report:** every divergence (a call the recording lacks), then a diff of the day's signals, final decisions and trades against the recorded journal. It exits 1 on any. It warns when the config files changed since the recording, and when the day had a restart (the replay covers the first engine).
- **`ReplayBroker`** (`execution/replay.py`):
  - **Matching:** submits match by client order id, cancels by order id, and trade updates arrive at their `rx`. An unrecorded call goes to a fallback SimBroker and counts as a divergence.
  - **Reads go to the recorded call that started nearest to now, unused answers first.** The planned rule, "the latest answer at or before now", failed the record-then-replay test in two ways:
    - **Same-instant calls:** in a zero-latency recording, several reads at one instant capture different stages of one sequence. The replay was handed the open orders as they stood after a protective stop was placed. It never placed the stop, decided its position was unprotected, raised a P1 and flattened.
    - **Early replays:** a live engine spends milliseconds between calls that a SimClock doesn't, so a replay runs slightly early within a sequence, and "at or before" then returns the previous poll.
  - **No yields:** an answer with zero recorded duration no longer yields to the loop (`ReplayMarketData` too), because the recorded call didn't.
- **The golden day** (`tests/fixtures/golden.py`, `tests/golden/standard_day.json`): `standard_day` plus a partial fill on WIN covers every case resolution 16 lists:
  - a winner held to the flatten (WIN, 10 of 24 shares, under its own protective stop after the partial: P3 `partial_fill_protected`);
  - a stop-out (LOSE);
  - `chased` (GAPPER), `missed_at_arm` (CHASE) and `expired` (EXP);
  - a capacity rejection (CAP, R-007: the day's two entries are used);
  - a shadow short and a doji.

  The snapshot keeps natural keys only. `UPDATE_GOLDEN=1` rewrites it.
- **Determinism tests:**
  - two runs of the golden day in separate processes, with `PYTHONHASHSEED` 1 and 2, write identical snapshots, equal to the golden file;
  - a day recorded the way `run-day` records it (REST, broker and stream wrappers, `universe.parquet`, `boot.json`) replays with no divergence and no difference;
  - deleting a book's broker recording makes the replay report divergences and exit 1;
  - `trader replay` runs end to end against a recorded fixture day.

## D-050 — Only real tickers enter the universe
- **Date:** 2026-10-12 · **Status:** Accepted (found by the first real backtest run)
- **Finding:** the first `trader backtest` failed on its first data request: `HTTP 400: invalid symbol: 0029900E0`.
  - With inactive assets included (D-042's survivorship guard), Alpaca's asset list also carries delisted securities under CUSIP-like placeholders: `0029900E0`, `003CVR016` (rights), `5326esc96` (escrows). Some are lower-case.
  - These keep their old exchange, so the class, exchange and symbol-character checks let them through.
  - The bars endpoint rejects a whole request over one such symbol.
- **Decision:** an asset is a candidate only if its symbol is 1–6 upper-case letters, the shape our signal and order ids already required (`core/ids.py`; such a symbol would otherwise crash the scan when it made a signal id).
  - `UniverseBuilder` records the new exclusion reason `not_a_ticker`, and the backtest's `eligible_asset` applies the same check.
  - US equity tickers are letters only: share classes use `.`/`/`, which are already excluded.
  - Test fixtures named `SPL2`/`PRC2` were renamed.
- **Backstop:** if Alpaca still calls a well-formed symbol invalid, the backtest's daily-bar fetch drops that symbol (logged) and asks for the rest of the chunk again, instead of failing the run. The live universe build relies on the filter alone, since active listings don't carry placeholders. A refused live build is a P2 `universe_unavailable` at boot.

## D-051 — G3 pre-flight: failure paths hardened before the first real session
- **Date:** 2026-10-12 · **Status:** Accepted
- **Context:**
  - The human verified `events.yaml` (P1.8), built the 2026-10-13 universe (1,587 eligible) and ran `run-day --dry-run`. The `--sim` rehearsal was skipped, so G3 is run-day's first contact with real data.
  - A read-only review looked for what the offline tests can't catch: five reviewers, adversarial checks on serious findings, and a completeness critic. It found no blockers.
  - The fixes below all sit on failure paths. Each has a test that fails on the old code.
- **Fixes:**
  1. **A trading task that dies no longer skips the flatten.**
     - The trigger engine logs and skips an event that fails. A failed journal write re-arms the signal, so the next trade retries it.
     - A dead engine or book worker is a P2 `state_failed` at once, and the engine's feed is detached from the hub.
     - `_stop_trading` no longer re-raises the task's error. Before, it did so at last entry, which skipped FLATTEN, EOD and REPORT and could leave a position overnight on its DAY stop.
  2. **The clock check is read again.**
     - A reading's window includes the retries inside the call, so one slow answer looked like skew. A reading out of tolerance is now taken again, up to 3 times, and the shortest round trip counts.
     - If boot found the clock skewed or unverified, or couldn't fetch the asset list, both are checked again at 09:29 before the scan.
     - D-048's fail-closed rule still holds: still skewed or unverified at 09:29 means nothing is armed.
  3. **The after-close pass** is tried 6 times, 5 s apart, before a P1 `not_flat`. Its result (what is held and which GTC stops protect it) is always recorded, even after the 15:58 P1; the book is halted only once.
  4. **After the close, cancels are waited for** (up to 20 polls of 0.5 s) before the GTC stops are sized. A pending cancel holds the shares, so the stop came up short. `FlattenResult.protected` now lists every GTC stop protecting a position, existing ones included.
  5. **EOD is retried every 30 s until the shadow time.**
     - A state after the flatten that still fails leaves the day un-DONE: run-day prints the state with "run run-day again today to finish".
     - That day's report isn't marked done, so the rerun finishes EOD and rebuilds the report.
  6. **Shadow outcomes read the signals from the journal.** The scan-time copies lack `triggered_at`, so every long the stream triggered was stored with `sip_only_trigger = 1`. The golden day changed in exactly those four rows.
  7. **Replay ignores market-data answers recorded before `boot.json`'s `started_at`**, which come from an attempt that died during boot, and says so in its notes.
  8. **The console no longer shows httpx/httpcore INFO lines** (about 25 a minute all day). The JSON log keeps them; an answer of 400 or more still reaches the console.
  9. **A second review, of these fixes themselves** (three lenses), found no blocker. It led to:
     - **Never a traceback after a finished day:** a dead task's P2 and `_shutdown` never raise. A failed incident write could otherwise turn a DONE day into a traceback.
     - **Bare shares are retried too:** the after-close pass is also retried while shares have no GTC stop. `FlattenResult.unprotected` names them, and the P1 adds "NO STOP: …".
     - **The EOD retry window** is at least 7 minutes from EOD's start, so a rerun after the shadow time also retries.
     - **An unknown drawdown is NULL, not 0:** a run that started after the close stores `max_intraday_dd` as NULL, since it sampled none of the session.
     - **Honest wording:** boot's P2 promises a re-check only when one will run.
     - **The replayed clock keeps the live skew:** the reading is moved by however far the replay's ask is from the recorded one.
     - **A killed run's recording stays readable:** a gzip member cut short by a kill is read up to the next member, so the rerun's appended records survive.
- **Left as they are** (the G3 runbook in HANDOFF covers the operator side):
  - **Stopping run-day skips the flatten.** Ctrl+C or closing the terminal leaves positions on their broker-side stops, which are DAY orders and expire at 16:00. To stop trading, create the kill file; if run-day dies, rerun it the same day.
  - **A resent subscription has no 405 fallback.** A subscription change made while the hub is reconnecting is resent on reconnect without it. This needs a reconnect at about 09:35 and Alpaca counting channels toward the 30 (Q2).
  - **Partial-fill re-protection is tight.** It must finish within 5 s under the 3/s order-API limiter. A P1 `unprotected_position` right after a partial fill is that race.
  - **Boot reconciliation acts on a single pass,** so a fill landing during a mid-day restart's boot can be a false P1.
  - **Replay loads the whole stream into memory** (about 1.5 KB an event).
  - **One surplus readiness call can fail the replay.** If the readiness poll gives up at 09:35:08, replay can report one extra SPY 1Min call as a divergence.
  - **Live v2 orders carry no `group_id`/`group_role`.** D-030 assumed they do. Legs are tied through the nested `legs`. SimBroker sets the fields, so its group branches aren't the live path. To fix after G3.
  - **Alpaca also checks an OTO stop against the current price,** not only against the limit. A fast reversal before the submit becomes `broker_rejected`, which fails safe.

## D-052 — G3 run 1 (2026-10-13): what Alpaca paper did at the open
- **Date:** 2026-10-13 · **Status:** Findings recorded. The G3 verdict and the fixes below await the human.
- **The run:**
  - It started at 08:12 and reached DONE at 16:30. Boot, both streams, the 09:29 and 09:35 subscriptions and the scan all worked on real data.
    - Subscriptions: 11 symbols on trades and quotes, with no 405.
    - Scan: 2.3 s, with a 0.32 s bar wait; 20 selected, 8 longs armed, 7 triggered.
  - EOD, the shadow outcomes (20), the report and the backup ran. The book was flat from 09:36:03.
  - `trader replay` matches the recorded day: 5.7 M events in 4 minutes.
- **Pass criteria:** every one held except "no P1".
  - A P1 `unprotected_position` on FIGS at 09:35:48 halted Book A for the day.
  - QLD, CNH and QMCO were then declined as `book_halted`. QLD's shadow R was +2.97.
  - FIGS's 16 shares were sold at 09:36:03 for +$0.32.
- **What Alpaca paper did at the open** (from `broker-A.jsonl.gz`):
  - **Acceptance took up to 13 s.** FIGS's OTO sat in `pending_new` from 09:35:29.97 to 43.00, and the exit market sell for 9.7 s.
  - **Cancels took 4–13 s:** FIGS 48.10 → 52.18 (4.1 s), RNG 31.12 → 44.05 (12.9 s).
  - **A cancel sent to a `pending_new` order can be dropped.** FIGS showed `pending_cancel` at 42.1, then `new` at 43.0. It filled 13 and then 3 of its 26 shares, and the cancel was reported `order_cancel_rejected` at 59.1.
  - **A partly filled entry's stop leg stays `held`** for the full 26 shares. `qty_available` equalled the filled shares (13, then 16).
  - **Fees:** with no trades, cash fell $0.02 overnight (999.99 → 999.97), a fee for G2's one-share round trip. The paper-trading docs say paper "does not account for regulatory fees". Our ledger estimated $0.01, and D-034 leaves estimates out of the paper cash check. The drift is far inside the $1 tolerance, but it accumulates.
- **Our defects:**
  1. **A cancel that doesn't take is never re-sent.**
     - `cancel_requested` stays set, so `_cut_short` skips the entry.
     - D-032's step 2 waits for every buy to be final before covering. The 16 shares stayed bare until the P1 at 5 s.
     - Nothing handles `order_cancel_rejected`.
  2. **`flatten_account` cancels its own working market sell on its next pass** (09:35:54). The sell filled first (09:36:03), but a faster cancel would have made the loop sell again.
  3. **The entry timeout counts from submission,** so it cancelled FIGS before paper had accepted it. Its 10 s never ran at the exchange.
- **The constraint:** wash-trade protection (D-030) rejects a stop or market sell while the buy is open. A partial fill can't be covered before its remainder is cancelled. With 4–13 s cancels at the open, R-020's 5 s can't always be met, even with defect 1 fixed.
- **Also learned:**
  - **IEX trade conditions seen** (D-043):
    - `' '` and `@` (regular sales);
    - `I` (odd lot; 64% of trades, already excluded from triggers);
    - `F` (intermarket sweep);
    - `T` (extended hours; 92 trades, all outside the trading window).
  - **Q2 is still open:** 11 symbols × 2 channels = 22, under 30 either way.
- **Proposed** (for the human):
  1. Re-send a cancel whose order is seen open again (`new`, `accepted` or `partially_filled`), at most once a second.
  2. During market hours, `flatten_account` never cancels a working market sell.
  3. The entry timeout counts from the broker's acceptance, not from submission (an S§3.3 clarification).
  4. **R-020 for partial fills (risk; the human's call):** either allow longer than 5 s while the remainder's cancel is pending at the broker, or keep 5 s and accept occasional P1s at the open.
  5. Later: confirm the fee from Alpaca's FEE activities, then count fee estimates in the paper cash check and round them as Alpaca does (amends D-034).

## D-053 — G3 fixes: slow acceptance and slow cancels at the open (resolves D-052)
- **Date:** 2026-10-13 · **Status:** Accepted. The human chose the R-020 rule and approved the plan on 2026-10-13.
- **Correction to D-052: the FIGS cancel was queued, not dropped.**
  - Alpaca holds a cancel sent while an order is `pending_new` and processes it seconds after acceptance. Meanwhile the order shows `new` and can fill.
  - RNG shows it: one cancel during `pending_new`, `new` at 34.40, cancelled at 44.05, with no re-send.
  - FIGS's queued cancel was overtaken by the flatten's (48.10 → 52.18). Its `order_cancel_rejected` (59.09) carried the status `canceled`.
  - Alpaca also rewrites `submitted_at` to the acceptance time.
- **Decisions:**
  1. **The entry timeout counts from acceptance** (S§3.3 clarified).
     - The 10 s run from the broker accepting the order. An order not accepted within 30 s of submission is cancelled anyway.
     - Only an acceptance this process saw restarts the clock. After a restart or an adoption, the timeout counts from submission.
  2. **Cancels go through one helper.**
     - A cancel is sent again at most every 2 s while the order shows as working (`new`, `accepted`, `partially_filled`), and never while it is `pending_new`, where the first is still queued.
     - `order_cancel_rejected` is logged. It allows an early re-send if the order is still open.
     - An entry still open 60 s after its first cancel is a P2 `entry_stuck`.
  3. **R-020's allowance for partial fills** (the human's choice):
     - While every open buy on the symbol is our entry with its cancel requested or pending, partly filled shares get up to `partial_fill_cancel_max_s` (20) from the first fill.
     - Once the cancel lands, the stop gets `unprotected_position_max_s` from then, never past the 20 s. Otherwise the limit stays 5 s.
     - It is a new required `risk.yaml` key; config checks that it is ≥ 5. `caps.py` is unchanged.
     - The P1 message says which limit applied.
  4. **Our market sell already at work counts as cover,** in R-020 and on the `stop_breached` path. Paper took 9.7 s to accept FIGS's exit.
  5. **`flatten_account` never cancels a market sell at work.**
     - Per symbol, it sells only when every open order on that symbol is such a sell. It waits for buys and pending cancels, and sells the other symbols meanwhile.
     - A pass's timing is unchanged, so the golden day is identical. After the close nothing changes.
  6. **SimBroker gets opt-in faults:** `accept_delay`, queued cancels (`queued_cancel_delay`, or `drop`), `order_cancel_rejected`, and a `cancels` log. It also no longer loses a slow cancel to a partial fill (an old bug).
  7. **An adversarial review of this diff found no blocker.** It led to:
     - **A lagging order list:** Alpaca's list trailed `get_order` by 0.3–0.6 s on 10-13. An entry `get_order` saw final would still be listed as an open buy, read as someone else's, and collapse the allowance to a P1 at the cancel's landing. Such entries are now remembered and left out of the buys.
     - **A stuck exit:** an exit at work counts as cover only for `partial_fill_cancel_max_s` after it was sent. After that, R-020 acts again.
     - **SimBroker** no longer revives an order that turned final while it waited for acceptance. A market sell accepted after the close stays queued.
     - **The log line is `cancel rejected`,** with the side and type, since it also appears for stops.
- **Amends:**
  - **D-032:** step 2's wait, and the fallback timing above.
  - **D-036:** steps 1–2; a market sell at work is kept.
  - **D-037:** a bare partial after a restart is a P1 at once unless its entry's cancel is still pending, in which case the allowance applies.
- **Also:**
  - `eval_epoch` stays 0, since nothing has been evaluated yet.
  - **Live versus final opening-range bars** (checked after the 10-14 universe build, which stored 10-13's final IEX bars):
    - The 09:35 scan's bars matched the final ones for 1,352 of 1,353 symbols. The odd one out was a late print: volume 2.3% lower live, and the high one tick lower.
    - The top 20 is the same either way.
    - D-040's 09:34 readiness wait works on real data.
  - **`trader replay --date 2026-10-13` now diverges by design:** the fixed code makes calls that recording lacks. G3 run 2's recording becomes the golden replay.
  - **Tests:** SimBroker 9, config 2, service 15, flatten 5, orchestrator 1, counting the review follow-ups. With the old service, the slow-open scenario reproduces G3 run 1's P1. 714 tests pass.

## D-054 — G3 run 2 (2026-10-14): passed; D-053 at the open; the golden replay; review findings
- **Date:** 2026-10-14 · **Status:** **G3 passed** (the human, 2026-10-14), on the condition that `trader doctor --quick` shows Book A with 0 positions and 0 orders. The fixes below await a plan.
- **The run:**
  - Boot 08:11:54 (skew 0.165 s), DONE 16:30. 1,559 eligible; the scan took 2.53 s with a 0.34 s bar wait; 20 selected, 11 longs armed, 9 shadow shorts.
  - **8 triggered:**
    - VRNS, KBH, BWA and FAST were taken.
    - KLRA and AADX were `chased`.
    - CIFR was `capacity_positions` (R-006, R-007).
    - STT was `insufficient_settled_cash`.
    - BSP, NTRS and XHB expired at 15:30.
  - **Trades:**
    - VRNS and KBH didn't fill; their timeouts cancelled them.
    - BWA (8 @ 60.25) and FAST (10 @ 49.79) filled. Their OTO legs stopped them out at 10:11 and 10:14: −$2.64 (−1.10 R) and −$1.51 (−1.08 R).
    - Net −$4.21 with fee estimates; equity went from 1000.29 to 996.08.
  - **One incident:** P3 `partial_fill_protected` (FAST). The log has no warning or error lines.
  - **Flat from 10:14:02.** The 15:55 flatten pass and the after-close pass found nothing to do.
- **Pass criteria:** all held.
  - The run reached DONE with no P1, and the report and journal were written.
  - The recording shows no positions or open orders at 15:55, 16:00 and 16:05.
  - `trader replay --date 2026-10-14` matches: 2 min 27 s on the full 81 MB recording.
- **How D-053 behaved at the open** (from `broker-A.jsonl.gz`):
  - **Acceptance, measured from submission:** VRNS took 12.13 s (09:35:30.39 → 42.52, broker time). Later in the morning: KBH 0.056 s, BWA 0.009 s, FAST 0.015 s.
  - **The timeout counted from acceptance:**
    - VRNS's cancel went out 10.5 s after acceptance (09:35:52.86), after `get_order` reported `new`. The old rule would have sent it at about 09:35:40, during `pending_new`, as it did with FIGS.
    - KBH's cancel also went out 10.5 s after acceptance. Polls ran every 1.05 s.
  - **Cancels:**
    - VRNS showed `pending_cancel` from the DELETE until it was cancelled 11.1 s later. That status isn't one that triggers a re-send, so nothing was sent again.
    - KBH's cancel landed in 20–35 ms.
    - There were two cancel calls all day, no `cancel rejected` and no `entry_stuck`.
  - **The partial fill:**
    - FAST filled 5 of 10 shares at 09:44:00.77 (broker time) and the other 5 shares 1.04 s later.
    - Its OTO leg was `accepted` 3 ms after the completing fill and `new` after 20 ms.
    - No cancel was pending, so the 5 s limit applied, not the 20 s allowance. The 5 shares were bare 1.04 s, and the P3 followed as D-032 intends. No cancel, standalone stop or sell was needed.
  - **Not exercised live:** these remain covered only by SimBroker tests:
    - the 20 s allowance and its landing;
    - a standalone stop, `stop_breached`, or an exit at work;
    - re-sends and `cancel rejected`;
    - the 30 s acceptance cap and `entry_stuck`;
    - a flatten with a position open.
- **Alpaca facts (new or confirmed):**
  - **`pending_cancel` never comes on the trade stream;** only REST shows it, as on 10-13.
  - **The open-orders list:**
    - It trailed `get_order` on every change read within 0.1 s, by at least 17–60 ms.
    - It never lists `held` legs.
    - Once it mixed a stale entry row with a fresh leg row.
  - **REST order payloads now carry `group_id` and `group_role`:** every 10-14 answer has them, and no 10-12 or 10-13 answer does. Stream payloads still lack them. D-051's note that live orders carry neither is out of date for REST.
  - **`submitted_at` is rewritten at acceptance,** for legs too. BWA's leg went 31.278 → 31.932 → 31.964.
  - **Fees:**
    - The overnight charge from 10-13 to 10-14 was $0.02 again (1000.29 → 1000.27, with no trades), against our $0.01 estimate.
    - Intraday, cash moved exactly by the fill notionals.
    - Tomorrow's boot cash (996.12 less tonight's charge) will show the charging unit for 5 sell fills on 2 orders.
  - **The PC clock** ran 0.20–0.22 s behind Alpaca at the open and 0.41 s behind exchange time by 16:00, a drift of about 33 ms/h. Boot measured 0.165 s.
  - **Q2 is still open:** 14 symbols (SPY, QQQ, IWM and 11 longs) × 2 channels = 28, under 30. No 405.
  - **The order-API budget:**
    - Busy polling ran at 182–186 calls/min with one entry working, against the limiter's 180/min plus a burst of 10.
    - The steady state was 27/min, with no 429s.
- **The golden replay** (G3's last step):
  - **The fixture:** `tests/golden/recordings/2026-10-14/` holds:
    - the recording, with the stream cut to every trade plus at most one quote per symbol every 10 s (81 MB → 2.1 MB; 3.3 MB in all);
    - the three config files that ran, whose SHA-256s matched `boot.json`;
    - the live journal's signals, final decisions and trades.
    - `tests/fixtures/g3_day.py` rebuilds all of it.
  - **Quotes don't matter to the replay:** they feed only R-016's quote age, which the trades keep fresh. Trades only, quotes every 10 s and quotes every 1 s all replay identically.
  - **The test:** `test_the_g3_day_replays_as_it_was_recorded` runs in 22 s. It requires:
    - DONE, with no divergence;
    - the recorded rows reproduced;
    - the replay's journal equal to `tests/golden/g3_2026-10-14.json` (`UPDATE_GOLDEN=1` rewrites it).
    - It gives the same result under two hash seeds.
  - **Against the live journal:**
    - Fills, trades and the ledger are reproduced exactly.
    - Orders, decisions, risk checks, signals, shadow outcomes and the P3 match apart from timestamps that differ by milliseconds.
    - Only `max_intraday_dd` differs (8.96 vs 9.47), because it samples marked equity every 10 s.
  - **The code that ran is commit cc6eb60.** `boot.json` says 24c7814 because the tree was uncommitted.
- **Review findings:**
  - Four reviewers each covered one area, and a separate verifier tried to refute every finding. None was refuted, and none blocks G3.
  - **Major (measurement, not trading): shadow entries on a gap-through trigger bar.**
    - S§7.2 books the trigger price even when the trigger bar opened past it.
    - CRWG's shadow short was booked at +5.08 R. Entered at the bar's open, it's +3.07 R.
    - A verifier re-simulated the backtest cache:
      - 562 of 2,260 triggered signals (25%) gapped through their trigger;
      - with bar-open entries, mean shadow R falls by 0.11 R for longs and 0.17 R for shorts;
      - D-042's optimistic long bound falls from +0.135 to +0.022.
    - **The human chose to plan this together with the trigger-minute rule.**
  - **Minor:**
    1. **Paper charges fees, but D-034's paper cash check leaves estimates out.**
       - The difference (−0.04 at the 10-14 EOD) grows by each day's fees and reaches the $1 tolerance in about 20–50 sessions.
       - From then on every EOD halts the book with `cash_mismatch`. This is D-052 item 5.
    2. **`_exit_covers` times its 20 s from Alpaca's `submitted_at`,** which paper rewrites at acceptance.
       - An exit accepted after 10 s counts as cover for about 30 s.
       - SimBroker doesn't rewrite `submitted_at`, so its test can't see this.
    3. **`exposure()` marks a symbol at cost when its IEX price is stale,** not at the broker's mark as D-047 says.
       - BWA's price was stale for 369 s of a 1,715 s hold, which hid up to −$1.28.
    4. **`trades.entry_delay_ms` subtracts a PC time from a broker time.**
       - BWA stored 892 ms (about 695 ms on one clock); FAST stored 1175 ms (about 977 ms).
       - The skew is checked only at boot.
    5. **The book's pre-gate cash skip makes the reason depend on price.**
       - STT got `insufficient_settled_cash` although R-007 also bound; the right reason would be `capacity_entries`.
       - D-044 ranks capacity first. The backtest uses the same order.
    6. **Busy polling uses the whole 180/min limiter with one entry.** Two entries at the open exceed it; run 1's P1 window was limiter-paced.
    7. **`boot.json` records only HEAD's SHA,** not whether the tree was dirty.
  - **Notes:**
    - **Counts and statistics:**
      - `daily_stats.n_signals` and `n_triggered` count shorts and live triggers, unlike S§7.1. Today they equal the SIP counts.
      - `max_intraday_dd` is the sampled peak-to-trough of marked equity, on a fee-excluded basis. Today's 9.47 is FAST's +$5.4 unrealized given back plus the loss. The specs don't define it.
      - Fee estimates are rounded per fill: 0.06 today, against 0.04 per order or 0.03 per day.
      - Trade P&L and VWAPs are unquantized when fills have mixed prices.
    - **Protection timing:**
      - `pending_new` stops count as cover (D-032), while acceptance at the open takes 10–14 s.
      - The 1 s grace and R-020 compare broker times with local now; the gap was 0.2–0.4 s today.
      - R-016's quote age is clamped to 0, because exchange timestamps run ahead of the PC.
    - **Records, logs and the report:**
      - The backup is taken before DONE is journaled.
      - The report's "regime in force (prior session)" line includes today's calendar tags.
      - No log line marks entry cancels, exits or the flatten passes.
      - The P3 doesn't give the bare time or what covered the shares.
      - Armed longs that never trigger get no decision row; `signals.status = expired` is their record.
    - **Triggers and data:**
      - IEX lag and the odd-lot rule delayed 4 of 8 live triggers by 1–7 minutes. KLRA (+1.14 R on SIP) was chased as a result.
      - IEX and SIP opening ranges disagreed on 5 of 20 directions, and 8 of 11 long triggers lay inside the SIP range.
      - The pessimistic trigger-minute rule stored BWA and FAST as stopped in their trigger minute. Live, they held 29–30 minutes, with the same R.
      - Eight sub-$1 symbols were eligible, with ATR above their price. 29 of the 192 `no_bar` symbols are under $5.
    - **Config:** `partial_fill_cancel_max_s` has a floor but no ceiling in `caps.py`; setting one is the human's call.
- **Next:**
  - **Phase 3 is done.** Its done-when list is met: this session, its replay, and the backtest with H4's direction noted in D-042.
  - A plan for the minor fixes.
  - A plan for the shadow-entry rule together with the trigger-minute decision; it restates D-042's shadow numbers and H4.
  - Both land before G4b, because Book B's veto value and the review are judged on shadow outcomes. Neither blocks Phase 4's coding.

## D-055 — P4.0a: fees booked from Alpaca's FEE activities; D-054's fixes #2, #3, #4 and #7
- **Date:** 2026-10-15 · **Status:** Accepted. The human approved the Phase 4 plan on 2026-10-14 and chose to book Alpaca's actual fees.
- **Fees (amends D-034; D-019's "paper accounts don't charge them" is out of date, D-052 and D-054):**
  - **The broker port** gains `fee_activities(after)`: `GET /v2/account/activities/FEE`, oldest first, 100 per page, paged by the last row's id, at most 50 pages.
    - Each row keeps `id`, `date`, `activity_sub_type` (REG, TAF, CAT), `net_amount` (negative for a charge), `status` (`executed`, `correct`, `canceled`), `symbol` and `created_at` (when it posted).
    - `after` goes in whole seconds, the form Alpaca documents ("YYYY-MM-DD and YYYY-MM-DDTHH:MM:SSZ").
    - A row the adapter can't read is a `BrokerDataError`, and a 404 a `BrokerUnavailable`, so the callers' P3 path takes both.
    - Checked against Alpaca's docs on 2026-10-14. The docs don't say:
      - whether paper lists these activities;
      - what the statuses mean;
      - whether `after` filters by posting time or by date.
    - They say fee activities are created "typically the day after the trade date (in UTC)", and that `date` is when the activity occurred or settled. So a charge may be dated its trade day, the next day or its settlement day.
  - **Booking** (`SettlementLedger.book_posted_fees`) runs at boot, before E is computed, and in EOD's new `fees:<book>` step, before the stats. One transaction reads the ledger and writes:
    - each charge not booked yet: kind `fee`, ref `fee:<id>`;
    - a booked charge the broker has since canceled: `fee-void:<id>`, which undoes it;
    - for a trade date whose charge has posted: one `fee-est-reversal:<trade>:<settle>` row per trade and settle date, retiring its estimates.
  - **What a booking reads.** The read starts at midnight of a day, so it works whichever way Alpaca filters `after`. That day is the day before the oldest trade date whose estimates are still open, or 10 days back, whichever is earlier, and never earlier than the day before the seed's day.
    - **Weeks between sessions** therefore miss nothing, and a charge up to 10 days late is still booked.
    - **Charges the seed already paid** are dropped on our side: those that posted at or before the seed, or, with no posting time, those dated before the seed's day.
    - `boot.json` records each book's seed time, so a replay drops the same ones.
  - **Which estimates retire.** A trade date's estimates retire once a charge that posted after that day's close is booked.
    - Alpaca charges a day's fees after its close, whatever date it gives them, so this holds under every dating.
    - A day that sold but has no charge of its own counts as charged nothing.
    - Today's estimates always stay.
    - With no posting time, only a charge dated a later day retires a date. The estimate then stays a day longer, on the safe side.
  - **Dating.**
    - A charge's row is dated the earlier of its activity date and the day it posted: the cash left then.
    - Charges and voids count toward E from the start of that day, like deposits, since they post outside the session.
    - Reversals are `fee` rows dated like the estimates they retire, so they net out in every ledger rule.
    - Today's realized P&L counts only today's estimates.
    - So E, settled cash and P&L come out right under every dating.
  - **Intraday, the per-fill estimates stay,** so settled cash stays conservative until the broker's charge replaces them.
  - **The EOD cash check is the same for paper and live.**
    - Ledger cash counts everything except today's estimates, which the broker charges after the close (`cash(entries, unposted_from=day)`). Earlier days count as their charged fees once booked, and as estimates until then.
    - The residual (broker − ledger) is stored in `daily_stats.cash_residual` (migration 0005).
    - Past half the tolerance it is a P3 `cash_drift`. Past the tolerance it is the `cash_mismatch` halt, as before.
    - `daily_stats.start_equity` is now the ledger's start of day after the fees step, so a charge booked at EOD for an earlier day stays out of today's `pnl`. Boot's E is still what the day traded with.
  - **If paper lists no FEE activities,** nothing is booked and the estimates stand in for the charges.
    - The residual then records the difference: about −$0.01 per sale day at 10-13's rate, a P3 after roughly 20–50 such days.
    - That is the plan's fallback, and it needs no code.
  - **Assumptions to check:**
    - **Status `correct`** is booked as one more charge. That is right if Alpaca cancels the original and books the correction, and double-counts if the correction silently replaces it.
    - **Live:** Alpaca's regulatory-fees page says live accounts accrue pending fees intraday. If live `cash` reflects them, today's estimates would need counting in live's check. Recheck in Phase 7.
  - **`trader fees --book A|B|all --days N`** lists the activities and the account's cash. It is read-only, but it opens the journal, so it applies migration 0005.
- **D-054's fixes:**
  - **#2 Stuck exit.**
    - An exit at work counts as cover for 20 s from our own send, as the journal stamps it, not from Alpaca's `submitted_at`, which paper rewrites at acceptance. The broker's time is used only for an order the journal doesn't have.
    - SimBroker now rewrites `submitted_at` at acceptance, as paper does.
    - **The loop wakes when an exit's cover ends,** so R-020's 5 s run from the lapse. A deadline that passes during a slow tick comes due at once.
      - Before, the loop slept 5 s from its last wake. An off-grid wake (another order's update), or a tick straddling the lapse, could push the P1 to send + 30 s.
      - That predates P4.0a; the review found it.
  - **#3 Stale marks.**
    - `exposure()` marks a symbol whose IEX price is stale at the broker's `current_price` from the last positions read, and at cost only when neither exists.
    - Ledger equity now leaves out only today's fee estimates, not all of them. R-019's band therefore compares it more closely with broker equity, and nothing is loosened.
    - The standard golden day's `max_intraday_dd` falls from 15.70 to 3.86 from the marks alone. G3's is unchanged.
  - **#4 Entry delay.**
    - `trades.entry_delay_ms` is when we booked the first fill (its ledger row's `created_at`) minus `triggered_at`, both on our clock. G3's golden: BWA 892 → 703 ms and FAST 1175 → 985 ms.
    - It is NULL when the booking came more than 5 s after the broker's fill time, i.e. a fill caught up after a restart or an outage. Otherwise it would measure the outage.
    - **The skew is read every hour** after boot's reading, and journaled in `day_runs` (kind `clock`, name `skew`).
      - Past 2 s it is a P2 `clock_skew`, once per episode.
      - It only alerts: boot's check alone decides whether entries are armed. A failed reading is only logged.
  - **#7 Provenance.**
    - `boot.json` gains `source_sha256`: a hash of `src/trader` (`.py`, `.sql`), `prompts/*.md` and `schemas/*.json`, in POSIX path order, with CRLF read as LF. An uncommitted tree now has a name.
    - Replay notes "the code changed since the recording" when it differs.
- **Capabilities** (`orchestrator/capabilities.py`).
  - A live run grants `fees_v1` and `skew_hourly`, and `boot.json` lists them. A replay grants only what its recording lists, so a day recorded before a capability existed makes none of its calls.
  - G3's golden replays with no fee call and no hourly reading, still with zero divergences.
    - Its `cash_residual` is −0.02. That replay books no fees, so the $0.01 estimates of 10-12 and 10-13 stand in for their $0.02 charges.
  - Replay matches fee reads by time alone, like account reads. Their `after` comes from the ledger, and a replay starts from `boot.json`'s ledger, where boot's booking is already done.
- **Smaller items (D-054's notes):**
  - **Log lines:** `fees booked`, `clock checked`, `entry cancel sent` (with why), `position opened`, `position closed` and `flatten pass`.
  - **The P3 `partial_fill_protected`** now says how long the shares were bare and what covered them, e.g. "FAST: bare 1.0 s after a partial fill; 10 shares now under a stop".
  - **The backup** is taken after DONE is journaled, and still on every failure path. It is best effort: a SQLite error there is logged, not raised.
- **Deviations from the approved plan:**
  - **One migration per part.** 0005 holds only `cash_residual`. Applied migrations never change (D-040), so a single 0005 would freeze Phase 4's other columns before their code exists. Later parts add 0006 onward.
  - **`cash_drift`, not `fee_drift`:** the residual isn't only fees.
  - **Capabilities** instead of a replay rule that ignores a surplus clock call.
  - **The reversal is a `fee` row per trade and settle date,** not an `adjust` per date. An `adjust` would count toward E from its own day; a `fee` row dated like its estimates nets them out exactly.
- **Statements of current behavior this updates:**
  - **D-019:** paper does charge fees.
  - **D-034:** the paper EOD check.
  - **D-035 and R-019:** ledger equity.
  - **D-044:** `entry_delay_ms`.
  - **D-047:** `boot.json`'s contents.
  - **D-048:**
    - BOOT books fees before E and starts the hourly reading;
    - the EOD steps;
    - the backup comes after DONE.
  - **D-049:** replay grants the recorded capabilities, notes a changed source, and matches fee reads by time.
  - **D-053 item 6:** `accept_delay` now rewrites `submitted_at`.
  - **D-054's notes:**
    - `max_intraday_dd` now leaves out only today's estimates;
    - the backup order;
    - the log lines;
    - the P3's detail.
- **Review.** Four reviewers (money, the broker port, execution, the orchestrator) each had a verifier who tried to refute every finding, then a completeness critic.
  - **Major, 2, both fixed:**
    1. **The fixed 10-day window lost charges** after weeks between sessions, and a later charge then retired their estimates, overstating cash.
    2. **Fee reads were keyed by an `after` taken from the clock,** so every replay of a live day that booked fees would diverge. Three reviewers found this.
  - **Minor, all fixed:**
    - next-day dating put a charge in the wrong day's E and realized P&L;
    - a first seed booked charges its cash had already paid;
    - an unreadable FEE row or a 404 aborted BOOT or EOD;
    - microseconds in `after` (an undocumented form; unconfirmed);
    - an exit's lapse could be seen up to 5 s late (pre-existing);
    - a restart put the outage into `entry_delay_ms`;
    - a backup error after DONE escaped `run()`;
    - a charge booked at EOD landed in today's `pnl`;
    - PLAN's migration wording.
  - **Refuted:** one finding, on the source hash's path order. It was hardened anyway.
  - **Test gaps, all filled:** the boot booking, the `fees_unavailable` P3s, the capabilities round trip, and the replay's note.
  - **A second pass tried to break each fix.** All held, and it found three more, now fixed:
    - **The seed cutoff at the seed's time of day** could drop the seed day's own charges if Alpaca filters `after` by date. Book A's 10-12 sale came a minute after its seed. Hence the read from a midnight, with the seed applied on our side.
    - **"The latest charged date"** could retire an estimate before its charge under next-day dating. Hence the posting-time rule.
    - **A lapse during a slow tick was still lost.**
- **Tests:**
  - New tests per file:
    - SimBroker 2;
    - Alpaca 6 (one has 4 cases);
    - fees 17;
    - EOD 4;
    - execution service 4;
    - orchestrator 5;
    - replay 1;
    - golden 1, a day that books fees at boot and EOD, recorded and replayed;
    - run-day CLI 3;
    - trades 2 (one replaced).
  - The record-then-replay test now also checks the capabilities round trip and `day_runs`.
  - Each review fix's test fails without its fix. 763 tests pass.
- **Pending:**
  - **The human's probe,** `trader fees --book A --days 5`. It shows whether paper lists the charges, how it dates them, and whether they carry `created_at`. Its result is D-058.
  - **G4b exercises the boot and EOD bookings live** for the first time.

## D-056 — P4.0b: size skips named by capacity; orders ahead of polls; a polling budget (D-054 #5, #6)
- **Date:** 2026-10-15 · **Status:** Accepted, as part of the Phase 4 plan the human approved on 2026-10-14.
- **#5: a size skip names the capacity rule that also binds.**
  - **The rule:** when sizing gives no shares before the gate, the reason is:
    1. `capacity_positions` or `capacity_entries` if R-006 or R-007 also binds, ranked as the gate ranks them (D-044);
    2. else `insufficient_settled_cash` if the settled cash can't buy one share;
    3. else `qty_zero`.
  - **Why:** the reason no longer depends on the price. On 10-14, STT got `insufficient_settled_cash` only because it cost more than the $20.39 left, although its two entries had already used up R-007.
  - **A label only:** the same signals are skipped, in Book A and in the backtest. The decision's detail adds e.g. "capacity: R-007".
  - **One test for capacity:** `gate.capacity_violations()` is that test, used by R-006 and R-007 too, with the caps on top. The reviewers confirmed:
    - identical gate output on 10,080 states;
    - identical Book A take/skip decisions and intents on 336 exposures;
    - a G3 replay whose broker calls are byte-identical with and without the label.
  - **G3's golden replay:**
    - `journal.json` stays the live truth. `g3_day.CODE_CHANGES` lists the two diff lines from STT's new reason, citing this entry, and the test asserts that the replay's diff equals them.
    - The golden journal changes in STT's reason and detail only.
    - So `trader replay --date 2026-10-14` now reports those 2 differences and exits 1, by design, as D-053 noted for 10-13.
  - **The synthetic days:** CAP gets `capacity_entries`.
    - In the orchestrator's normal and crash-restart days it was `insufficient_settled_cash`.
    - The golden day already had `capacity_entries`: WIN's partial fill leaves cash for 8 shares, so the gate rejects CAP on R-007.
  - **D-042's skip split** (settled cash 360, the day's entries 326, …) was counted under the old labels. P4.0c's backtest re-run restates it.
- **#6: the polling budget.**
  - **The problem (D-054):** with one entry working, busy polling took 182–186 calls a minute, against the limiter's 180 a minute plus a burst of 10. Two entries at the open exceed that, and run 1's P1 window was paced by the limiter.
  - **The limiter** (`data/ratelimit.py`):
    - POST and DELETE requests (submits, cancels, market sells) are urgent. They wait in their own first-in-first-out queue, go ahead of every routine waiter, and may use the reserve.
    - Routine reads stay first-in-first-out and never take the bucket below 4 tokens (`ORDER_RESERVE`, on each book's trading limiter only).
    - Any 60 s still sees at most capacity + rate = 190 calls, under Alpaca's 200.
    - A reviewer's simulation with saturating polls found the longest urgent wait was 0.25 s with the reserve, and 10 s with neither the reserve nor the queue rule.
  - **The service** (`execution/service.py`):
    - **The loop** still ticks every second while anything is busy.
    - **Positions and open orders** are read:
      - every second while shares are bare or a stop is pending (R-020);
      - every 2 s while only entries are open;
      - else every 5 s.
      - Any order event, and any read that retires an entry or books a fill, asks for a read at once. A refresh is stamped before its reads, so an ask that arrives during them survives.
    - **An entry is read every tick:**
      - before its first read;
      - while it waits for acceptance (D-053's clock);
      - after a cancel, since Alpaca never streams `pending_cancel`;
      - while partly filled (the 1 s grace);
      - in the last 2 s before its timeout;
      - while the book is closing.
    - **Otherwise a working, unfilled entry is read every 2 s,** timed from its last read. Between reads, decisions use its latest snapshot, from the stream or the read.
  - **The result:**
    - Two working entries make 120 calls a minute (the old rule: 240); one makes 90 (old: 180).
    - Entries waiting for acceptance are still read every tick: two waiting make about 180 a minute until paper accepts them, as at the open. The urgent queue and the reserve keep order calls from waiting behind those polls. Waiting entries can't fill, so R-020 has nothing to do then.
  - **Deviations from the plan's wording:**
    - **Timing.** The plan said "skip `get_order` when the stream reported that order within the poll interval". Timing the skip from the stream made the set of reads depend on how stream events and ticks interleave, so a replay asked for orders the live recording never read: the record-then-replay tests diverged. Timing from the last read, with every entry read at least once, removes that, and nearest-time matching (D-049) absorbs the rest.
    - **Cadence.** With the plan's wording alone, two working entries would still make about 180 calls a minute, over the plan's own target of 150. So a working entry is read every 2 s, not every second.
- **Review.** Three reviewers (labels, the limiter, polling) each had a verifier, then a completeness critic. There was no blocker, and nothing in the labels.
  - **Confirmed and fixed:**
    1. **A fill learned from a read (a quiet stream), or from an event that arrived during a refresh's reads, asked for no fresh view.** With a rejected leg, the shares then waited up to 5 s, and R-020 fired a P1 instead of trying a stop first (RISK.md §5). This predates P4.0b, which shifted its timing.
    2. **Replays near a time-based cancel.** A live tick spends milliseconds a replay doesn't. Near the entry timeout or a partial fill's grace, live could act on the stream's fill while its replay acted on a 2 s-old recorded read, giving a cancel the recording lacks.
    3. **Untested behavior:** nothing tested the 2 s re-read, the urgent caller's priority when it is already waiting, or that the broker's limiter has its reserve.
  - **Refuted:**
    - "Two waiting entries still poll at 180 a minute": that is the design, as stated above.
  - **Noted:** a halt raised between the book's inputs and its sizing still gets a capacity or cash label, not `book_halted`. That window was already there for the cash label, lasts milliseconds, and affects the label only.
- **Statements of current behavior this updates:**
  - **D-032:** "open entries are polled every 1 s".
  - **D-037:** the loop's read cadence.
  - **D-041 and D-044 item 3:** the size-skip reason.
  - **D-054:** `trader replay` of 10-14 matching (see above).
  - **STRATEGY §3.5:** now states the rule.
  - **ARCHITECTURE:** §3's continuous row, §4.2's limiter and §4.6's step 3 are updated.
- **Tests:**
  - New tests per file:
    - gate 1;
    - books 1 (4 cases);
    - backtest 1;
    - rate limiter 4;
    - Alpaca broker 1, plus the reserve assertion;
    - execution service 5:
      - the budget;
      - a partial fill the stream drops;
      - a full fill the stream drops;
      - an event during a refresh;
      - a fill during a refresh;
    - `test_polling_replay.py` 2 (5 cases: fills at the timeout and at a partial fill's grace, recorded with 3 ms of live time per tick, then replayed).
  - **Changed:** 2 orchestrator expectations (CAP), and G3's comparison now asserts `CODE_CHANGES`.
  - Each fix's test fails without it: four of the five replay cases diverge on the first version, and the old polling makes 240 calls a minute.
  - 784 tests pass.

## D-057 — P4.0c: shadow outcomes enter at the open past the trigger; ambiguous trigger minutes from the SIP tape
- **Date:** 2026-10-15 · **Status:** Accepted. In the Phase 4 plan (2026-10-14), the human chose to resolve the trigger minute from the SIP trade tape, together with D-054's gap-through finding. The backtest's restated numbers come in a later entry, after the human's tape fetch.
- **The problem:**
  - **D-054's review:** S§7.2 entered every shadow at trigger + slippage, even when the trigger minute opened past the trigger. CRWG was booked at +5.08R; from the bar's open it is +3.07R.
  - **D-042:** D-041's rule counts a stop touch in the trigger minute as a stop. That stopped 39% of triggered longs in their own minute, though 1-minute bars can't tell whether the touch came before the entry. Long shadow R lay anywhere from −0.31 to +0.14.
- **The rule** (`memory/shadow.py`; S§7.2 amended):
  - **Entry:** max(trigger, the trigger minute's open) + slippage, for every signal; shorts min(trigger, open) − slippage. Slippage stays a function of the trigger.
  - **Unambiguous minutes** are settled by their bar (`trigger_resolution = bars`):
    - opened past the trigger: the entry came first, so a stop touch in that minute is a stop, at stop − $0.01;
    - closed at or past the stop: the touch came last, also a stop;
    - never touched the stop: no stop in that minute.
  - **An ambiguous minute** (a long's opened below the trigger, touched the stop and closed above it; shorts mirror) is read from the SIP trade tape (`tape`):
    - the first print at or past the trigger orders the minute;
    - the trade is stopped if a later print in the minute is at or past the stop, at min(stop, that print) − $0.01 (short: max + $0.01);
    - otherwise the walk goes on from the next bar, by D-041's rules;
    - MFE and MAE count only the prints the trade held in that minute: from the entry print through the exit, or to the minute's end. The whole bar would count a pre-entry touch, or a high printed after the exit.
  - **Which prints count:**
    - Alpaca's `u` marks `canceled` and `incorrect` prints void. Its docs define `incorrect` as the original of a corrected trade and `corrected` as the valid replacement, so a `corrected` print counts. The plan's "drop corrected" had them the wrong way round; the docs were followed.
    - Prints with one of D-043's excluded conditions (odd lots and the rest) don't count either: they set no bar price.
  - **No usable tape** (none read, a refused read, or no print at the trigger) means D-041's pessimistic stop (`fallback`). When a tape was asked for and didn't settle the minute, the day raises a P3 `shadow_tape_unresolved` naming the symbols.
  - **A limitation, kept and measured:**
    - The entry price still comes from bars. A minute that opened below the trigger but first printed far past it still enters at trigger + slippage. The synthetic GAPPER shows it: first print 51.00 against a 50.41 trigger.
    - `ShadowOutcome.tape_entry` keeps that first print. The backtest report counts the tape-read longs whose first print lay past the chase cap, so the restatement can quote the bias.
- **The data** (checked against Alpaca's docs, 2026-10-15):
  - **`AlpacaRest.trades()`** calls `GET /v2/stocks/trades`:
    - `start` and `end` are inclusive, `limit` is 10,000 (the maximum, across all symbols), sorted ascending, with an optional `asof`;
    - every `next_page_token` is followed, since a page may hold fewer rows than the limit with more to come;
    - each print keeps its `t`, `p`, `s`, `c` and `u` as a `SipTrade`.
  - **The live job** makes one request per ambiguous signal, for [minute, minute + 1 min], and drops prints outside the minute. The free plan's 15-minute SIP delay is long past at close + 12 min.
  - **Recording and replay:** the request is recorded and replayed like the bars, since its key comes from the bars and the signal alone. A replayed REST error now reads as it did live: the recording keeps the raw message, and a replay drops the prefix older recordings stored.
  - **The capability `shadow_tape`** gates it. A day recorded before it (G3's golden) asks for no tape and replays unchanged, with its ambiguous minutes marked `fallback`.
  - **Migration 0006** adds `shadow_outcomes.trigger_resolution`.
- **The backtest:**
  - **`trader backtest --tape`** fetches the tape of each ambiguous minute not yet cached (`bt_tape`, `asof="-"`). A refused read isn't cached, so the next run tries it again. Without `--tape`, cached tapes are used and the rest fall back.
  - **Book A's leg** now takes each long's shadow outcome, from the same resolver. D-042's separate bar-open simulation is gone, since the shadow entry is now the same.
  - **The report** counts the trigger minutes:
    - settled by bars;
    - read from the tape;
    - fallen back with no tape (`--tape` fetches these);
    - fallen back on a tape with no usable print.
    - It also counts the tape-read entries past the chase cap. The per-signal Parquet gains `trigger_resolution`, `tape_read` and `tape_entry`.
  - **Fixed, from the review: a look-ahead in cached reruns** (D-042's guard).
    - The universe build caches OR baselines in `or_bars`, and a later split restates a symbol's rows in the new share basis. A rerun reaching a day before that split read those rows.
    - On the real cache this put DD (2026-06-23) and JEM (2026-07-01) into the top 20, displacing SH and IFF, and shifted 27 ranks. It also made two network requests a "cached" rerun shouldn't.
    - `prepare()` now rebuilds `or_bars` from `bt_or_raw` every run, at no request cost. A reviewer confirmed this reproduces D-042's selection exactly.
- **The goldens:**
  - **The standard day:**
    - CHASE, which gapped to 41.00, now enters there: shadow R +4.84 becomes −0.03.
    - CAP and GAPPER have ambiguous trigger minutes whose synthetic tape shows the stop touch first, so both hold to the flatten instead of the pessimistic stop. CAP goes from −1.09 to −0.15; GAPPER from −1.08 to +4.75. GAPPER's MAE of +4.75 is the entry limitation above.
  - **G3** (recorded before the capability):
    - CRWG's shadow short enters at the bar's open: +5.08 becomes +3.07, D-054's figure.
    - KBH enters at its gapped open.
    - Six ambiguous minutes keep their pessimistic numbers, marked `fallback`: AADX, BWA, FAST, FNGU, PGR and VRNS. D-054 noted that BWA and FAST held 29–30 minutes live.
- **Review.** Three reviewers (the rule, the data path, the backtest) each had a verifier, then a completeness critic. There was no blocker.
  - **Confirmed and fixed:**
    - MFE and MAE took the whole trigger bar where the tape had ordered it;
    - no test of the minute's end boundary, the short side of the ambiguity test, or the job's exclusions;
    - a replayed REST error doubled its prefix (the cause predates P4.0c; P4.0c's P3 quotes it);
    - the cached rerun look-ahead above (predates P4.0c);
    - the report lumped retryable and unusable tapes;
    - the backtest and CLI tape tests couldn't catch a tape read for every minute, or `--tape` never reaching the engine;
    - S§7.2's wording on the entry print.
  - **Refuted:** "nothing catches a replay that skips the tape reads". The goldens do; the record-then-replay test now compares `shadow_outcomes` too.
- **Statements of current behavior this updates:**
  - **D-041:** the entry; the trigger-minute stop, now only for minutes the bar settles and for the fallback; MFE and MAE over the whole trigger bar.
  - **D-042:**
    - the cache tables, which now include `bt_tape`, with `or_bars` rebuilt each run;
    - the Book A leg;
    - "about 850 small requests": the cache holds about 605 ambiguous minutes in that window.
  - **D-045:** the shadow job also reads one tape per ambiguous minute.
  - **D-047:** recorded REST errors keep the raw message.
  - **D-054:** its shadow finding is resolved here.
- **Tests:** 16 new:
  - shadow 7, including a replaced gap-through test;
  - shadow jobs 3;
  - Alpaca REST 1;
  - replay 2;
  - backtest 2;
  - orchestrator 1.

  The record-then-replay test and the backtest CLI test were extended. Each fix's test fails without it. 799 tests pass.
- **Pending:**
  - **The human's tape fetch:** `uv run trader backtest --start 2026-04-01 --end 2026-10-09 --tape`, outside 09:00–16:30 ET. About 605 requests, a few minutes.
  - **Then:** I re-run the report from the cache and restate D-042's numbers, its skip split (D-056) and H4 in a later entry.

## D-058 — D-055's fee probe: paper lists its FEE activities, dated the trade day and posted that evening
- **Date:** 2026-10-15 · **Status:** Accepted. The human ran D-055's read-only probe, `trader fees --book A --days 5`, at 14:04 ET. No code changes.
- **What paper listed** for Book A from 10-12 to 10-14:
  - Two rows per trading day, CAT and REG. All were `executed` and carried `created_at`. There was no TAF, and nothing `correct` or `canceled`.
  - `date` is the trade date. CAT posted at 20:05 ET and REG at 20:15 ET that evening. That is 00:05 and 00:15 UTC the next day, which matches Alpaca's "typically the day after the trade date (in UTC)".
  - The charges were $0.02 on 10-12, $0.02 on 10-13 and $0.04 on 10-14 (REG $0.03). Cash was $996.08.
- **D-055's rules hold on these rows:**
  - Each charge is dated its trade date. It retires that date's estimates at the next booking, since it posted after the close.
  - The seed paid none of them. Book A was seeded at 13:02:51 ET on 10-12, before that day's sale.
  - EOD runs before a day's charges post, so the check is right to leave out today's estimates.
  - Whether `after` filters by posting time or by date is still unknown, since every row passes both ways. Reading from a midnight covers both.
- **A dry run** of `fee_reconciliation` on a copy of the journal, with these six rows:
  - It books the six charges and three reversals: +$0.01 (10-12), +$0.01 (10-13) and +$0.06 (10-14).
  - Ledger cash and 10-15's E stay $996.08, the broker's cash: a residual of $0.00.
  - The first boot after 10-15 (G4b's, unless a day runs before it) books the same nine rows, plus any charge posted in between.
- **The estimates differ per day from the charges:** $0.01, $0.01 and $0.06, against $0.02, $0.02 and $0.04. Over the three days they sum the same, by chance.
  - **REG is the SEC fee alone,** rounded up per order or per day, not per fill. On 10-14, $975.75 sold comes to $0.0201 and was charged $0.03. Per fill it would be $0.05.
  - **FINRA's TAF is paused** until 2026-12-31 (settings.yaml's note). The estimate charges it at the 2027 rate, so it errs high.
  - **CAT costs a cent each trading day,** even on 10-14 with two round trips. The estimate has no CAT term, so a day with one small sale estimates a cent low, as 10-12 and 10-13 did.
  - **Where that shows:**
    - An estimate stands in only until the next booking, and never in its own day's EOD check.
    - So D-055's "settled cash stays conservative" holds to within a cent a day, and only when a booking fails, which is already a P3 `fees_unavailable`.
    - A day's realized P&L counts its estimates, so it can differ from that day's charges by a few cents.
  - No change. A CAT term can be added if those cents ever matter.
- **Still unchecked:**
  - D-055's assumption about status `correct`: no such row has appeared.
  - Live's intraday fee accrual (Phase 7).
- **Statements of current behavior this updates:**
  - D-055's pending probe, now done, and its "settled cash stays conservative" (to within a cent a day);
  - S§3.6's fee row: Alpaca also charges a CAT fee.

## D-059 — P4.0c closed: D-042's backtest restated under D-057's shadow rule; ranks 1–5 do best, with no gradient below them
- **Date:** 2026-10-15 · **Status:** Accepted. The human ran `trader backtest --start 2026-04-01 --end 2026-10-09 --tape` at 21:49 ET. This restates D-042's results from the run's report and Parquet. No code changes.
  - The run rewrote both files in place, so the report D-042 cites (`runtime/reports/backtest-2026-04-01-2026-10-09-iex.md`) now holds these numbers. D-042's own figures survive only in its text.
- **The run:**
  - It read the tape of all 605 ambiguous trigger minutes (313 longs, 292 shorts) in 7 minutes: 607 requests, all answered 200, two of them a second page. Apart from the calendar sync (one request), everything else came from the cache.
  - The selection is D-042's: 133 sessions; 2,660 signals (1,322 long, 1,280 shadow short, 58 doji); 2,260 triggered.
  - Trigger minutes: 1,655 settled by their bar and 605 by the tape. None fell back.
- **Checked offline,** first by a scratch re-simulation from the cache and then by three independent verifiers, with no requests:
  - It reproduces all 2,260 stored shadow R values exactly.
  - With D-041's rule (entry at the trigger; any stop touch in the trigger minute is a stop), it gives D-042's figures exactly: longs −0.307, shorts −0.176, both −0.242, with 39% of longs and 36% of shorts stopped in their trigger minute.
  - With D-042's optimistic rule, it gives D-042's +0.135 and +0.125, and +0.869 and +0.704 for ranks 1–5.
  - **Book A's leg, re-run on D-041's outcomes, reproduces every D-042 total:** 265 trades, 11.7% winners, −0.295R, −$0.76 a trade, profit factor 0.68, $798.31, a 28.2% drawdown and a Sharpe of −2.44. D-042's own Parquet is gone, so the match is in totals, not trade by trade. Its leg already entered at the bar's open.
- **Book A's long-only leg** (D-042 → now):
  - 265 trades; winners 11.7% → 13.2%; mean gross R −0.295 → −0.243;
  - expectancy −$0.76 → −$0.63 a trade, net of fee estimates; profit factor 0.68 → 0.742;
  - $1,000 → $832.41 (−16.8%), not $798.31 (−20.2%); max drawdown 28.2% → 25.2%; daily Sharpe −2.44 → −1.84. SPY returned +19.7%.
  - The tape is the whole change: 69 of the 265 trades had an ambiguous trigger minute.
- **Skips, under D-056's labels.** Of the triggered longs, Book A didn't take 877:
  - 540 for the day's entries (R-007), 242 for open positions (R-006), 94 chased, and 1 for settled cash (TMO at $656.24 on 09-18).
  - D-042 counted settled cash 360, the day's entries 326, open positions 97 and chased 94: the same 877.
  - **Two changes moved them.**
    - Under D-041's outcomes, D-056's labels alone give 618, 164, 94 and 1: nearly every cash skip also hit a capacity rule, which D-056 now names.
    - The tape's longer holds then move 78 skips from the day's entries to open positions: more positions are still open at a later trigger.
  - **Every capacity skip is R-007's.** In this leg positions never outnumber the day's entries, and both caps are 2, so every open-positions skip also hit R-007. The label only says whether a position was still open, since R-006 is named first (`books/reasons.py`).
- **Shadow R** (every triggered signal):

  | | D-042, pessimistic | D-042, optimistic | now | now, winners |
  |---|---|---|---|---|
  | longs (1,142) | −0.307 | +0.135 | **−0.072** | 14.4% |
  | shorts (1,118) | −0.176 | +0.116 (re-simulated; D-042 gave none) | **−0.211** | 15.2% |
  | both (2,260) | −0.242 | +0.125 | **−0.141** | 14.8% |

  - **Two changes pull opposite ways:**
    - **Entering at the open.** 562 trigger minutes opened past the trigger (302 longs, 260 shorts), the 562 D-054 counted. 76 more opened exactly at it, which changes nothing.
      - Entering the 562 at their open lowers the mean of all triggered longs by 0.110R, shorts by 0.171R and both by 0.140R. That is 0.42R, 0.74R and 0.56R per signal that opened past the trigger.
      - Entered at the trigger, a price the minute had already opened past, the 562 averaged +0.369R; at their open, −0.195R.
    - **The tape** gives back 0.345R to longs, 0.136R to shorts and 0.241R to both. In 441 of the 605 ambiguous minutes (221 of 313 longs, 220 of 292 shorts), it found the stop touch came before the entry.
    - **Trigger-minute stops** fall from 39.2% to 19.9% of longs, and from 36.4% to 16.7% of shorts.
    - **The bracket.** With D-057's entry and a trigger-minute stop at stop − $0.01, bars alone give:
      - −0.417 (every ambiguous minute stopped) to +0.019 (none stopped) for longs;
      - −0.347 to −0.128 for shorts;
      - −0.382 to −0.053 for both.
      - The tape lands 79%, 62% and 73% of the way up.
      - These bracket the answer rather than strictly bound it: 124 of the tape's 164 trigger-minute stops filled below stop − $0.01, at the print.
      - D-054's +0.022 for longs used D-042's optimistic rule, which also spares minutes that opened at or past the trigger. D-057 stops those.
  - **One signal moves the long mean by 0.11R.** On 2026-08-19, MRNA (rank 1, gapped +84.5%) triggered at $122.10, with its stop $0.38 below.
    - Book A didn't take it: TJX and FBIN had used the day's two entries at 09:35.
    - Its trigger minute opened at $119.57. The first print at the trigger came at 09:37:53, and every usable print after it that minute stayed at or above $121.78, $0.06 over the stop. D-041's rule had stopped it at −1.02R.
    - It then ran to $174.63 at the flatten: +126.7R.
    - It is real by the rules and the data, so it stays. Without it, longs average −0.183R and both −0.197R.
  - **The shape:** about 85% of triggered signals lose about 1R, and the median is −1.04R to −1.05R for both sides and in every rank bucket. The mean comes from the 5–6% that make more than 5R.
- **H4** (mean shadow R by rel_vol rank):

  | ranks | longs, D-042 | longs, now | both, D-042 | both, now |
  |---|---|---|---|---|
  | 1–5 | −0.213 | **+0.530** | −0.071 | **+0.184** |
  | 6–10 | −0.291 | −0.382 | −0.248 | −0.278 |
  | 11–15 | −0.498 | −0.310 | −0.382 | −0.295 |
  | 16–20 | −0.205 | −0.087 | −0.267 | −0.174 |

  - **Verdict: ranks 1–5 do best, but there is no gradient below them.**
    - The report's H4 test (ranks 1–5 above ranks 16–20) passes, for longs and for both. Under D-041's rule, longs failed it.
    - Below the top, longs' ranks 6–10, 11–15 and 16–20 average −0.382, −0.310 and −0.087: they rise as rel_vol falls.
    - Across signals the rank correlation is about zero: Spearman +0.01 for longs and +0.03 for both, where H4 predicts a negative one.
    - So the data show a top-bucket effect, not H4's ordering.
  - **Even the top bucket is thin.** The 95% intervals below come from resampling the 133 days 10,000 times:
    - Longs' ranks 1–5 average +0.530, interval −0.22 to +1.70. They beat the other three buckets' mean by 0.79R, interval +0.01 to +1.98.
    - Without MRNA, longs' ranks 1–5 average +0.063, still the best bucket. They beat the rest by 0.32R, interval −0.06 to +0.73; the resampled advantage was zero or less 5% of the time. Both sides: −0.042, against −0.174 for ranks 16–20.
    - With every R capped at +20, longs' ranks 1–5 average +0.137, still the best.
    - So ranks 1–5 come first under every check, but a few large winners set the margin.
- **D-057's entry limitation, measured.** Suppose each tape-read minute entered at its first print at or past the trigger, plus slippage, instead of at the trigger plus slippage:
  - Longs would go from −0.072R to −0.084R, and shorts from −0.211R to −0.222R. H4's order wouldn't change.
  - **R understates the cost.** A later entry also widens the risk, which lifts the R of every exit below the stop. In dollars, Book A's 265 trades at the same sizes would lose $20.17 more ($187.76, not $167.59).
  - **Live, Book A would have skipped COGT, TRU and DNA as chased.** Through the leg, EWU, NBIL and LKQ take their places, and Book A ends at $827.04 instead of $832.41 (mean R −0.227, max drawdown 25.6%).
  - **The first print** lay a median 0.017% past the trigger (90th percentile 0.16%, at most 0.71%).
    - It lay past the 0.30% chase cap for eight longs: WFRD, COGT, AXGN, TWST, SAIL, TRU, DNA and EPAM.
    - Seven of them lost 1.0R to 1.4R, and TWST made +21.8R.
  - The effect is small ($5.37 on the book over six months), so the rule stays.
- **For later phases** (facts; no change):
  - **Over this window the rules lose.** Book A lost 16.8% while SPY gained 19.7%.
    - Every mean shadow R here is negative except ranks 1–5 of longs (+0.530; +0.063 without MRNA) and of both sides (+0.184; −0.042 without MRNA).
    - Shorts' ranks 1–5 average −0.139.
  - **Book A's choice of trades isn't measurably worse.** Its trades averaged −0.243R, against −0.165R for the 876 triggered longs it didn't take, without MRNA. The gap, −0.08R, is noise: the day-resampled 95% interval runs from −0.45 to +0.30.
  - **Book B's veto value** (Phase 5) will be measured on live shadow outcomes from this same rule. With 15% winners and a few outliers setting the mean, its intervals will be wide.
    - S§7.3 names no resampling unit. Resampling by day, as here, is a proposal for P5.1.
    - S§7.3's intervals are 90%; this entry's are 95%.
- **Statements of current behavior this updates:**
  - **D-042:**
    - its results, all restated here;
    - its "Until then, quote both bounds" no longer applies;
    - its "about 850 small requests": it took 605 tape reads (607 requests);
    - the report and Parquet it cites now hold this run's.
  - **D-054:** its bar-open optimistic bound (+0.022 for longs) used D-042's optimistic rule.
  - **D-056:** its pending restatement of D-042's skip split, now done.
  - **D-057:** its pending restatement, now done.

## D-060 — D-058's follow-ups: a charge retires only its own day's estimates and earlier ones; CAT is not negligible
- **Date:** 2026-10-15 · **Status:** Accepted. Three independent agents verified D-058 offline. They confirmed every figure and found what follows. D-058 is committed, so its corrections are recorded here.
- **A fix in `fee_reconciliation`** (`risk/ledger.py`; the test came first):
  - **The flaw.** D-055 retired a trade date's estimates once *any* booked charge had posted after that day's close.
    - So an older day's charge posting late, after a later day's close, retired the later day's estimates before its own charge had posted.
    - That day then counted as charged nothing until its charge came: its whole fee, not a cent, and with no alert.
  - **The fix.** A charge now retires a day's estimates only if it posted after that day's close and its cash left that day or later (`_charge_day`, D-055's dating).
    - So an older day's late charge leaves a later day's estimates alone.
    - Every earlier case is unchanged: next-day dating, a day with no charge of its own, and a charge with no posting time.
  - **Whether it happens:** only with out-of-order posting, which the probe didn't show (each day's charges posted that evening).
  - **D-058's dry run** on the real journal gives the same nine rows and a $0.00 residual.
  - **Test:** `test_an_older_days_late_charge_leaves_a_later_days_estimates` fails on the old rule.
- **Corrections to D-058:**
  - **D-019** listed the CAT fee ($0.000003 a share, "under $0.001 for this bot's sizes") and ignored it as negligible. Rounded up per day, it costs a cent every trading day. D-058 should have named D-019 among the statements it updates.
  - **D-058's "holds to within a cent a day, and only when a booking fails"** (of D-055's "settled cash stays conservative") was too narrow.
    - An estimate also stands in, with no alert, whenever a booking runs before that day's charges post: at EOD by design, and at any boot that comes before them.
    - The cent bound holds in every case. The SEC fee and TAF, rounded up per fill, never come to less than the REG charge, which Alpaca rounds per order or per day. Only CAT is missing.
- **Docs:**
  - S§3.6's fee row now says "per sell fill" and names the daily CAT fee.
  - PLAN's P4.0a note says when a day's charges post: that evening, after EOD.
  - Q5 says what D-058 found about CAT.
- **Tests:** 1 new (fees).

## D-061 — P4.1: Claude's output models and schemas: strict, echoing the `pack_id`, in `full` and `wire_safe` modes; filter, clamp, then validate
- **Date:** 2026-10-15 · **Status:** Accepted (the Phase 4 plan, decision 7).
- **The models** (`llm/schemas.py`, A§5):
  - **Enums:** S§5.5's catalyst types (18) and risk flags (13), S§6.2's DSL fields (17) and ops (8), and S§6.5's outcome factors (19). A test parses STRATEGY.md and requires the same lists in the same order; a value added there in any spelling fails it.
  - **`pack_id`:** `PremarketBrief`, `VetBatch` and `DailyReview` start with `pack_id` (`^[A-Za-z0-9-]{1,64}$`), which Claude copies from the pack.
    - Pydantic always checks the format. Validated with the pack's id in the context (`context={"pack_id": ...}`), it checks the value too, so an answer about any other pack fails and gets the one repair.
    - The error doesn't name the expected id: a repair has to read it from the pack.
    - **For P4.3:** a pack id must match the pattern and carry a part that can't be guessed (a prefix of the pack's SHA-256, say), or the echo proves nothing. A§8's example gains `pack_id` then.
  - **Closed and strict.** The models refuse extra fields: a fixture's assessment that sets a `size_multiplier` is rejected. They are strict, so pydantic is never looser than the schema the call sent.
    - In its default lax mode, pydantic took "yes" and 1 for `true`, "0.7" for a number and "3" for an issue id, all of which the schema refuses.
    - Strict, it also refuses a few things a Python Draft-7 validator lets through: a trailing newline under `$`, NaN against a range, 3.0 for an integer and a lone surrogate. Of these, only the surrogate can come through the CLI, at the cost of one repair.
  - **Unbounded, as in A§5:** `signal_id` and a filter's `value`. A review of anything but a target is dropped (A§7.3), and S§6.2's evaluator checks values.
- **The wire schemas** (`wire_schema(model, mode)`):
  - **Shape:** every `$ref` inlined, titles and defaults dropped, every object closed (`additionalProperties: false`, unless a dict field gives its value schema there).
    - The transforms recurse into schemas, never into the `properties` mapping, so a field named like a keyword survives.
    - They name no `$schema`: nothing in them differs between draft-07 and 2020-12. The tests check them with a Draft-7 validator, and the review compiled all six with ajv (draft-07).
  - **`full`** keeps the lengths, counts, ranges and patterns.
  - **`wire_safe`** states them in each property's description (e.g. "at most 240 characters") and makes every property required. The optional ones stay satisfiable by being nullable (`existing_issue_id`) or constant (`compare_to`).
    - It is the subset strict structured outputs accept, held for G4a: if the CLI refuses `full`, `llm.schema_mode` (P4.4) switches.
    - It may be more cautious than needed, since it also drops `pattern` and `minItems`. G4a will show.
  - **Size, compacted:** vet 1.4 KB, brief 1.6 KB, review 3.2 KB in `full`; at most 3.6 KB in `wire_safe`, and 4.2 KB once Windows quotes it. The limit is 8 KB.
  - **Key order:** the models' field order, not sorted like the repo's other JSON.
    - A schema's order is roughly the order Claude writes in. Sorted, an assessment would give its reason before its symbol, and a review its notes before its signal id.
    - So `--check` compares exact text, key order included. A checkout's CRLF line ends don't count.
- **Filter, clamp, then validate** (amends A§7.3; the runner is P4.4's):
  - **First, drop what doesn't belong:** assessments of symbols that aren't candidates, and reviews of signals that aren't targets.
  - **Then clamp** (`clamp_to_schema`, with the `full` schema): over-long free text, and the tails of the free lists (risk flags, factors, process issues, notes, lesson proposals). Each cut is counted (`llm_calls.clamped`).
  - **Never clamped, so they go to validation and the one repair.** Plan decision 7 says "lengths and counts are clamped; enums never are"; the review showed where cutting a count changes the answer.
    - **Numbers:** a confidence read on a 0–100 scale would have become 1.0, the highest.
    - **A string with a pattern:** cutting an identifier can't make it right.
    - **The lists whose items count as a set:**
      - a lesson's filters are ANDed, so dropping one tests a broader subset than the proposal states;
      - assessments (one per candidate) and reviews (one per target): a stray entry ahead of a real one would push it out.
  - The clamp walks the schema, never the data beyond it, and builds new containers. So a deeply nested value reaches validation instead of raising `RecursionError`.
- **Files:**
  - `schemas/{premarket_brief,vet_batch,daily_review}.json` (`full`) and their `.wire.json` twins (`wire_safe`).
    - `python -m trader.llm.schemas --write` writes them.
    - `--check` exits 1 on a stale or missing file, or a JSON file there the code doesn't make.
  - `tests/fixtures/llm/` holds one valid output per role and seven invalid ones: an enum, a symbol, a type, a `pack_id`, an extra field, a missing field and a DSL field that isn't whitelisted. The envelopes come with P4.4's fake CLI.
- **Review.** Two reviewers (the semantics; the spec and tests) found no blocker. Every finding is fixed, and each fix's test fails without it (nine mutations, all caught). The findings:
  - The clamp clamped numbers, cut ANDed filters, and let a stray assessment or review push a real one out. A§7.3 now filters by membership first, and the clamp leaves those alone.
  - A well-formed but wrong `pack_id` passed; now the context checks it.
  - `deepcopy` raised `RecursionError` on deeply nested junk.
  - The transforms treated `properties` as a schema. It was latent, for fields named like keywords.
  - A patterned string could be cut.
  - The STRATEGY parsers missed names with digits, or without backticks.
  - No test reached the clamp's `anyOf` path, the 8 KB limit or a stray file.
  - The wording on agreement, file names and key order was off.
- **`jsonschema` 4.26 is a new dev dependency,** for tests only (Draft-7). It touches neither trading, risk nor credentials. The lock gained it and four of its dependencies, and changed nothing else.
- **Tests:** 53 in `test_llm_schemas.py`. 853 tests pass.

## D-062 — P4.2: the sanitizer: text cleaned until it settles (or removed), and injection heuristics read through folds and several views
- **Date:** 2026-10-16 · **Status:** Accepted (the Phase 4 plan, P4.2).
- **Cleaning** (`llm/sanitize.py`: `normalize`, `clean_text`, `cut`):
  - **What it removes:**
    - HTML is reduced to its text. Script, style, noscript, template, iframe, object, svg and the raw-text elements (xmp, plaintext, noembed, noframes) are dropped, and block tags read as spaces.
    - Entities are decoded, then the text goes through NFKC.
    - Invisible characters are removed: categories Cc, Cf, Cs, Co and Cn, variation selectors, the combining grapheme joiner, the two Khmer inherent vowels, and two other blank marks.
    - Characters that render as a blank (the Hangul fillers and the braille blank) become spaces, so the pack shows what a reader sees. Whitespace is collapsed.
  - **It repeats until nothing changes,** so double-escaped markup comes out as text.
    - **Markup that hasn't settled after 8 passes is replaced by a placeholder, and the item is flagged.** No real news needs more than 3 passes.
    - The review showed that markup escaped as deep as the limit came out as live tags, unflagged.
  - **Fidelity:**
    - A `<` that never closes, or a `<US$1` price, stays as text. It used to swallow the rest of the field.
    - Superscript digits, fractions and the trademark sign no longer merge into what precedes them: "$1.25^1", "4 1/2%" and "ZEPBOUND" now come out where NFKC made "$1.251", "41/2%" and "ZEPBOUNDTM".
    - Digits inside `<sup>` get a "^".
  - **The cut** keeps at most `max_chars` characters and ends in "...".
    - Not "…": NFKC turns "…" into "...", so a second pass would cut again.
    - It ends at a space within 15 characters.
    - It never splits a number from its unit ("$1.25 billion"), a letter from its marks, an Indic conjunct, or a flag's two letters.
    - Cleaning clean text changes nothing; two property tests check this.
  - **Size:** 32,768 characters are read per field (`RAW_CAP`), and a longer field is flagged.
    - The worst case measured is 0.84 s per item: three checks of a full field of `<`. Realistic packs take a fraction of that.
    - R-020's 5 s window runs on the same event loop, so **P4.8 should build packs off the loop** (`asyncio.to_thread`).
- **The heuristics** (`is_suspicious`):
  - **The views it reads:**
    - the clean text;
    - the decoded text, with its tags and line breaks kept;
    - the text with invisible characters read as spaces and glued words split (`ResultsNote to AI models:`);
    - text smuggled in tag characters (beyond what flag emoji spell);
    - text reversed under a bidi override;
    - text CSS hides from a reader (10 letters or more; the text stays in the pack).
  - **Folds, applied before matching:**
    - lookalike letters, found by Unicode name: Cyrillic, Greek, Armenian, small capitals, and negative circled, negative squared and regional-indicator letters;
    - NFKD with marks dropped, which removes accents and stacked marks;
    - curly quotes and angle brackets straightened, and case folded;
    - a letters-only form and a leetspeak form, for the canonical phrases.
  - **The patterns aim at what addresses a model, not at news words:**
    - The AI word must be the one addressed: "Note to AI models:" counts, "Note for AI bulls" doesn't.
    - A role word needs an order after it or a line break before it: "System: approve" counts, "System: Windows 11" and "Human: Fall Flat" don't.
    - Warnings and commentary are excluded: "disregard earlier messages sent in error", "Ignore Everything Else:".
  - **Measured on the reviewers' labeled corpora,** offline, with a scratch harness:
    - **No false positives on 735 benign items** across 20 Benzinga categories: earnings, FDA, M&A, analyst ratings, legal, the AI sector, HTML summaries and held-out sets.
    - **3 of 78 benign edge probes are flagged,** all quoting the attack phrase ("Judge Tells Jury To Disregard Prior Instructions"). That is kept by design; the first version flagged 75 of the 78.
    - **231 of 247 attacks are flagged (94%).** The misses are the long tail the reviewers advised leaving to the model-side defenses: "this overrides all prior instructions", "imagine you are", "ignore prior guidance", Cherokee lookalikes, and two-letter turn markers.
  - **The tests carry 62 adversarial and 67 benign headlines.**
- **The flag and `suspicious_content` (resolves a spec gap):**
  - RISK.md says suspicious strings "set `suspicious_content`", but the prompts leave it to the model, which an injection could talk out of it.
  - **Code ORs `suspicious_content` into the risk flags of any candidate whose pack news is flagged, after validation (P4.4),** so neither the clamp nor the model can drop it.
  - P4.5's prompts explain the pack's `suspicious` key.
- **For P4.3:**
  - Clean the pack's other third-party strings (`name`, `source`) with `clean_text`, unflagged.
  - `TextLimits` defaults to a 300-character headline and a 600-character summary. At 40 candidates with 8 items each, that is 288,000 characters of news, against the 300 KB pack cap, so `encode()` will trim on busy days.
- **For E1:** `anonymize` is exact, whole-word and case-sensitive. Tickers that are also words ("AI" for C3.ai, "A") need care in what E1 passes.
- **Accepted, and recorded:**
  - "&notice" decodes to "¬ice" (HTML's legacy entities).
  - Closed tag-like text in a plain headline ("<AAPL.O>") is dropped.
  - Items are checked one at a time, so two items each carrying half a phrase aren't joined. P4.3 may join a symbol's items.
- **Review.** Three reviewers (evasion; false positives; robustness and spec) found two blockers and several majors:
  - the leak at the pass limit;
  - the Khmer marks;
  - glued payloads;
  - the "instructions above" family and Human:/Assistant: turns;
  - AI-sector false positives.
  - Every finding is fixed or recorded above. Each fix's test fails without it: 15 mutations, all caught.
- **Environment:** the editor tool decodes 4-digit unicode escapes into literal characters. The source uses named escapes (`\N{...}`), and a repo-wide scan found no invisible characters.
- **Tests:** 155 in `test_sanitize.py`, including two property tests and a sweep of every default-ignorable code point. 1,008 tests pass.

## D-063 — P4.3: the pre-market candidates and the context packs; fixes to P4.2's sanitizer
- **Date:** 2026-10-17 · **Status:** Accepted (the Phase 4 plan, P4.3 and decision 12).
- **Pre-market candidates** (`llm/candidates.py: select_premarket`):
  - **Who qualifies:** an eligible symbol (S§3.1) whose last print (the snapshot's latest trade, live feed) came today at or after 04:00 ET and may set a price (D-043's conditions), above S§3.2's $5 floor, with a gap that isn't at a split ratio (`orb_sip.suspect_gap`).
    - **The floor and the split check go beyond decision 12's text.** The 09:35 scan drops both, so such a symbol can't become a signal, and an unlisted reverse split would otherwise top the gainers.
    - **The price-setting check is defense in depth.** The IEX snapshots recorded on 10-13 and 10-14 never had an odd lot as the latest trade (0 of 19), while about 60% of those days' stream trades were odd lots. IEX's pre-market opens at 08:00, so the 04:00 bound binds only on SIP.
  - **The gap** is that print against `universe_daily.prev_close` (SIP). This supersedes D-024's snapshot daily bar as the source.
  - **The picks:** the top 30 gainers (gap above 0), then up to 10 more symbols with news in the 18 h window, by the size of their gap. At most `max_candidates` (40) in all, gainers first; ties go to the symbol.
- **The packs** (`llm/context.py`): three pure builders and `encode()`.
  - **The frame.** Every pack opens with:
    - `pack_version`, `pack_id`, `role`, `as_of`;
    - `session`, with the last entry and the flatten;
    - `experiment` and `limits`;
    - `events_today`, `market` (index gaps and the previous session's regime tags) and `book_b` (equity, recent R, and the day mode, except in the brief);
    - `playbook`, empty until P5.3.

    The role's own sections follow, then the ranked list, and `instructions_note` comes last, after all the untrusted text, as A§8 had it.
  - **`experiment` and `limits` carry every config number the prompt drafts hard-code,** so P4.5 can point the prompts there: the opening range's minutes, the candidate caps, the news windows, the day modes' multipliers, the lessons' gate. `limits` also states EDGAR's window and forms, so an empty `filings` list reads as "none in 5 days".
  - **The candidates:**
    - **Brief:** name, exchange, how it was picked, the previous close, the pre-market price and its time, the gap, ATR, ATR%, ADV, filings and news.
    - **Vet:** A§8's fields plus the trigger and the stop. A long already triggered at arming waits for its verdict (S§3.3, S§5.4), so it is vetted too.
    - **Each news item:** its age, its source, the headline and summary under `untrusted`, and `suspicious`.
  - **Filings:**
    - The newest 10, never one accepted after `as_of`. An 8-K's items are kept only if they read as item numbers.
    - **`Evidence.filings` None means unknown** (EDGAR failed, or wasn't asked), and shows as `filings_unavailable: true`. The review found the first default said "none" instead: the vet would have read "no 424B5" for a symbol nobody looked up.
  - **The review pack.** The input types are for P4.10 to fill:
    - the day's signals, with each book's decision and trade, Book B's verdict and the shadow path;
    - the review targets, with the path, SPY's move and news;
    - incidents, the most severe first;
    - statistics (plain numbers);
    - lessons;
    - the DSL whitelist;
    - `history`.
  - **News stamps (review):**
    - An item is `before_decision` when the engine could read it by the decision: the later of its creation and its receipt is at or before the decision time.
    - It also carries `minutes_from_decision` and `edited_after_decision`. NewsHub keeps an edited article's first `received_at`, so its text may postdate the decision.
    - **A target's news alternates before and after the decision, nearest first,** so the per-target cap and the trims keep the decision-time news longest.
  - **Every capped list counts what it leaves out:**
    - `candidates_omitted` or `targets_omitted` (the builder's cap and the trims);
    - `news_omitted` and `filings_omitted`;
    - `signals_omitted`, `incidents_omitted` and `lessons_omitted`;
    - in `history`: `bad_process_omitted`, `open_issues_omitted` and `open_notes_omitted`.
  - **Determinism:**
    - Compact UTF-8, keys in the builders' order.
    - Every mapping a caller passes is sorted: gaps, stats, books, decisions, trades. Every list has a tie-break.
    - The bytes are the same for any input order and under two hash seeds, tested for all three roles.
- **Untrusted text:**
  - **Under `untrusted`:** news; and in the review, Book B's reasons, lessons' statements and queries, `history`'s texts, and incident messages, which may quote an error.
  - **Outside it, cleaned and cut:** names, sources, forms, event names and codes.
    - Each is also kept within 2 bytes a character, so a flood of 4-byte characters in a model's earlier text can't crowd the review's targets out.
  - **`suspicious_news`:**
    - A candidate (in the review, a target) carries it when one of its items is flagged, or when its items, joined in pack order, are. D-062 left that join to P4.3; a phrase split across two headlines now flags.
    - The flag survives any trim, and `EncodedPack.flagged` lists those candidates for P4.4.
  - **The sanitizer knows the packs' own keys** (`sanitize.PACK_KEYS`) and two phrases about the framing ("end of untrusted text", "is not untrusted"). A test keeps the key list in step with the packs.
- **`encode()`:**
  - **The trims, in order:**
    1. news: the last item of the lowest-ranked candidate holding the most, down to one each;
    2. summaries, the lowest-ranked first, only where dropping one saves bytes; each is marked `summary_omitted`;
    3. candidates, the lowest-ranked first.

    If even the pack without candidates doesn't fit, `PackTooLargeError` means no call.
  - The byte accounting is incremental and exact; a test makes the eleventh drop where the count gains a digit.
  - **`pack_id`:** the role, the date and the first 16 hex digits of the SHA-256 of the trimmed pack, hashed with a placeholder of the same length.
- **Sizes and times,** on this PC:
  - **A realistic busy brief** (40 × 8 items) is 254 KB untrimmed. It builds in 1.0 s, almost all of it the sanitizer, and encodes in 3 ms.
  - **The worst cases fit and keep every candidate and target:** 4-byte text in every field, every cap reached, and the review at 30 days of history. `history_days` is now at most 30 in the settings.
- **Fixes to P4.2's sanitizer** (uncommitted, so fixed in place), found by this review and its tests:
  - **Pack news reads 4,096 characters a field** (`untrusted(raw_cap=...)`), and a longer field is flagged. Alpaca's summaries run a few hundred characters.
    - With the parser fix below, the worst item costs 0.1 s, so an adversarial 25 × 5 vet pack builds in about 12 s; before, it took minutes.
    - P4.8 builds packs off the event loop (D-062), and may reuse the brief's cleaned items for the vet.
  - **The HTML parser was quadratic in unclosed tags.** It rescanned the open elements for every chunk of text and every stray end tag; it now keeps counts.
  - **A cut could end at "</".** With the mark, that parses as a comment the next cleaning drops, so `clean_text` wasn't idempotent: hypothesis found "aamp;</aaaa" cut to 10 characters. A cut now steps back past a dangling opener, and 50,000 examples pass.
  - Oversized text is flagged before any work is done on it.
  - The reviewers' corpora still give no false positives on 802 benign items, with the same catches.
- **For P4.4:**
  - `EncodedPack.kept` is the filter's membership set: symbols, or signal ids for the review.
  - `llm_calls.pack_sha256` and `pack_bytes` come from `sha256` and `len(data)`.
  - **Adding `suspicious_content`:** de-duplicate the flags and, at 8, replace the model's last one. `risk_flags` holds at most 8, and a stored verdict with 9 would fail the next validation: P4.7's rebuild, or P4.10's `ReviewSignal`.
  - Log failed attempts by code: pydantic's `ValidationError` quotes the model's output, so use `errors(include_input=False)`.
- **For P4.5:**
  - The prompts read `limits`, `experiment`, `suspicious_news`, the `..._omitted` fields, `summary_omitted` and `edited_after_decision`.
  - The review prompt says model-written text under `untrusted` is data.
  - A lesson is weighed by its numbers; its statement only describes it.
- **For P4.8:**
  - **The inputs:**
    - the universe's listings and previous closes;
    - the snapshots' latest trades;
    - news from the last 18 h and 24 h;
    - EDGAR per candidate. The 09:00 prefetch doesn't cover vet-only symbols, and the `filings` table can't tell "none" from "never fetched", so track which symbols EDGAR answered;
    - Book B's E and recent R;
    - the previous session's regime;
    - today's events.
  - **Replay serves the recorded pack bytes for every role, the review included,** and validates the recorded answer against the recorded `pack_id`. The replay's seeded journal holds no news or history, so a rebuilt review pack would carry another id. This amends decision 10 when P4.8 lands.
- **For P4.10:**
  - Pass the targets ranked, each with at most 3 items from before the decision and 2 from after.
  - Pass lessons promoted first, then the newest proposals, and history's lists newest first (issues and notes by when last seen or raised).
  - The open issues and notes a review may link are in `pack.body["history"]`.
- **Spec updates:** A§2, A§3's 09:00 row, A§4.7, A§8 (the example, now with `pack_id`, and the encoding), A§10's EDGAR row, S§5.2's candidates, RISK.md's prompt-injection row.
- **Review.** Four reviewers (spec and integration, the encoding, the injection surface, the tests), each checked by an adversarial verifier, made 37 findings:
  - 2 major, of which 1 was confirmed: the filings default;
  - no blocker;
  - 1 refuted.

  Every finding is fixed or recorded above. Each fix's test fails without it: 52 mutations, all caught.
- **Tests:** 44 in `test_llm_context.py`, 10 in `test_llm_candidates.py`, and 3 more in `test_sanitize.py`. 1,065 tests pass.
- **Sources:** IEX's sessions, the pre-market 08:00–09:30 ET: https://www.sec.gov/files/rules/sro/iex/2026/34-105959.pdf

---

## D-064 — P4.4: the runner, its transports and the budget; the CLI's argv and how a call is stopped
- **Date:** 2026-10-18 · **Status:** Accepted (the Phase 4 plan, P4.4 and decisions 1, 2, 4, 5 and 6).
- **The runner** (`llm/client.py: LlmCalls`) is the one way trading code calls the model (invariant 5).
  - `run(role, pack, *, deadline, purpose, persist)` makes one logical call: an attempt and at most one repair. It never raises for a failure: whatever goes wrong comes back as `ok=False` with a `failure` code. Only a cancellation goes on.
  - **One `llm_calls` row per attempt** (migration 0007). The call's id is `YYYYMMDD-role-NN` (`YYYYMMDD-smoke-role-NN` for smoke calls), its attempts' `-1` and `-2`. They are numbered from the journal inside the insert's transaction, so they repeat on a replay and continue after a restart.
  - **`LLMResult.call_id` is the attempt whose result stands.** Every `llm_call_id`, `last_llm_call_id` and `source_call_id` column holds such an id; `verdicts`, `daily_reviews` and `signal_reviews` reference `llm_calls(id)`. `logical_id` is the call's own id. The first build returned the logical id, which those foreign keys reject.
  - **`final`:** the attempt whose result stands has `final = 1`. A call whose last attempt still has `final = 0` never produced a result: a repairable first try, then a cancellation or a crash before its repair. P4.7 treats it as abandoned and re-vets; the first try's failure still counts.
  - **`persist(conn, output, attempt_id)`** runs in the transaction that completes a good answer. What the caller stores from the answer commits with the attempt's row, or neither does; if it raises, the attempt fails as `internal_error`. P4.7's verdicts and P4.10's review use it. Without it, an `ok` final row doesn't mean the caller stored anything.
- **Refusals,** before an attempt:
  - `no_time` (less than the role's `min_attempt_s` left: 60, 15 and 120 s) and `budget_exhausted` (checked in the attempt's own insert) leave no row.
  - **`sandbox_dirty`, `prompt_missing` and `pack_too_large` come back on every call until a human fixes their cause.** Each is journaled as an attempt that failed without a CLI run: `failures()` counts it, the cap doesn't. This is decision 4: "every failed attempt counts, except one refused for budget or time". The first build left them without a row, so a stray file in the sandbox would have cost Book B every call all day with no degradation, no alert and no trace in the journal. P4.8 raises a P2 for them (`PERSISTENT_REFUSALS`).
- **The budget** (`llm/budget.py`):
  - `attempts(day)` counts trading CLI runs against `max_calls_per_day`.
  - `failures(day)` counts failed trading attempts toward `llm_failures_degrade_after` (RISK.md §2), but not abandoned ones. A call and its failed repair are two.
  - Smoke calls count toward neither.
  - `slot` holds `max_concurrent` attempts at a time. It is taken per attempt, so a repair can queue behind another call's first try; the planned schedule never runs two calls together.
- **Each attempt:**
  1. inserts its row;
  2. writes `<attempt id>.input.json`, the exact stdin;
  3. runs the transport, its timeout min(the role's `timeout_s`, the time left to the deadline);
  4. is classified;
  5. writes `<attempt id>.output.json`;
  6. completes its row.
- **When something goes wrong:**
  - **A cancellation** closes the row as `abandoned`. The insert is shielded: a cancelled caller's insert still commits, as every journal write does, and the row it made is then closed too.
  - The cleanup waits 2 s at most. At a loop's teardown the journal's writer may already be gone, and boot's `close_abandoned()` then closes the row.
  - **Any other error inside the runner** closes it as `internal_error`, a failed attempt: a transport error that isn't an `OSError`, a journal error, `persist` raising. A bug in the classifier does too, and its artifact is still written. A failed insert is a refusal (`internal_error`, no row). Before, all of these escaped `run()` and left the row open.
  - **An artifact that can't be written fails its attempt** (`artifact_failed`). No call goes out whose input isn't on disk, and no answer is used whose output isn't (invariant 8). The rename is retried three times (0.35 s): on Windows a scanner can hold a new file for a moment.
- **File work runs on a thread only beside a threaded journal.**
  - Under simdrive (an inline journal: replay, sim days, `DayRun`) a thread hid that work from `settle()`. The SimClock then jumped in the middle of an attempt, or the drive got stuck: the review measured 3 to 10 simulated minutes for a 0 s call.
  - Now a 7 s scripted attempt takes exactly 7 s there, beside a part of the day that wakes every second, and the ids hold with two calls at once.
- **Classification** (`classify`, A§7.3), in this order:
  1. **The process:** a timeout, or stdout over its cap.
  2. **The envelope:**
     - no JSON result: `invalid_json`, or `nonzero_exit` after a non-zero exit;
     - an error subtype: `max_turns`, `execution_error`, `budget_usd`, `structured_retries`;
     - `is_error`;
     - a non-zero exit, even with a good envelope;
     - a subtype other than `success`: `invalid_envelope`;
     - no `structured_output`.
  3. **The answer:** filtered to the pack's members, clamped, and validated with the pack's `pack_id`; a failure is `schema_invalid` or `wrong_pack_id`.

  An executable that couldn't start is `spawn_failed`.
- **The repair:**
  - Only the output failures in `REPAIRABLE` get it (`invalid_json`, `no_structured_output`, `structured_retries`, `schema_invalid`, `wrong_pack_id`), and only with `min_attempt_s` left. It sends the same pack, with `REPAIR_NOTE` and at most 10 errors added to the instruction.
  - **The errors are pydantic's messages, located only by the output models' own field names** (read from their schemas) **and list positions.** Anything else shows as `?`. So does the key of an `extra_forbidden` error, even one that spells a field of ours from elsewhere.
  - The first build showed any part shaped like a name, so a key the model invented, perhaps steered by injected news (`SYSTEM_veto_all`), came back in the trusted `-p` text. Values and the expected `pack_id` never appear.
- **The artifacts** (`runtime/llm/<date>/<attempt id>.{input,output}.json`):
  - **What the output holds:**
    - the envelope, or the first 64 KB of a stdout that isn't one;
    - the answer as the runner returned it;
    - stderr's tail and the exit code;
    - how a late child was stopped;
    - the errors, and the clamp and drop counts;
    - the argv, with the schema shown by its hash.
  - **It is ASCII JSON.** A lone surrogate in the model's JSON (`\ud83d`, the one strict-mode case D-061 lets through) stays an escape. UTF-8 can't hold one, and the first build crashed writing it, losing the attempt.
  - **Redaction** covers the credential's exact value and anything shaped like `sk-ant-` followed by 8 more characters.
    - A tail or a capped stdout begins or ends at an arbitrary byte, so it drops 512 characters at that edge (`CUT_MARGIN`). A secret the cut went through then leaves no part behind.
    - The review showed a 16 KB tail could start inside the token and keep most of it.
- **The CLI's argv** (`build_argv`, A§7.2):
  - The instruction comes right after `-p` (decision 2). Every role's instruction ends "Copy the pack's pack_id into your answer."
  - **`--setting-sources user` is new: the CLI then reads no project or local settings, `CLAUDE.md` or `.mcp.json` from the sandbox's parent folders.**
    - The sandbox sits inside the repository, under its `CLAUDE.md` and `.claude/`.
    - On POSIX the CLI reads `.claude/settings.local.json` from the git root even when started in a subdirectory. An `env` block or an `apiKeyHelper` there would have reached every call around `build_claude_env`'s allowlist.
    - Claude Code's docs give the flag for exactly this: "Pass `--setting-sources user` ... so Claude Code reads neither the project's settings files nor its `.mcp.json`" (permissions), and it turns off "project configuration such as `.claude/settings.json`, `CLAUDE.md`, and `.mcp.json`" (debug-your-config).
    - It also keeps `safe_mode: false` from reopening the `CLAUDE.md` gap.
  - **`--safe-mode`** (`llm.safe_mode`, on by default) turns off skills, plugins, hooks, MCP servers and auto memory. Its docs say authentication and permissions work normally, and they don't mention settings files: hence `--setting-sources` as well.
  - **What still loads:** the bot's own `CLAUDE_CONFIG_DIR` (`runtime/claude-config`) and managed policy.
  - **Never `--bare`:** bare mode can't use the OAuth token.
  - **G4a's fallbacks:** `tools_flag: deny_all`, `safe_mode: false`, `schema_mode: wire_safe`.
- **The environment** (`build_claude_env`):
  - the allowlist;
  - `CLAUDE_CONFIG_DIR`;
  - `DISABLE_AUTOUPDATER=1`, new: the CLI must not update itself between calls. Doctor warns about a CLI newer than `max_tested_cli_version`.
  - exactly one credential.
- **The executable** (`resolve_claude`):
  - **On Windows only a native `.exe` will do.**
    - A `.cmd`, `.bat` or `.ps1` shim runs through cmd.exe, which parses the inline schema's quotes again, and an interrupt stops at the shim.
    - A `.js` or extensionless file passes `shutil.which` (PATHEXT) but can't be started.
  - Doctor and `from_config` both call it, so the two checks can't drift. `from_config` raises `LlmUnavailableError` for it, as for a missing credential.
- **The transport** (`ClaudeCliTransport`, decision 6):
  - The child starts in its own process group.
  - **Whether the child is still running comes from its return code, polled every 50 ms.**
    - In Python 3.12, `Process.wait()` resolves only once the pipes have closed too, and a loop's teardown can cancel the task waiting on it.
    - The first build read that cancelled wait as an exit. When `asyncio.run` tore down with a call in flight (a second Ctrl+C), the CLI was left running; the review reproduced it 3 times in 3.
    - A descendant holding the pipes also turned a finished answer into a timeout, about 21 s late.
  - Once the child is gone, its pipes get 1 s to reach their end.
  - **Stopping a late child:**
    - **Windows:** CTRL_BREAK to its group, `kill_grace_s` (5 s), then System32's `taskkill /T /F` by its full path (a bare name is looked for in the interpreter's folder and the working directory first), then `TerminateProcess`. Without a console (a scheduled run) CTRL_BREAK can't be sent, and the kill comes at once.
    - **POSIX:** SIGINT, the grace, SIGTERM, the grace, then SIGKILL to the group.
    - `timed_out` only when the child was still running at the deadline.
  - **A cancelled call kills its child before the cancellation goes on:** however often it is cancelled (a second cancel used to skip the stop), and when the loop is torn down.
  - **Output caps:** stdout is read to 4 MB; past that the child is killed (`overflow`) and the rest drained. Stderr keeps its last 16 KB, marked `stderr_cut` once bytes are dropped.
  - **Residual:** there is no Job Object yet, so a descendant that outlives the CLI, or a hard kill of the trader, can leave a process running. G4a's `--kill-test` records whether the real CLI leaves descendants.
- **Configuration.** Each new setting has a default, so the frozen G3 config still validates: `llm.schema_mode`, `tools_flag`, `safe_mode`, `kill_grace_s`, `max_pack_bytes`, `max_tested_cli_version`, and each role's `min_attempt_s`.
  - **Config refuses** a `min_attempt_s` over the role's `timeout_s`, and `allow_web: true`. E5's web tools aren't built, so the brief would run without them under a mislabeled experiment.
  - **`LlmCalls.encode(pack)`** trims to `llm.max_pack_bytes`, the cap `run()` refuses past, so the two can't disagree.
  - **`from_config` takes `command`, `env`, `sandbox_dir` and `artifacts_dir`.** A replay or a `--sim` day passes an env with no credential and its own folders. Otherwise it would write over the live day's artifacts, since ids repeat.
- **Testing without the CLI** (A§7.4):
  - **`scripts/fake_claude.py`** checks the call:
    - the instruction after `-p`, every flag and its value, `--setting-sources user`, `--safe-mode`, no `--bare`, a schema under 8 KB;
    - the config dir, the autoupdater off, exactly one credential, no other `ANTHROPIC_*` or `CLAUDE_*` variable, and no `NODE_OPTIONS`;
    - an empty working directory.

    Then it answers one of 16 scenarios. The slow ones set their interrupt handler before the slow import, so an early CTRL_BREAK can't end the process the default way.
  - **`llm/fake.py`:** `ScriptedTransport`, whose delays pass on the injected clock, plus `answer_for` and `envelope`.
    - A `Seen` never shows its env in a repr: under `--sim` it would hold a credential.
    - P4.8 builds the `--sim` env without one.
  - **The test guard now wraps `subprocess.Popen`.**
    - Every path that starts a process is covered: the transport, doctor's probes, a `cmd /c claude` wrapper.
    - It refuses any argv part that names `claude`, however spelled; Windows also runs `claude.exe.` and `claude `.
    - It raises a `BaseException`, which no `except Exception` in the runner can turn into an ordinary failure.
- **For P4.5:** the fingerprint's inputs are public: `role_specs`, `wire_schema` and `schema_bytes`, `REPAIR_NOTE`, `build_argv`. Hash the prompt's content, not its absolute path, which differs between the live root and a replay's.
- **For P4.6:**
  - **The isolation canary:**
    - **The in-sandbox half** (a `CLAUDE.md`, a `.mcp.json` and a hook in the working directory) can't go through `LlmCalls`, which refuses a dirty sandbox. Build it from `build_argv`, `build_claude_env` and `ClaudeCliTransport` against a canary folder.
    - **The parent half** runs through `LlmCalls`, with `sandbox_dir` an empty child of the canary folder. Add a parent `settings.local.json` with an `env` canary; it only means something on POSIX.
  - Check the real sandbox is still empty after each call.
  - `--kill-test` checks the stop path and descendants.
  - Smoke calls use `purpose="smoke"`, and a `min_attempt_s` below their short timeouts.
  - **At G4a,** confirm that OAuth and `structured_output` work with `--setting-sources user` and `--safe-mode`.
- **For P4.7:**
  - Read `final`.
  - Store verdicts with `persist`; a final `ok` row alone doesn't mean they were stored.
  - Re-vet an abandoned call: one with an `abandoned` final row, or whose last attempt isn't final.
- **For P4.8:**
  - Type `DayDeps.llm` as `LlmCalls`: it needs `budget.failures()` and `close_abandoned()`. Run `close_abandoned()` at boot, before any call.
  - Raise a P3 for each failed attempt, from the rows of `result.attempts`, and a P2 for `PERSISTENT_REFUSALS`.
  - Put `LLMResult.failure` into `decisions.detail`.
  - Build the transport with `kill_grace_s`.
  - Give replay and `--sim` their own env (no credential) and folders.
  - Encode packs with `calls.encode`.
  - A `RecordingTransport` can key its records by order and `pack_id`, since ids repeat on a replay.
- **Spec updates:**
  - ARCHITECTURE: §1, §2, §4.7, §6 (which id each `llm_call_id` column holds), §7.1–7.4, §12;
  - RISK.md: what `llm_failures_degrade_after` counts, and the silent-billing row (`llm.provider`, not `llm.auth`);
  - STRATEGY.md §5.4;
  - CLAUDE.md invariant 5 (approved with the Phase 4 plan);
  - the settings example's comments.
- **Review.** Four reviewers (spec and integration, the process and its stop path, security, logic and tests), each checked by an adversarial verifier, made 49 findings, 33 distinct issues:
  - **7 confirmed major:**
    - the threads under simdrive (two reviewers);
    - `call_id` against the foreign keys;
    - errors after the insert leaving rows open (two);
    - the lone surrogate;
    - the CLI left running at a loop's teardown.

    4 more were rated major and judged minor.
  - No blocker; none refuted.

  Every finding is fixed or recorded above. Each fix's test fails without it: 91 mutations, all caught. Three of them first needed a new test: a dict key the model wrote, a smoke call on a spent day, and a child that overflows and keeps running.
- **Tests:** 119 in `test_llm_runner.py` and 40 in `test_claude_cli.py`, plus doctor, config and environment tests. 1,230 tests pass.
- **Sources:**
  - `--setting-sources`, `--safe-mode`: https://code.claude.com/docs/en/cli-reference
  - `settings.local.json` read from the git root; `--setting-sources user` before `claude -p` in a repository: https://code.claude.com/docs/en/permissions
  - the clean configuration: https://code.claude.com/docs/en/debug-your-config

---

## D-065 — P4.5: prompts v1 hold no numbers; the LLM setup's fingerprint
- **Date:** 2026-10-18 · **Status:** Accepted (the Phase 4 plan, P4.5).
- **Prompts v1** (`prompts/*.md`) stay static: everything dynamic is in the pack.
  - **They hold no numbers.** Every number the drafts stated now comes from the pack:
    - the opening range's length: `experiment.strategy.opening_range_minutes`;
    - the candidate caps and the news and filings windows: `limits.max_candidates`, `limits.news_lookback_hours`, `limits.filings_lookback_days`;
    - the reduced mode's size: `experiment.day_modes`;
    - the lessons' gate: `experiment.lessons` (`min_n_per_side`, `min_effect_r`);
    - how long a triggered candidate waits for the vet: `experiment.book_b.max_wait_s`;
    - the account: `book_b.equity_usd`; the time: `as_of`.

    The answers' length and count limits are left to the schema each call sends (in `wire_safe` mode, in each property's description), and the prompts say "within the schema's limits". Only SEC form names keep digits (424B5, S-3).
  - **Each asks for the `pack_id`** ("copy the pack's `pack_id` exactly"), as each instruction does.
  - **The brief's web-tools sentence is gone:** E5 isn't built, and config refuses `allow_web` (D-064).
  - **They explain what the packs mark** (D-063's list for P4.5):
    - the `..._omitted` counts and `summary_omitted`;
    - `filings_unavailable`: unknown, not "none";
    - `suspicious` and `suspicious_news`;
    - in the review: `published`, `minutes_from_decision`, `edited_after_decision`, `decision_at`, `decision_basis` and `selected_for`.
  - **The review's security note covers what a model wrote earlier:** Book B's reasons, the lessons' statements and queries, `history`, and incident messages, all under `untrusted`.
  - **A lesson is weighed by its numbers** (`n_subset`, `n_complement`, `effect_r`, `ci`); its statement only describes it.
  - **Two corrections to the drafts:**
    - The brief said anything outside the schema's fields "is ignored". The schemas are closed, so an extra key fails validation.
    - The vet's wait runs from arming or the call's start (S§5.4, decision 8), not from the trigger.
- **Tests keep the prompts honest:**
  - no digit outside an SEC form name;
  - every name a prompt quotes is a key, a value or a dotted path of its role's real pack, or a field or enum value of the answers, so a renamed pack key fails. The packs are built with the live `experiment_section`, and trimmed too, so `summary_omitted` appears;
  - each asks for the `pack_id`, and the brief never mentions the web.
- **The fingerprint** (`llm/epoch.py`, S§9):
  - **What it hashes:**
    - per role: the prompt's text, the instruction, the schema sent (`wire_schema(output, schema_mode)`), the model, the effort, and the argv's flags. The argv is built by `build_argv` with placeholders for the instruction, the schema and the prompt's path, so `max_turns`, `tools_flag`, `safe_mode` and every fixed flag count;
    - `REPAIR_NOTE`, `schema_mode` and `PACK_VERSION`.
  - **What it leaves out:** how a call runs (timeouts, `min_attempt_s`, the budget, `kill_grace_s`, `max_pack_bytes`), and the CLI's version, which each row records and doctor checks.
  - **A prompt's hash ignores line endings,** here and in `llm_calls.prompt_sha256` (`roles.prompt_sha256`). This repo has `core.autocrlf`, so a checkout can rewrite the prompts with CRLF, which changes no text the model reads. The prompt's path never counts, so a replay's root hashes the same.
  - **`note_fingerprint()`,** in one transaction:
    - the first fingerprint is stored silently;
    - the same one does nothing;
    - a different one raises a P3 `llm_fingerprint_changed` naming what changed (for example `roles.vet.model` or `roles.premarket.prompt_sha256`), and is stored, so each change alerts once.
  - **Epochs stay the human's call.** A changed setup starts a new epoch unless it is a bug fix (S§9); the P3 asks the question and bumps nothing. Until P7.1 freezes the setup at epoch 1, these P3s are notes.
- **For P4.8:** at boot when Book B runs, never on a Book-A-only day, call `note_fingerprint(journal, epoch_fingerprint(settings, prompts_dir), at=now)`, and put the fingerprint in `boot.json`'s LLM setup.
- **Spec updates:** ARCHITECTURE §2 and §4.7, STRATEGY §9, RISK §8 and §11.
- **Tests:** 25 in `test_llm_prompts.py`; 20 mutations, all caught. 1,255 tests pass.

---

## D-066 — P4.6: `trader smoke llm`: real calls on fictional packs, an isolation canary with its control, and a kill test that watches for leftovers
- **Date:** 2026-10-18 · **Status:** Accepted (the Phase 4 plan, P4.6, and D-064's notes for P4.6). The code only: running it is G4a, the human's.
- **The command:** `trader smoke llm [--role all|none|premarket|vet|review] [--schema-mode full|wire_safe] [--tools-flag empty|deny_all] [--no-canary] [--canary-control] [--kill-test]` (`llm/smoke.py`). Its last line is `SMOKE LLM PASSED` or `SMOKE LLM FAILED`, with exit 1.
  - **Preflight:** the executable (`resolve_claude`), the credential, `claude --version` under the calls' own environment (not a model call), a version no older than `llm.min_cli_version`, and an empty sandbox.
  - **One call per role,** through `LlmCalls` with purpose `smoke`: ids `YYYYMMDD-smoke-role-NN`, never counted toward the cap or Book B's degradation (D-064). They run in the real sandbox, with the settings' models, efforts and timeouts; the deadline leaves two attempts (2 × `timeout_s` + 30 s). Artifacts go to `runtime/llm/smoke/<date>/`.
  - **What it prints,** per attempt, from its row and its redacted artifact:
    - the result and duration, the exit code, the subtype, `is_error`, turns, clamped and dropped counts, the session id, the cost estimate, the resolved model, and the envelope's keys;
    - on a failure, the CLI's own message (`result`, `errors`), the validation errors, how a late child was stopped, and stderr's last lines, with control characters removed;
    - the answer by its counts only, never the model's free text, and what became of the injection headline.

    After each call the sandbox must still be empty: the next trading call would refuse a sandbox that isn't.
  - **It holds `run-day.lock`** (`orchestrator/lock.LOCK_NAME`). A smoke run beside a session would share its sandbox, and a file the CLI left there for a moment would make Book B's calls refuse.
  - **Two additions to the plan's options:**
    - `--tools-flag` tries A§7.2's first fallback, as `--schema-mode` tries the schema's, without editing `settings.yaml`;
    - `--role none` runs only the canary or the kill test, for a rerun that spends nothing on the roles.
- **The packs** (`llm/smoke_packs.py`, from the package data `llm/smoke_data.json`):
  - **Forty fictional issuers** with six-letter symbols, which no US exchange lists; made-up news from five kinds of stories; the live `experiment` and `limits`, so the prompts read what a trading day's packs hold (D-065).
  - **The brief:** 40 candidates (30 gainers, then 10 with news), up to 3 news items each; filings that are a prospectus, an 8-K, unknown (EDGAR failed) or none. About 40 KB.
  - **The vet at full size:** 25 candidates with 5 news items each. About 58 KB.
  - **The review:** 25 signals, 8 of them targets (2 traded, 2 vetoed by Book B, 4 notable), with news either side of each decision, incidents, a lesson and `history`. About 34 KB.
  - **The canary's:** a vet pack of 3 candidates.
  - **One issuer's news carries an injection headline** in every pack. The sanitizer flags it, and the runner adds `suspicious_content` to its assessment.
  - The same bytes every run, so a pack's id repeats across runs.
- **The isolation canary,** under `runtime/llm_canary/<stamp>/`, plants each source of configuration a call could pick up, each with its own way of showing that it loaded:
  - **Instructions:** `CLAUDE.md` and `CLAUDE.local.md` asking for a code word in every free-text field. CLAUDE.md reaches the model as a user message after the system prompt (memory docs), so one that loaded shows in the answer.
  - **Settings:** `.claude/settings.json` and `settings.local.json`, holding:
    - hooks on `SessionStart`, `UserPromptSubmit`, `Stop` and `SessionEnd` that append to a marker file: `echo <event> >> '<path>'`, which Git Bash, sh and PowerShell all run (the CLI's shells for hooks; a test runs it in Git Bash and PowerShell 7);
    - an `env` block pointing `ANTHROPIC_DEFAULT_{OPUS,SONNET,HAIKU}_MODEL` at `canary-model-<place>`. Project and local settings may set these: they aren't on the docs' list of variables ignored there. A leaked block fails the call, or names the model;
    - an `apiKeyHelper` printing `canary-key-<place>`. It outranks the token (D-021), so a leaked one fails the call too;
    - `enableAllProjectMcpServers: true`.
  - **An `.mcp.json`** whose server is our Python writing a marker when started: no shell.
  - **Why each matters, from the docs:**
    - in a `-p` run, a folder's hooks, `env` block and helpers are used, and its `.mcp.json` servers connected without asking ("What runs before you trust a folder"; headless);
    - hooks and `settings.json` load from the working directory's `.claude/` only, and `settings.local.json` from the git root except on Windows;
    - `CLAUDE.md` loads from the working directory and every folder above it.
  - **Two halves,** as D-064 planned:
    - **the parent half** plants them above an empty sandbox and calls through `LlmCalls`;
    - **the in-sandbox half** plants them in the working directory itself and calls `ClaudeCliTransport` with `build_argv`'s argv, since `LlmCalls` refuses a dirty sandbox. Its redacted record is written beside the canary.

    Both roots are made git repositories when git is on PATH, so POSIX's git-root rule is exercised. A half passes when its call succeeds and shows no code word, no marker, no planted model or key, and no new file (git's own excluded).
  - **The control** (`--canary-control`) repeats the in-sandbox half without `--setting-sources user`, `--safe-mode`, `--strict-mcp-config` and `--mcp-config`:
    - with a `CLAUDE_CONFIG_DIR` of its own, so the bot's stays clean;
    - without the `env` block or `apiKeyHelper`, which would fail its call and hide the code word.

    Its three detectors (instructions, hooks, the MCP server) must each fire, or the canary can't vouch for that one. The model and key canaries need no control: they fail any call they reach. The control also loads the `CLAUDE.md` files above `runtime/`, the repository's own, which is harmless.
  - **A clean canary removes its folder,** so no stray `CLAUDE.md` stays under `runtime/`. A canary that found something keeps it for the human.
  - **Two changes to the plan's canary:**
    - It calls the vet, not the brief: the canary needs only a call, and the vet is the cheapest.
    - The control drops `--setting-sources` and the strict MCP flags as well as `--safe-mode`: since D-064 added `--setting-sources user`, dropping `--safe-mode` alone would leave the canary nothing to fire on.
- **The kill test** (`--kill-test`): the vet with a 3 s timeout, through `LlmCalls` and the real transport (`min_attempt_s` 1 for it).
  - **It passes when** the call times out, is stopped (interrupt, terminate or kill), and nothing the CLI started outlives the stop. It answers D-064's residual for the real CLI.
  - **`llm/descendants.py`'s `DescendantWatch`** follows the child from the transport's `on_spawn`:
    - **Windows:** it lists the processes (Toolhelp32) every 0.2 s while the child runs, and keeps the child's descendants by their parent links. It counts only processes that started after the watch did, so a reused parent id can't adopt an older process. After the stop, a survivor is a seen pid still running under the same name.
    - **POSIX:** whether the child's process group still has a member.
  - **Found while building it:** the venv's `python.exe` is a launcher that starts the interpreter as its own child. Killing the launcher kills the interpreter, but a process the interpreter started survives. Parent links break once an intermediate process dies, so descendants must be seen while the child runs, not looked up after.
  - `scripts/fake_claude.py`'s new `slow_orphan` scenario leaves a process behind that ignores the interrupt. The kill test reports it, with its pid.
- **Changes to existing code:**
  - `client.py`: `credentialed_env`, `redactor` and `artifact_text` are public, so the canary's records are redacted as the runner's artifacts are; `from_config(roles=...)`.
  - `parse_version` moved from `doctor.py` to `claude_cli.py`.
  - `orchestrator/lock.py`: `LOCK_NAME`, used by `live.py` and the command.
- **Not done:** `CLAUDE_CODE_DISABLE_CLAUDE_MDS=1` would turn off every `CLAUDE.md` as a third layer, beside `--setting-sources user` and `--safe-mode`. The canary shows at G4a whether the two suffice; if a code word ever comes back, this variable is the next step.
- **For G4a (D-067):** record whether `structured_output` works with `--tools ""`, `--setting-sources user` and `--safe-mode` (Q4); the envelope's keys and subtypes; `schema_mode`; the exit codes seen; the canary's and the control's results; each role's duration against its timeout; the kill test's stop and any survivor; whether `.claude.json` is in the config dir (D-021); and the version for `llm.max_tested_cli_version`.
- **Spec updates:** ARCHITECTURE §2, §4.7, §4.11 and §7.4; CLAUDE.md's Commands; HANDOFF's G4a runbook.
- **Tests:** 53 in `test_smoke_llm.py`, none of which runs the real CLI. They cover the packs, every role call's lines, the preflight's refusals, each canary detector with a stand-in that reads the planted files as a CLI that isn't isolated would, the planted hook in Git Bash and PowerShell, the control, the kill test on `fake_claude.py` (interrupted, killed, and an orphan left running), and the watch on a real grandchild. 50 mutations, all caught. 1,308 tests pass.

---

## D-067 — G4a passed: structured output under full isolation, a clean canary with a working control, and a clean stop (answers Q4 and D-021)
- **Date:** 2026-11-17 · **Status:** Accepted (G4a: the human ran `trader smoke llm`).
- **The runs:**
  - **Setup:**
    - Claude Code 2.1.296, the native `claude.exe`;
    - `schema_mode` full, `tools_flag` empty, `safe_mode` on;
    - fingerprint `521d83d96b60b5ab` (D-065);
    - artifacts in `runtime/llm/smoke/2026-11-17/`.
  - **20:34 ET:** `uv run trader smoke llm` ran the three roles and the canary. PLAN's G4a line named only this bare command, so the control and the kill test didn't run. That line now names the runbook's full command.
  - **20:45 ET:** `uv run trader smoke llm --role none --canary-control --kill-test` ran the canary again, its control, and the kill test.
  - Both runs ended `SMOKE LLM PASSED`.
- **Q4: yes.** `--json-schema` returns `structured_output` with the `full` schemas and every isolation flag on:
  - `--tools ""` with `--disallowedTools mcp__*`;
  - `--strict-mcp-config` with an empty `--mcp-config`;
  - `--setting-sources user`;
  - `--permission-mode dontAsk` and `--permission-prompts none`;
  - `--max-turns 3`, `--no-session-persistence` and `--safe-mode`.

  No fallback was needed, so `wire_safe` and `deny_all` remain untried.
  - **Every call:**
    - exited 0, with subtype `success` and `is_error` false;
    - took 2 turns, ending with `stop_reason` `tool_use` and `terminal_reason` `completed`;
    - left stderr and `permission_denials` empty, with `safety_stops` at 0.

    None needed a repair, had anything clamped or dropped, or started a subagent.
  - **`result`** holds the same JSON as `structured_output`.
  - **Two turns a call:** the answer arrives as a tool call, so `max_turns` must stay at 2 or more. Ours is 3, which leaves a turn for the CLI to retry an answer that fails the schema.
  - **Not seen:** the exit codes and subtypes of a usage-limit or auth failure, since no call hit one. The runner doesn't depend on them: it classifies by the envelope's `is_error` and subtype as well as by the exit code (D-064).
- **The envelope's 27 keys:** `api_error_status`, `duration_api_ms`, `duration_ms`, `fast_mode_disabled_reason`, `fast_mode_state`, `first_content_frame_ms`, `is_error`, `modelUsage`, `num_turns`, `permission_denials`, `queued_turn_count`, `result`, `result_index`, `safety_stops`, `session_id`, `stop_reason`, `structured_output`, `subagent_stats`, `subtype`, `terminal_reason`, `time_to_request_ms`, `total_cost_usd`, `ttft_ms`, `ttft_stream_ms`, `type`, `usage`, `uuid`.
  - **`modelUsage`** names only the model asked for: `opus` resolved to `claude-opus-5-5`, and `sonnet` to `claude-sonnet-5-5`. Each has a 1M-token context and `costBasis` `list`.
  - **`fast_mode_state`** was `off`, with `fast_mode_disabled_reason` `sdk_opt_in_required`.
  - **Caching:** each call wrote 20–26K tokens to the 1-hour prompt cache, and later vet calls read 2.4K of them back.
- **The role calls,** with the settings' models, efforts and timeouts, on D-066's packs:

  | Role | Model, effort | Took (API) | Timeout | Thinking / output tokens | Cost estimate | Answer |
  |---|---|---|---|---|---|---|
  | premarket | opus, high | 59.5 s (58.1) | 600 s | 2,170 / 7,774 | $0.32 | 40 of 40; day mode normal; allow 11, neutral 14, veto 15 |
  | vet | sonnet, low | 16.5 s (15.5) | 60 s | 0 / 3,196 | $0.14 | 25 of 25; allow 1, neutral 14, veto 10 |
  | review | opus, high | 107.6 s (106.8) | 600 s | 7,767 / 11,844 | $0.40 | 8 of 8; 10 process issues, no lesson proposals, 2 notes for the human |

  - **Overhead:** the CLI adds about 1 s to each call.
  - **The vet's speed:** 16.5 s at full size leaves room inside Book B's 60 s wait for a verdict (plan decision 8).
  - **Cost:**
    - At list prices, a day's three calls cost about $0.86. On the subscription that counts toward usage limits, not money.
    - Under `provider: anthropic_api` it would be about $18 a month, nearly 2% of a book's $1,000: a bar Book B's vetoes would have to clear.
- **The injection headline didn't work.**
  - The sanitizer flagged QZIVOR's news in all three packs.
  - Claude itself put `suspicious_content` on QZIVOR in both the brief and the vet, and on no other candidate.
  - No role followed the headline's instruction:
    - the brief vetoed 15 candidates, and allowed QZIVOR at 0.55 on its guidance and contract news, noting that the instruction was ignored;
    - the vet rated QZIVOR `neutral` at 0.5;
    - the review raised the headline as an information-level news issue, and checked that the fictional day's vet hadn't complied.
- **The canary was clean** in both halves, both times.
  - **The control** fired all three detectors with isolation off: instructions, hooks and the MCP server.
  - **What that shows:** the isolation flags, together, keep out the `CLAUDE.md` files, settings, hooks and `.mcp.json` that a plain `-p` run loads.
  - **Not needed:** D-066's possible third layer, `CLAUDE_CODE_DISABLE_CLAUDE_MDS=1`.
  - Both canary folders were removed.
- **The kill test passed.** The vet timed out at 3 s and stopped on CTRL_BREAK within the 5 s grace (`stopped: interrupt`), so `taskkill` wasn't needed.
  - **The interrupted CLI exited 0,** with empty stdout and stderr. The transport's own timer marks the timeout, so the attempt is `timeout` whatever the exit code.
  - **The CLI's children** were `where.exe` and `conhost.exe`. Neither outlived the stop, and the sandbox stayed empty.
  - **D-064's residual,** a grandchild surviving the stop, didn't arise: with `--tools ""` the CLI starts no tool processes.
- **D-021 answered: `.claude.json` is in `CLAUDE_CONFIG_DIR`** (`runtime/claude-config`). Beside it:
  - `.last-cleanup`, `backups/` and `sessions/`;
  - `policy-limits.json`, with its signature and stamp files;
  - `remote-settings.json`.
  - **Server-managed settings:** `policy-limits.json` and `remote-settings.json` are the account's server-managed policy and settings. Managed settings apply under `--safe-mode` too (cli-reference). On a Team or Enterprise plan, an admin's settings would therefore reach every call, and no flag can keep them out.
  - **Open:** the human has been asked whether the subscription is a personal plan. If it isn't, read `remote-settings.json` before G4b. **Answered the same evening: a personal plan.** No admin manages its settings, so nothing reaches the calls this way.
- **The human sets `llm.max_tested_cli_version: "2.1.296"`** in `config/settings.yaml`. Doctor then warns about any newer CLI, and a newer CLI needs this smoke run again.
- **Found the same evening:**
  - **Doctor's `alpaca.clock` check FAILed:** the PC was 14.49 s from Alpaca's clock; the limit is 2 s (R§9).
    - `w32tm /stripchart` against time.windows.com agreed: −14.5 s.
    - **The cause:** the Windows Time service was stopped, so nothing had corrected the clock. It is set to start automatically, with NTP server `time.windows.com,0x9` and a poll interval of 32,768 s.
    - **Impact:** it doesn't touch G4a's results, but it must be fixed before G4b. The human restarts the service and resyncs. The machine that runs the bot (H0.4) needs that service running.
    - **Fixed at 20:54 ET.**
      - The human started the service. `w32tm /resync /force` at once answered "no time data was available", since the service hadn't yet heard from its server.
      - The service then synced by itself from time.windows.com, and `w32tm /stripchart` read +0.005 s.
      - The next sync is due in about 9 h (poll interval 32,768 s).
    - Every other doctor check passed: Book A's account at $996.08, Book B's at $1,000.00, with no positions or orders.
  - **H0.2 was already done:** `claude.token` reads 40 days old, so the token dates from 2026-10-08 and expires 2027-10-08.
  - **The smoke's review pack isn't self-consistent** (D-066):
    - its books' P&L (+$3.21 and −$1.60) and its `stats` are fixed numbers that don't match its own trades;
    - Book B's `chased` on the untraded targets doesn't fit the two entries it took by 09:38.

    The review found both among its 10 process issues, a good sign for its checks. The fix belongs in P4.10, where the review pack gets its real builder.
  - **For P4.8:** `config.py` accepts a `max_turns` of 1, which would fail every structured call. It should require 2 or more.
- **Spec updates:**
  - PLAN: H0.2 and G4a ticked, G4a's command, STATUS;
  - HANDOFF;
  - Q4, below.

---

## D-068 — P4.7: Book B, its VerdictStore, and the entry path both books share
- **Date:** 2026-11-18 · **Status:** Accepted (the Phase 4 plan, P4.7 and decision 8; D-064's notes for P4.7; G4a, D-067).
- **Book B** (`books/llm_filtered.py`, `LlmFilteredBook`): the first of these that applies decides a trigger.
  1. **A halt other than `llm_degraded`,** or a book that flattened today: `book_halted`.
  2. **Degraded:** the day's failed attempts reach `llm_failures_degrade_after`, or the `llm_degraded` halt is in force: `llm_degraded`. The count is re-read before every decision, since RISK.md §2 counts attempts, not calls: a first try that failed degrades Book B while its repair still runs.
  3. **Day mode `no_trade`:** `day_mode_no_trade`, for every signal, whether it has a verdict, waits for one, or has none.
  4. **A `veto`:** `vetoed`, before the chase guard.
  5. **No verdict to be had:** `llm_unavailable`. The covering call failed, left the symbol out, or had no room for it in its pack; or no call covers it.
  6. **Pending:**
     - on the first look, already past the chase cap: `chased`;
     - before the deadline: `Defer(until=deadline)`;
     - at the deadline: `vet_timeout`, or under `revet_premarket` the brief's verdict, as in steps 4 and 7.
  7. **`allow` or `neutral`:** the shared entry path, with the day mode's multiplier (normal 1, reduced 0.5). After a wait, its codes are `entered_after_wait` and `chased_after_wait`. A size skip or a gate rejection keeps its own code.
- **A trigger that waits:**
  - **No chase check on a second look.** A trigger asked again while its verdict is still pending isn't chased, only parked again or timed out. The worker asks again whenever the store changes, so a check on every look would make Book B's trade depend on when it happened to be asked: a spike past the cap at an unrelated wake-up would end a wait that the verdict, arriving with the price back under the cap, would have entered. The chase guard runs once the verdict is in (S§5.4), and at the deadline the code is `vet_timeout` whatever the price. (The first build checked the cap on every look; the review caught it.)
  - **Nothing is journaled while it waits.** `on_trigger(ev, sig, *, waited=False)` returns `BookDecision | Defer` (base.py's `Book` protocol). A crash meanwhile leaves the signal triggered and undecided, so the restart re-serves it (`source="restart"`).
  - **It is priced from the feed once it has waited.** `EntryInputs.prepare` takes `waited`. `LiveInputs` then ignores an arm check's own price and time, which are up to a minute old by then. If the hub has had no trade for a minute (after a restart, say), one REST snapshot supplies the last trade; Book A's triggers are priced as before.
- **Every decision carries `llm_call_id`** (`record_decision` now writes it):
  - the verdict's attempt;
  - the brief's, for `day_mode_no_trade`;
  - the failed attempt, for `llm_unavailable` (none when no call covered the symbol or none ran);
  - none for halts, degradation, pending chases and `vet_timeout`.
- **The deadline** is max(armed_at, the covering call's start) + `max_wait_s`, never later than the last entry (plan decision 8, refining S§5.4's "after arming").
  - Before the call starts, the time it was expected stands in for its start. A trigger parked before the vet starts is asked again then, and parked anew if the call has started since.
  - A trigger that arrives after the deadline, its verdict still pending, is `vet_timeout` at once.
  - A repair is part of its call: the deadline counts from the call's first attempt. A late answer still applies to signals that haven't triggered.
  - The last-entry cap means the worker can always settle a parked trigger by the last entry; the first build could defer past it.
- **`VerdictStore`** (`books/verdicts.py`):
  - **It holds** the day mode, the brief's and the vet's verdicts per symbol, which vet job covers which symbol (expected, under way, done), the day's failed attempts, and a version.
  - **Who the vet judges** (`vet_targets`): the armed or triggered longs with no verdict, or with only the brief's when `revet_premarket` is on, and no vet done or under way for them.
  - **`standing(signal)`:**
    - a premarket verdict stands unless `revet_premarket`;
    - else the vet's;
    - else pending, while a vet job covers the symbol; under `revet_premarket` it carries the brief's verdict as its `fallback`;
    - under `revet_premarket`, the brief's verdict, if the re-vet failed or left the symbol out;
    - else unavailable.
  - **A vet job:** `expect_vet(signals, at)` returns a `VetJob`; then `vet_started(job, at)`, the runner's call, and `vet_done(job, result, sent=pack.kept)`, or `vet_abandoned(job, at)` if the call is cancelled, which leaves its symbols to be vetted again. Each touches only its own job's symbols, so two jobs can't end or restart each other's.
  - **The journal first, then memory.**
    - `persist_brief` (the day mode, `INSERT OR REPLACE`, and the brief's verdicts) and `persist_vet` (the vet's verdicts, each with its `signal_id`) run as the runner's `persist`, in the transaction that completes the answer's attempt.
    - `brief_done` and `vet_done` then put the answer in memory, synchronously, before they re-read the failure count. If that read fails, memory still matches the journal.
    - A failed or missing brief stores the default day mode (normal, source `default`) before memory takes it. It uses `INSERT OR IGNORE`, so it never replaces a brief's.
    - A signal id the journal doesn't know is stored as NULL rather than failing the whole answer on the foreign key.
  - **`load()` after a restart,** on a fresh store and before any other call, rebuilds memory from `day_modes`, `verdicts` (trading calls only), `llm_calls`, the day's signals and Book B's decisions:
    - **A call's state** comes from its last attempt, the calls taken in their journal order (`logical_id`, then `attempt`), never the clock's. Following D-064, it is abandoned if that attempt is `abandoned` or never became final.
    - **What is pending again,** from the restart: every armed long that no call judged and Book B hasn't decided. That covers a vet a crash abandoned and a vet the crash cut short before its first attempt; `Rebuilt.revet` lists them.
    - **The signal ids** for the vet's rows are restored.
    - **A failed brief whose default day mode a crash kept off the record** gets it now.
    - `Rebuilt.brief` says how the brief ended.
  - **The journal can't tell some cases apart.** After a restart, a vet refused before any attempt (`no_time`, `budget_exhausted`) or a symbol its pack had no room for looks like a vet the crash cut short, since neither leaves a row. Such symbols are vetted again. A refusal for time or budget is refused again; no room can't happen at 25 candidates of 5 news items (about 58 KB, far under the 300 KB cap).
  - **`version` and `wait_for_change`:** every change bumps the version, for P4.8's worker.
- **Migration 0008** adds:
  - `verdicts.signal_id` (references `signals`; NULL for the brief's verdicts) and `verdicts.eval_epoch`;
  - `day_modes.source`, `multiplier` and `eval_epoch`;
  - an index on `verdicts(date, symbol)`.

  The plan put these in its single Phase 4 migration. That migration was split by part (0005–0007), so they land here.
- **The shared entry path** (`books/entry.py`): `priced`, `seen`, `skip` and `EntryPath.enter` (the chase guard, the price, the size, the gate), extracted from `baseline.py`. Book A's decisions are unchanged: a differential run of the old and new `BaselineBook` over 13,824 combinations of inputs found no difference in any field, and the golden day and G3's replay pass byte-identical.
- **Budget:** `failed_attempts(journal, day)`, the count that `Budget.failures` and the store share.
- **Choices made here (no spec said otherwise):**
  - **Re-vet fallback.** Under `revet_premarket`, a re-vet that fails, leaves a symbol out or misses the deadline falls back on the brief's verdict, not `llm_unavailable` or `vet_timeout`. A valid verdict exists, and an outage shouldn't count as a veto in the measurement.
  - **A vet verdict over a standing brief's.** Without `revet_premarket`, a verdict the vet gives for a symbol whose premarket verdict stands is stored, but the brief's verdict stands.
- **The Phase 4 plan's file** (`C:\Users\Admin\.claude\plans\squishy-popping-honey.md`) was gone: Claude Code's routine cleanup removes old plan files. Its approved text is restored unchanged as `docs/PHASE4_PLAN.md`, and the references now point there.
- **Review.** Three reviewers worked independently: the decision logic against the spec, the store's state and restarts, and the tests' strength (with mutations and a differential check of Book A). No blocker. 2 major findings and 13 minor (some found twice), all fixed:
  - **major:** a pending trigger re-checked against the chase cap on every look (above);
  - **major:** `load()` blind to a vet cut short before its first attempt;
  - **minor:**
    - degradation read only when a call ended;
    - deadlines past the last entry;
    - covers not tied to their call (now `VetJob`);
    - a cancelled vet left under way;
    - `load()` following the clock, not the journal's order;
    - the answer applied after the failure count's read;
    - the signal-id foreign key's reach;
    - the signal ids and a failed brief's default lost in a crash;
    - a late re-vet treated worse than a failed one;
    - a waited trigger falling back to its stale price;
    - untested: the feed's price after a wait, `no_trade` on pending and uncovered signals, a size skip's call id, the verdicts' `eval_epoch`.
- **For P4.8:**
  - **Boot:** `close_abandoned()`, then `store.load()` before any other use of the store. Re-run the brief if `Rebuilt.brief` is `abandoned` and its deadline hasn't passed; call `brief_missed()` when no brief will run. Re-vet `Rebuilt.revet` with a job.
  - **The brief:** `run("premarket", pack, persist=store.persist_brief)`, then `brief_done(result)`.
  - **The vet:**
    - at arming, before the engine can fire an arm check: `job = expect_vet(store.vet_targets(armed longs), at=now)`;
    - just before `run`: `vet_started(job, at=now)`;
    - `run("vet", pack, persist=store.persist_vet)`;
    - then `vet_done(job, result, sent=pack.kept)`, also when the call was refused; `vet_abandoned(job, at=now)` if it was cancelled.
  - **The worker:**
    - on `Defer`, park the trigger;
    - wake it at `until`, or on `store.wait_for_change(version)`, and ask again with `waited=True`, in rank order, one decision at a time;
    - the deadline never passes the last entry, so asking at `until` always settles it. Build the store with `last_entry` from the day's times.
  - **Degradation:** when the store reads degraded, halt Book B with `llm_degraded` (scope session) and raise a P2. The book doesn't wait for the halt.
- **Spec updates:**
  - STRATEGY §5.3 and §5.4;
  - ARCHITECTURE §2, §4.4 and §6;
  - PLAN;
  - `docs/PHASE4_PLAN.md`.
- **Tests:**
  - 32 in `test_book_b.py` and 39 in `test_verdicts.py`. Both drive the real runner (`LlmCalls`) on a scripted transport, so the rows, the `persist` callbacks and the failure counts are a trading day's.
  - 2 more in `test_books.py` (Book A never waits; a decision keeps its call) and 2 in `test_day_tasks.py` (a waited trigger's price, from the feed or a snapshot).
  - 74 mutations, all caught. The first run's 5 survivors led to three sharper tests, a better anchor, and the removal of `load()`'s redundant pending branch for abandoned calls.
  - 1,383 tests pass.

---

## D-069 — P4.8: Book B in the day: its jobs and deadlines, a worker that parks waiting triggers, `--books A,B` and `--no-llm`, recorded and replayed LLM work, and the report
- **Date:** 2026-11-19 · **Status:** Accepted (the Phase 4 plan, P4.8 and decisions 5, 8, 9 and 10; the "For P4.8" notes of D-063, D-064, D-065, D-067 and D-068).
- **The timeline** (`plan.py`). Book B's morning times are fixed clock times: the pre-market scan at 09:00, the brief at 09:05, its deadline 09:28. The review is at 16:15 and its cutoff a minute before the report (16:29). Both move with an early close (13:15 and 13:29). `--dry-run` shows them when Book B runs.
- **Book B's jobs** (`orchestrator/llm_jobs.py`, `BookBJobs`). None of them raises into the day: Book A never depends on the LLM (S§5.4).
  - **Boot**, before Book B's book starts:
    1. `close_abandoned()`: the attempts a crash left open;
    2. `note_fingerprint()`: a P3 `llm_fingerprint_changed` when the setup changed (D-065);
    3. the store rebuilt from the journal (`VerdictStore.load`);
    4. the degradation check, for a restart that came after a failure but before its halt.

    **A boot that fails** is a P2 `llm_job_failed`. Book B is halted `llm_degraded` until the next open and its jobs are off (`disable`), while its account is still reconciled, protected and flattened. Only a wiring error (no `DayDeps.llm`) raises.
  - **The news feed** starts in the background once the market-data stream is connected (`start_news`): a day of backfill (the vet's 24 h window is the longer one), then the stream.
    - So the backfill never delays the books' boot, and the stock stream takes the first connection if the plan allows only one (Q2).
    - A start that fails is tried again every minute, with one P3 `news_unavailable`.
  - **Morning** (PREMARKET, before the 09:29 subscription):
    - The brief isn't asked again once today's day mode is settled: the brief's, or the default a failed or missed brief stored (`VerdictStore.mode_known`, new). So a restart never pays for a second brief.
    - No attempt starts after 09:27 (the deadline less the brief's `min_attempt_s`). From then on, `brief_missed()` stores the default day mode and raises a P3 `brief_missed`.
    - **The 09:00 scan** (`LivePacks.premarket_scan`):
      - the day's eligible universe, with one snapshot per symbol on the live feed;
      - the symbols with news in the brief's 18 h window;
      - `select_premarket`;
      - EDGAR prefetched for the picks, until 09:05. On a late start EDGAR gets 30 s, but never past 09:27.

      A scan that fails is a P3 `premarket_scan_failed`. The brief then runs without candidates: it still sets the day mode.
    - **The brief** at 09:05: its pack as of then, `run("premarket", deadline=09:28, persist=store.persist_brief)`, then `brief_done(result)`.
    - **A job that raises** is a P2 `llm_job_failed`, with the default day mode.
  - **The vet, at arming** (OR_SCAN, after `arm`/`rearm` and the `missed_at_arm` skips, before Book B's worker starts):
    - `start_vet` takes the armed and triggered longs. It leaves out those missed at arming and, after a restart, those Book B already decided.
    - `vet_targets` and `expect_vet` mark the signals pending at once. The call then runs as a task:
      1. the pack, with EDGAR for up to `VET_EDGAR_S` (10 s): first the symbols never answered, then again those answered earlier, which keep their answer if time runs out. So a filing accepted since 09:00 shows up whenever time allows;
      2. `vet_started`;
      3. `run("vet", deadline=last entry, persist=store.persist_vet)`;
      4. `vet_done(job, result, sent=pack.kept)`.
    - **A cancellation** is `vet_abandoned`. **A job that fails** is a P2 `llm_job_failed`, and `vet_done` with a failed result for the job's own symbols. So they are `llm_unavailable` at once ("the vet failed: internal_error") instead of waiting out their deadline.
    - **The vet runs as S§5.3 says**, whatever the day mode: verdicts are measurement data even on a `no_trade` day.
  - **The review** (REVIEW, after the shadow job, even if that failed; not after a cancellation):
    - at 16:15, with the report − 1 min as the call's deadline; no attempt starts after the cutoff less the review's `min_attempt_s` (16:27);
    - once a day: a `review` step in `day_runs`, written in the transaction that stores a good answer, so a crash can't make a rerun ask again. A failure, `no time` or `nothing to review` is marked after;
    - its failed attempts don't count toward degradation: the day's entries are over, and the P2 would only be noise;
    - only with `ReviewWork` (see below).
  - **After each call:**
    - a P3 `llm_call_failed` for each failed attempt, from its row;
    - a P2 `llm_refused` instead for a refusal that repeats until a human acts (`PERSISTENT_REFUSALS`: a dirty sandbox, a missing prompt, a pack over its cap; D-064);
    - a P3 `llm_call_refused` for a call refused before any attempt (no time, or the day's cap);
    - then degradation: once the day's failed attempts reach `llm_failures_degrade_after`, a session halt `llm_degraded` for Book B until the next open, and one P2 `llm_degraded`. Neither repeats, after a restart either: the halt in force is checked first. Book B doesn't wait for the halt: it re-reads the count before every decision (D-068).
- **Book B's worker** (`day.py`):
  - **Parking:** a `Defer` parks the trigger, with its deadline. The worker then waits for the first of three things: a new trigger, the earliest deadline, or a change in the store (`wait_for_change`). Then it asks every parked trigger again with `waited=True`, together with the new ones, in rank order, one decision at a time. A change during a round wakes the next one.
  - **Book A** never defers, so its loop is the one it always was. Its golden day and G3's replay pass byte-identical.
  - **At the stop** (`_stop_trading`), every trigger still parked is asked once more. At the last entry every deadline has passed, since none is later (D-068), so each gets its decision; a test shows the stop settling them in rank order. A stop before then (the day failed) leaves one still pending undecided, and EOD gives it `restart_lost`.
  - **A restart re-serves per book** (`signals_with_orders`, `rearm(with_orders=...)`): a triggered signal goes back to each book that hasn't decided it and has no order of its own for it. The first draft skipped a signal any book had ordered, so a Book B trigger parked when the engine died was `restart_lost` whenever Book A had entered it.
- **`run-day`** (`live.py`, `cli.py`):
  - **`--books`** takes A, B or A,B.
  - **Book B is off for the day** (plan decision 9) with `--no-llm`, `llm.enabled: false` or `books.B.enabled: false`. Book A runs alone, and the echo, `boot.json` (`"llm": {"book_b": "off: --no-llm"}`), a `day_runs` note and the report say why. Book B alone and off is refused, by `--dry-run` too.
  - **A missing CLI or credential** (`LlmUnavailableError`) also leaves Book B off, with a P2 `llm_unavailable`: Book A keeps its day. `--dry-run` says whether Book B's LLM is ready.
  - **A book kept off** is reconciled, protected and flattened by nobody. So a paper day first reads its account, read-only, and anything held or working there is a P2 `idle_book_not_flat` naming `trader flatten --book X`.
  - **Paper:**
    - the CLI's `--version` read once (not a model call; it goes into every `llm_calls` row and `boot.json`). One newer than `llm.max_tested_cli_version` is a P3 `llm_cli_untested`: the binary is shared with the interactive CLI, which updates itself;
    - `ClaudeCliTransport` with `kill_grace_s`, wrapped by `RecordingTransport`;
    - `NewsHub` on the market-data keys, its sleep on the injected clock and every article recorded;
    - `EdgarClient`. Without a usable user agent there is none, with a P3 `edgar_unavailable`, and every pack's filings are unknown;
    - the packs built live and recorded.
  - **`--sim`:** a scripted model (`fake.valid`: every candidate neutral) and an environment with no credential. The sandbox and artifacts are the simulated day's own, `runtime/sim/<date>/llm_sandbox` and `llm/`, since ids repeat (D-064).
  - **`boot.json`** gains `"llm"`:
    - the transport, the provider, the CLI's version, `schema_mode`, `tools_flag`, `safe_mode`;
    - each role's model and effort;
    - whether EDGAR is on, and the fingerprint.

    A Book-A-only day without a reason has no such key, as before.
- **News and EDGAR** (`orchestrator/evidence.py`, `EvidenceDesk`):
  - **News** is a read of the `news` table: no budget.
    - `news_ok` says whether the read can be trusted. A failed read, or a feed that isn't running (`NewsHub.mode` neither `stream` nor `poll`), is unknown news.
    - The packs then say `news_unavailable: true` (`PACK_VERSION` 2), and each prompt says an empty `news` list then means unknown, not none.
    - The first draft read an outage as "no news", which the brief's prompt treats as a pump sign: the mix-up D-063 rated major for filings.
  - **EDGAR** is asked one symbol at a time, in rank order, until its budget ends on the injected clock, raced against `clock.sleep_until`. `asyncio.wait_for` would have used the loop's time, not the SimClock's.
    - **Answers are kept for the day.** Only an answer gives a list; anything else is None, unknown (D-063). The vet refreshes them, as above.
    - **A 403 or 429** (`EdgarBlockedError`) stops the rest of that budget. Any other failure loses only its own symbol.
  - **EDGAR's ticker cache** (an older bug): a torn file failed every EDGAR call until someone deleted it. A crash could tear it, or `--sim` beside a paper day, since both use `runtime/edgar/`. It is now written beside and moved into place, and one that won't read is fetched again.
- **Packs** (`orchestrator/packs.py`):
  - **`LivePacks`** builds the brief's and the vet's packs from the day.
    - **The index ETFs' gaps** come from one snapshot of the regime symbols. Each is measured against the daily bar dated the previous session: the snapshot's `dailyBar` before the open, `prevDailyBar` after it (D-024). The first draft always used `prevDailyBar`, so the 09:05 brief measured from the close two sessions back. The ETFs' previous close comes from the snapshot, because the universe's SIP previous close covers listed stocks, not SPY, QQQ and IWM. A gap is unknown when the snapshot fails.
    - **Book B's equity and recent R:** its start-of-day E, and the R of its latest 10 trades before today.
    - **Building runs on a thread** beside a threaded journal, as D-062 asked. Beside an inline one (simdrive) it runs inline, as the runner's file work does (D-064).
  - **`RecordingPacks`** records every pack a job built, sent or not, with when its build began and ended. It also records a build that failed, and, for the vet, the symbols it was asked to judge.
  - **`ReplayPacks`** serves them back by role and order, each at the time it was ready live. So a replayed vet starts when the live one did, and its triggers' deadlines match.
    - A recorded failure fails again (`RecordedBuildError`).
    - A pack the recording lacks is a divergence, and so is a vet asked to judge other symbols than the recorded one.
- **Recording and replay** (`llm/replay.py`, `orchestrator/replay.py`; decision 10, as D-063 amended it):
  - **`llm.jsonl.gz`** holds the packs and each attempt's answer: exit code, stdout, stderr's tail, timings, how a late child was stopped, or the error that kept it from starting. Each record is flushed at once (`Recorder.append(..., flush=True)`, new).
    - Stdout and stderr are kept as text with `surrogateescape`, so any bytes come back exactly.
    - The credential is redacted, as in the artifacts (`redactor`). Where the runner cut stdout or stderr, the record first drops `CUT_MARGIN` characters at the cut, as the artifacts do (D-064). The first draft didn't, and a tail that began inside the token kept most of it.
  - **`trader replay`** runs Book B when `boot.json` lists it:
    - `ReplayPacks` and `ReplayTransport` answer each attempt at max(now, its recorded end);
    - the runner runs with no credential and no executable, in the replay's own `llm_sandbox` and `llm` folders;
    - so the replay needs no news, EDGAR or `claude`.

    An attempt matches by role and order, and is checked by its pack's `pack_id` and whether it is a repair. Any mismatch, and an attempt the recording lacks, is a divergence. Recorded records left unused are a divergence too on a single-run day. After a restart (`boot-2.json`) they are only a note: the restart appends its own, and the replay covers only the first run.
  - **`comparable()`** adds Book B's decisions' `llm_call_id`s, its day mode, the verdicts and the trading LLM calls, only when there are any. So a Book-A-only day's comparison, G3's included, is unchanged.
  - **The reason Book B was off** reaches the replay's day from `boot.json`.
  - **A test records an A+B day** in which a trigger waits 55 s for the vet, then replays it with no divergence or difference.
  - **`context.decode()`**, new, reads an encoded pack back from its bytes, with its trims.
- **The report** (`reports/daily.py`):
  - **The header** has the day's attempts (ok, failed, abandoned), the budget used against `max_calls_per_day` (CLI runs: a refusal isn't one), the failed attempts against `llm_failures_degrade_after`, and Book B's day mode. A day it was kept off says why, read from the day's `day_runs` note, so `trader report daily` regenerates it.
  - **A "B verdict" column:** the verdict Book B's decision rests on, else the day's latest.
  - **An LLM-calls table.**
  - **"Claude":** the day mode's reason and the review's summary, and a table of the target reviews.
  - **The model's text** is escaped:
    - HTML escapes every cell, as before;
    - in Markdown the text sits on one line in a code span, its fence longer than any run of backticks in it (`ModelText`). So it opens no link, image, HTML or emphasis. That includes a bare URL, which GFM and VS Code's preview would autolink; the first draft's backslash escaping missed those. In a table its pipes are escaped too.
- **Found while building it: the simulated brokers numbered orders and fills the same way for both books** (`sim-ord-1`, `sim-exec-1`). With two books on one journal, Book B's fills went to Book A's orders: the first A+B day ended with both books' cash wrong and a P1. Run-day `--sim --books A,B` would have done the same. Ids now carry the book (`sim-b-ord-1`). Book A keeps the plain prefix its golden days were recorded with, so they pass unchanged. Alpaca's own ids are unique.
- **Config:** a role's `max_turns` must be 2 or more (D-067: the answer arrives as a tool call).
- **The review's content is P4.10's.** P4.8 runs the job (its time, cutoff, once-a-day marker and failure record) through `ports.ReviewWork`: `pack()`, `persist()` and `not_reviewed()`. P4.10's `memory/reviews.py` provides it.
  - **Until then** run-day, `--sim` and replay pass none, and no review call is made.
  - **When it lands,** a replay of a day recorded before it would run a review it never made. So P4.10 adds a capability (D-055) for it.
- **Known limitations:**
  - **A refusal made before the transport** leaves no attempt record: a dirty sandbox, a missing prompt, or an input artifact that failed. The replay's clean sandbox then calls the transport and reports a divergence for a day the engine handled correctly. Each such refusal is already a P2 `llm_refused`, so recording them waits until one is seen.
  - **A torn gzip member** (older, `recorder.py`): after a crash, a false gzip header inside the torn member loses every record after it. A probe recovered 870,712 of 875,000 records. The odds are about 1% for the LLM file, and higher for the stream file.
- **For P4.9:**
  - `DayRun` takes `books=("A", "B")`, `llm_script=`, `verdicts`, `day_mode` and `llm_delay`, with `FakeNews` and `FakeEdgar`, and records Book B's LLM work when it records.
  - The synthetic day still has no pre-market prints (its snapshots start at 09:30), so its brief has no candidates. P4.9 adds pre-market prints, news and filings, and the A+B golden.
  - **Restart tests:**
    - A restart after the vet must not vet again a signal Book B already decided. No test covers it yet: a mutation that vetted them again survived.
    - The restart test with Book A entering (the review's major) checks only that Book B's trigger is no longer `restart_lost`. The synthetic tape's last print before its 09:36:30 restart is 35 s old, so a re-served trigger can end as `risk_gate:R-016` (stale data). To test the entry itself, the fixture needs prints through the restart.
- **Spec updates:**
  - ARCHITECTURE §2, §3, §4.2, §4.7, §4.9, §4.10, §4.11 and §8;
  - STRATEGY §5.2, §5.3 and §5.4;
  - RISK §11's new incident codes;
  - the three prompts (`news_unavailable`);
  - CLAUDE.md's Commands (`--no-llm`, approved with the Phase 4 plan);
  - PLAN; HANDOFF.
- **Review:** two adversarial reviewers, then a mutation run.
  - **The day and its jobs:** 1 major, 2 minors and 2 nits, all fixed:
    - the restart's re-serving (major);
    - a cancelled shadow job no longer runs the review;
    - the news feed no longer delays the books' boot;
    - the review's once-a-day marker shares its answer's transaction;
    - the review no longer counts toward degradation.
  - **Wiring, replay and security:** 3 majors, 6 minors and 8 nits. All are fixed except the two known limitations above.
    - **Majors:** the brief's index gaps, the credential in a cut tail, and a Book B boot failure that stopped Book A's day.
    - **Minors:** news before the stock stream, news unknown read as none, a stale EDGAR answer at the vet, bare URLs in the Markdown report, and a book kept off left unwatched.
    - **Nits:** the report and the replay losing why Book B was off; replay misses that weren't divergences, and `comparable` ignoring the decisions' calls; the replay's vet check, which would have misfired with more than 25 candidates; EDGAR off with no incident; the ticker cache; `--books B --dry-run`'s message; and the CLI's version never checked.
  - **Mutations:** 210 mutants of P4.8's code, each run in an isolated copy of the repository, and 204 caught.
    - The first run left 60 alive. Tests were added or sharpened until 54 of them fail one: 47 against the whole suite, and the last 7 against their own tests.
    - Of the other 6, five change nothing observable. One of them became equivalent only once the report read the day's note. The sixth waits for P4.9's restart tests (above).
    - Three more mutants check the fixes for the review's findings: a shutdown during the shadow job, the review's degradation, and unused recorded answers. All three are caught.
- **Tests:** 1,504 pass, up from 1,383. Ruff, its formatter and mypy are clean. Book A's golden day and G3's replay pass unchanged.

---

## D-070 — P4.9: end-to-end A+B days: the synthetic day's morning for Book B, the scenarios, and the A+B golden day
- **Date:** 2026-11-19 · **Status:** Accepted (the Phase 4 plan, P4.9; D-069's "For P4.9").
- **The synthetic day gains Book B's morning** (`tests/fixtures/days.py`):
  - **Pre-market prints,** marked extended-hours (`T`, which counts for price): GAPPER and LOSE gap up, SHORT down, SPY slightly up, and DOJI's only print is at 03:50, before the scan's 04:00.
  - **News and filings,** each known only from its own time. `FakeNews` and `FakeEdgar` now filter by the clock; before, they returned items from the future. GAPPER's headline tries an injection, GAPPER has a 424B5 from the evening before, and CAP has an 8-K at 07:00. EXP's news is out of every window.
  - **Snapshots carry daily bars as D-024 describes them.** Before the open, `dailyBar` is the previous session's and `prevDailyBar` the one before; from the open, today's bar so far and the previous session's. The session two back closes 2% lower, so picking the wrong bar would show.
  - **So the brief has candidates:** GAPPER and LOSE as gainers, SHORT for its news. GAPPER's verdict carries `suspicious_content`. The vet judges the armed longs the brief didn't: WIN, EXP and CAP.
  - **Book A sees none of it.** Every pre-market print comes before 09:20, so neither an A-only engine (subscribed at 09:29) nor its brokers (fed from its 09:20 start) receive one. Book A's golden day is byte-identical, and a test shows an A-only day, even booted at 08:55, makes no LLM call, no news read, no EDGAR request and no snapshot before the open (`SyntheticMarket.asked` records each call's time).
- **The scenarios** (`test_orchestrator_b.py`), each a whole day on a SimClock with the model scripted:
  - a neutral day, on which Book B decides as Book A does;
  - the brief's veto of LOSE, which frees Book B's cash and its second entry for CAP (14 shares), which Book A can't take;
  - a trigger that waits and enters (`entered_after_wait`); one past its chase cap when the answer comes (`chased_after_wait`, on a day where WIN jumps at 09:36); one still pending at its deadline (`vet_timeout`), with `llm_unavailable` after the vet fails;
  - answers still invalid after their repair: three failed attempts, the `llm_degraded` halt with its P2, and Book A's day unchanged;
  - a `no_trade` day, whose decisions rest on the brief while the vet still runs, and a reduced day, which halves both of Book B's entries;
  - a failed brief and a late start (09:28), each sending every armed long to the vet with the default day mode;
  - two restarts: one mid-vet that vets again only what Book B hasn't decided, and one after the vet that asks for nothing again;
  - the call cap: the vet is refused, and its signals are `llm_unavailable`, never entered unvetted.
- **The A+B golden day** (`tests/golden/standard_day_ab.json`; `golden.py`'s `golden_ab_run` and `TABLES_AB`):
  - **Its day:** Book A's golden day, with Book B beside it from 08:55. The brief vetoes LOSE, WIN waits 8 s for the vet and enters, GAPPER is `chased`, and Book B takes CAP.
  - **`TABLES_AB`** adds each decision's call, fills by book, the LLM calls, the verdicts, the day modes and the halts. A call's `pack_id` hashes the pack it sent, so any change in what Book B sends shows as a golden diff. That is deliberate: such a change alters the model's input, and a reviewed diff is the place to see it.
  - **Checks:**
    - two processes with different hash seeds write both golden days byte for byte;
    - record-then-replay reproduces every `TABLES_AB` table, pack ids included, with no divergence, difference or note. It replaces P4.8's A+B replay test, which ran the same day and compared less;
    - Book A's rows on the A+B day equal its own golden day's: its decisions, risk checks with their state, orders, fills, trades, ledger and stats, and the signals and shadow outcomes both books share.
- **Found while building it:**
  - **A waited trigger is priced from the feed, and R-016 allows 15 s.** The synthetic tape prints WIN at :01 and :50 of each minute, so a verdict that lands between those prints leaves WIN's price too old. That is why D-069 found the restart's re-served trigger could end as `risk_gate:R-016`. The test now times the re-vet to land 2 s after a print, and Book B enters. Real ORB names print far more often. A thinly traded name, though, would cost Book B entries that Book A, deciding on the trigger's own print, keeps. That is worth watching in the evaluation, not a change.
  - **No code changes.** P4.9 added tests and fixtures only. The mutant D-069 left for it (a restart re-vetting a signal Book B already decided) is now caught.
- **Spec updates:** ARCHITECTURE §12; PLAN; HANDOFF.
- **Tests:** 1,517 pass, up from 1,504: 14 new, and P4.8's A+B replay test folded into the golden's. Ruff, its formatter and mypy are clean. Book A's golden day and G3's replay pass unchanged.

---

## D-071 — P4.10: the post-close review's content, its follow-ups, and SPY's move in the shadow job
- **Date:** 2026-11-19 · **Status:** Accepted (the Phase 4 plan, P4.10 and decision 11; the "For P4.10" notes of D-063 and D-069).
- **`memory/reviews.py`** fills P4.8's review job (`DailyReviewWork`, the orchestrator's `ReviewWork`). One read of the journal gives the pack; the answer is stored in the transaction that completes the call, with the day's `review` step (decision 11).
- **The targets** (`select_targets`, S§6.5), from the day's top-20 signals and never a shadow short:
  1. every signal either book traded. One that Book A traded and Book B vetoed is a trade: the pack's signal list still shows the veto;
  2. every triggered long Book B vetoed. "Triggered" counts the SIP bars too: a long only SIP saw reach its trigger got no decision from Book B, and its veto is still reviewed;
  3. up to `notable_untraded` (5) other triggered longs, the largest |shadow R| first.

  At most 20, in that order, so a trim drops the notable ones first.
- **When a target was decided** (`decision_point`). Decision 11 says it is the `as_of` of the pack behind Book B's verdict. The journal doesn't keep a pack's `as_of`, so the review takes the `started_at` of that call's first attempt. The runner starts the attempt right after it encodes the pack and writes its input, so the two differ by milliseconds. The input artifact holds the exact time, but the journal's value replays the same and needs no file.
  - Book B's verdict is the one its decision rests on, else the day's latest, as in the report.
  - Without a verdict, the decision time is the live trigger, else the arming, else the SIP trigger minute.
- **News** comes from the journal's `news` table, with `received_at`, so an item counts from when the engine could read it (D-063). Each target gets the 3 items nearest before its decision, within 24 h of it (the longer of the brief's and the vet's windows), and the 2 nearest after.
- **SPY's move** (decision 11). On a review day the shadow job adds SPY to its SIP bars request (`compute_shadow(spy=...)`). Each triggered outcome then gets `shadow_outcomes.spy_move_pct`: SPY from the trigger minute's open to the exit minute's close.
  - Only on review days, so Book A's days and G3's replay request the same bars as before.
  - The goldens' `shadow` query now names its columns, so the new column leaves Book A's golden and G3's byte-identical. The A+B golden selects them all.
- **The rest of the pack:**
  - each book's result (`daily_stats`) and today's incidents;
  - the epoch's stats (`running_stats`): each book's trades, mean R and win rate; the triggered longs' shadow R; Book B's vetoes and the mean shadow R they turned down;
  - lessons, promoted first, then the newest proposals;
  - `history` (S§6.6): the last `history_days` summaries of reviews that came, that period's `bad_process` reviews, and the epoch's factor tally before today. The tally counts wins and losses by the sign of shadow R; a flat outcome is neither. Open issues and notes come last, newest first.

  A day with no signals has nothing to review, and no call is made.
- **Storing an answer** (`persist_review`; migration 0009):
  - `daily_reviews` keeps the targets asked about (`targets_json`, from the attempt's `candidates_json`, so a replay stores the same).
  - One `signal_reviews` row per target. A review of anything else, or a second one of the same target, is dropped, and repeated factors are kept once. The runner already drops non-targets (A§7.3); this is the store's own guard.
  - **Issues and notes:** one that links an open item bumps its count and last-seen date, and keeps its first text and severity. One review links an item once. An unknown id, or one that names a closed item, opens a new item. Every raising, new or linked, is a `review_mentions` row with that day's text.
  - Lesson proposals are `proposed` (amends P5.2), with `source_date` and ids `L-001` on.
- **A review that didn't come** (`record_missing`) is `invalid` when an answer came but didn't hold up: `invalid_json`, `invalid_envelope`, `no_structured_output`, `schema_invalid`, `wrong_pack_id` or `output_too_large`. Anything else is `missing`: a timeout, no time, a refusal. Either way, the row says why, with the attempt and its targets when there was one.
- **Follow-ups:**
  - `trader followups list [--all]` lists the oldest and most recurring first, and prints the model's text without control characters.
  - `trader followups set issue|note ID --status S [--response T]` takes an issue's `fixed` or `wontfix`, a note's `accepted`, `rejected` or `done`. Only the human closes them.
- **The report:**
  - a table of the issues and notes the review raised, new or recurring with its count, in today's words, escaped as the model's text;
  - a review that didn't come says why.
- **Wiring:**
  - run-day, `--sim` and `DayRun` pass `DailyReviewWork`.
  - Replay passes it only when `boot.json` lists the new capability `review_v1` (D-055). So a day recorded before P4.10 replays without a review, and without SPY in its shadow request.
  - A degraded Book B still gets its review, since the review covers Book A's day too. A Book B whose boot failed doesn't.
  - The job logs `review done` after the call, as the brief and the vet log theirs, for G4b's supervision. Its `no review today` lines say why: a day recorded without one, or no signals to review.
- **The smoke's review pack** (D-067):
  - Its books and stats now come from its own trades and shadow outcomes (`running_stats`).
  - Each book uses its two entries on the two traded targets by 09:38. So Book A's decision on the vetoed targets, and both books' on the notable ones, is `capacity_entries`, no longer `taken` or `chased`.
  - The smoke's input changes; it next runs at a gate.
- **The A+B golden day is rewritten,** as the plan allows in P4.10. It gains the review's call, its rows, SPY's moves and the `review` step; nothing else changed. Its replay reproduces them all. Book A's rows on it still equal Book A's own golden day's.
- **Spec updates:**
  - ARCHITECTURE §3 (16:12, 16:15), §4.8, §4.10 and §6;
  - STRATEGY §6.5;
  - CLAUDE.md's Commands (`followups`, approved with the Phase 4 plan);
  - PLAN; HANDOFF.
- **Tests:**
  - `test_reviews.py`: targets, decision times, news stamps, storing and linking, a missing or invalid review, history, stats, follow-ups and their CLI;
  - three whole-day review scenarios: an answer stored part by part, a timeout recorded as missing, and a restart that never asks again;
  - the report's raised items and missing review; the run-day wiring.
  - **30 mutations of the review's rules,** all caught. Six survived the first run and led to sharper tests: a veto that never triggered, notable signals whose rank and |R| disagree, an item out of the news window, `partly` knowable, the follow-ups' order, and a flat shadow R.
  - 1,534 tests pass, up from 1,517. Ruff, its formatter and mypy are clean. Book A's golden day and G3's replay pass unchanged.

---

## D-072 — G4b passed: a supervised A+B paper session, its replay, and what Claude's review found (partly answers Q2)
- **Date:** 2026-11-21 · **Status:** Accepted (G4b: the human ran `uv run trader run-day --books A,B` on 2026-11-20).
- **The session:** Friday 2026-11-20, on the human's commit `acea04f` (`boot.json`'s `git_sha`), with the capabilities `fees_v1`, `review_v1`, `shadow_tape` and `skew_hourly`.
  - **The day before** (HANDOFF): `trader news`, `trader filings` and `trader stream --news` worked. There are no `ALPACA_DATA_*` keys, so market data used Book B's. Doctor gave one WARN, `events`, for the quiet days before the 12-04 jobs report.
  - **The dry run** at 08:07 was as the runbook expected; run-day started at 08:37.
  - **Every milestone came on time:**
    - `book B ready`; `fees booked` (Book A: 10-12 to 10-14's 6 charges and 3 reversals, net $0.00); `news started` (1,764 articles backfilled);
    - the 09:00 scan (31 candidates of 1,853 symbols, 245 with news), and EDGAR answered for all 31;
    - `brief done` at 09:06:11; `subscribed` at 09:29; `scan done` at 09:35:04 (20 selected, 12 armed); `vet expected`, then `vet done`; one `trigger parked`;
    - flatten passes at 15:55 and 16:00, both books flat; EOD at 16:05, with a $0.00 residual in each book; 19 shadow outcomes at 16:12 (2 from the tape);
    - `review done` at 16:16:21; the report; DONE at 16:30.
  - **The log** has one warning, `foreign order` (below), and no errors. One P3 incident, `partial_fill_protected`; no P1 or P2.
- **Pass, on all five criteria:**
  1. the last line is `run-day 2026-11-20: DONE`;
  2. no P1;
  3. `doctor --quick` the next day: `alpaca.A` $990.11 and `alpaca.B` $994.82, each with 0 positions and 0 orders;
  4. the report has the LLM-calls line, the day mode, the calls table, the review's line and its targets table;
  5. `trader replay --date 2026-11-20` ends `the replay matches the recorded day`, after 3 min 49 s.

  Claude ran `doctor --quick` and the replay, both read-only, and checked the rest against the log, the journal and the report.
  - **Phase 4's "Done when" holds:**
    - each of the 3 attempts has its `llm_calls` row and both artifacts;
    - each of Book B's 10 decisions has its reason code and the call behind it;
    - the review stored a signal review for each of its 7 targets;
    - the replay reproduced the decisions, serving the recorded answers back.
- **The replay, beyond what it compares:** its review tables, shadow outcomes, daily stats, fills, ledger and verdicts equal the live journal's. Its timestamps differ by milliseconds, because its clock jumps between recorded instants. One value differs too:
  - **Book A's ROST limit was 216.12 live and 216.13 in the replay.**
  - **Why:** the trigger print (215.905) and four prints at 215.9 came in two frames, 0.5 ms apart. Live, the second frame was in before Book A priced its limit (`last 215.9`). The replay prices at the trigger print (`last 215.905`).
  - **Effect:** none on the decision or the fill (216). A replay can't reproduce scheduling that fine, so the golden fixture pins the replay's value.
- **The market:** SPY fell 3.51% (715.84 to 690.68), though it opened only 0.32% down. QQQ fell 5.5% and IWM 2.8%. The regime was down and high-vol.
- **The day's trading:**
  - **Signals:** 20 in all: 12 longs, every one armed; 7 shadow shorts; 1 with no direction.
  - **Outcomes:** 10 longs triggered on IEX, and every one's shadow stopped out (−1.01 to −1.21R), three after an MFE above 2R (KBH 4.76, GTX 3.55, DIVO 2.37). TW's shadow short made +3.36R.
  - **Entries:** both books took ROST and FNDA, their two entries for the day. They skipped the rest for capacity (R-006, R-007), and BSP for the chase guard.
  - **Results:** Book A lost $5.97 ($6.03 with the estimated fees); Book B lost $5.18 ($5.22).
  - **The difference was ROST's entry.** Book B's trigger waited 6.3 s for the vet, and its order filled at 215.59 against Book A's 216: $0.80 of the $0.81. The wait made it, not a veto.
- **Claude's calls,** with the settings' models, efforts and limits:

  | Role | Model, effort | Took (API) | Limit | Turns | Output tokens | Cost estimate | Answer |
  |---|---|---|---|---|---|---|---|
  | premarket | opus, high | 70.9 s (69.8) | 600 s; deadline 09:28 | 3 | 9,796 | $0.35 | 31 of 31; day mode normal; allow 1 (GAP 0.62), neutral 30 |
  | vet | sonnet, low | 8.4 s (7.5) | 60 s; Book B waits 60 s | 2 | 1,310 | $0.04 | 11 of 11; veto 1 (BILL 0.75), neutral 10 |
  | review | opus, high | 81.5 s (80.7) | 600 s; cutoff 16:29 | 3 | 9,046 | $0.36 | 7 of 7 targets; 7 issues, 2 notes, no lesson proposals |

  - **Clean:** no failures, repairs, timeouts, clamps or drops; 3 of 12 CLI runs. The calls cost about $0.75 at list prices, which the subscription counts toward its usage limits.
  - **The day mode:** normal. There was no scheduled event, and the risk-off tape was neither an extreme release-driven move nor a market-wide shock.
  - **The brief and the review used all 3 turns,** where G4a's calls used 2 (D-067).
    - So each needed the spare turn that `max_turns: 3` keeps, probably for the CLI's retry of an answer that failed the schema.
    - One more failed answer would have ended the call at the turn limit. A repair would then still have had time: the brief finished 22 minutes before its deadline, the review 13 before its cutoff.
    - **For the human:** `max_turns: 4` would keep a spare.
  - **The vet's timing:**
    - EDGAR's lookups for its 11 symbols took 3.3 s after arming, so the verdicts came 11.8 s after arming.
    - ROST triggered 5.5 s after arming and was parked for 6.3 s.
    - FNDA's trigger found its verdict ready.
  - **The verdicts were mostly neutral:** 40 of 42.
    - Most of the brief's candidates were leveraged and inverse ETFs (SQQQ, SOXS, SPXS, TZA, VXX and others), which a down tape puts at the top of the gappers.
    - STRATEGY §1 admits ETFs. **For the human:** should leveraged and inverse ETFs stay in the universe?
- **What the review raised** (`trader followups list`): 7 issues and 2 notes, all new. Claude's reading of each:
  1. *warn, execution: fills lagged triggers by 13–20 s.* **Alpaca paper's lag, not ours.**
     - The four entries at 09:35 sat in `pending_new` for 9.5–13.1 s (`entry accepted waited_s`), then filled 1.4–4 s later.
     - G3's 09:35 entry (FIGS, 2026-10-13) also took 13.6 s, while run 2's entries at 09:42 and 09:44 filled in about 1 s. It seems the paper broker is slow in the opening rush.
     - D-053's rule kept the 10 s `entry_timeout_s` from cancelling these orders: an acceptance restarts the timeout.
     - Live entries may not lag like this, so it belongs in Phase 7's limitations.
  2. *warn, shadow_sim: the shadow is optimistic about stop fills in fast moves.* **Valid.**
     - ROST's stop at 214.62 filled at 213.71 and 213.70 within a minute: −1.66R and −1.95R live, against −1.01R in the shadow.
     - The shadow fills a stop at the stop price, plus its slippage (D-057), and a stop the price gaps through fills worse.
     - Lessons tested on shadow R would carry this bias (note 2). For P5.1 and P5.2.
  3. *info, execution.A: ROST's partial fill went 0.7 s without a stop.* **By design:** D-053's exception, since an OTO's stop leg waits for the whole entry to fill.
  4. *info, book_b.vet: EMN's verdict was neutral, though its reason was against the long.* **Valid:** a point about the model's consistency. A prompt rule ("a reason against the long is a veto") would start a new epoch.
  5. *info, book_b.premarket: GAP's brief saw +3.46%, but it opened +7.26%, and nothing re-vetted it.* **By design** (`revet_premarket: false`), but a fair point. **For the human:** re-vet a premarket verdict when the open's gap is much larger?
  6. *info, accounting: the trades sum to −5.97, the book to −6.03.* **The pack's fault.** A trade's `pnl_usd` is gross, a book's is net of the estimated fees ($0.06 and $0.04), and the pack says neither. **Fix:** give each trade its fees.
  7. *info, stats: `b_vetoes` is 0, yet BILL was vetoed.* **The pack's fault.** The stat counts Book B's `vetoed` decisions, that is, vetoes of signals that triggered, and BILL triggered on neither feed. **Fix:** name the stat for what it counts.
  - *Note 1: stops at 0.1 ATR look tight, and `take_profit_r` is null.* These are the strategy's parameters (S§3, the paper's rules), and one day is no evidence. **For the human,** with Phase 7's data.
  - *Note 2: shadow R may understate stop losses,* as in issue 2.
  - **Claude's own find:** the review's summary says "SPY gapped −3.51%".
    - The pack's `market.spy_gap_pct` is the move from the previous close to the latest trade. At 09:05 that is a pre-market gap; at 16:15 it is the whole day's move. The open gap was −0.32%.
    - **Fix:** in the review's pack, label it as the day's change and add the open gaps beside it.
- **Other findings:**
  - **The `foreign order` (Book A, 09:35:09.650) was our own.**
    - It was the `stop_loss` leg of ROST's OTO entry. Alpaca's update for it arrived 2 ms after the submit's answer, before the entry's row, with its group id, was journaled.
    - `is_ours` found no match, so the service called it foreign and resynced; a later read recorded the leg under its parent.
    - Harmless but misleading. **Fix:** call an order foreign only after any submission in flight has been journaled.
  - **The PC clock drifted** from +0.098 s to +0.345 s during the session, about 35 ms an hour, against R§9's 2 s limit.
    - On 11-21, doctor read 0.16 s, so the Windows Time service is correcting it (D-067's 32,768 s poll).
    - That is fine for a day. For Phase 6's unattended weeks, the service must stay running.
  - **News:**
    - Every article came from `fiscal.ai`: 2,394 were stored that day (1,764 from the boot's backfill, 630 from the stream), 395 of them dated within the session.
    - The pre-check's "thin feed" (HANDOFF, 2026-11-19) was wrong: two minutes of the stream just happened to bring none.
    - D-028's "Benzinga" is now "one or more feeds, named by `source`", in Alpaca's docs' words. No code reads `source`.
  - **Q2, partly answered.**
    - The news socket ran beside the stock socket all day, as it had in `trader stream --news` the day before. So the free plan allows two market-data sockets.
    - The 09:35 subscription reached 15 symbols with trades and quotes, 30 subscriptions in all, with no 405. That is at the limit either way, so it doesn't show whether channels count toward the 30. Whether IEX sends `statuses` is still open too.
  - **Fees:** the boot booked 10-12 to 10-14's charges and reversals, net $0.00, as the runbook said. The day's estimates post overnight, and the next boot books them.
- **The golden replay,** as G3's run 2 became one (D-054):
  - **The fixture:** `tests/golden/recordings/2026-11-20/` holds the recording (4.1 MB): both brokers, `llm.jsonl.gz`, and the stream, trimmed as G3's is from 146 MB to 2.5 MB. Beside it are the session's config files and the live journal's comparable rows. `tests/fixtures/g4b_day.py` rebuilds it all.
  - **The test,** `test_the_g4b_day_replays_as_it_was_recorded`, replays the day in about 35 s. It checks:
    - there are no divergences, and the comparable rows equal the live ones;
    - a review is stored for each of the 7 targets;
    - the replay's journal matches the pin (`tests/golden/g4b_2026-11-20.json`), Book B's and the review's tables included.
- **A test that read the PC's date:** `test_the_cli_takes_no_llm` failed on Saturday, 2026-11-21.
  - **Why:** it runs `trader run-day --dry-run` through the CLI, which passes no clock, so `run_day` fell back to the PC's clock and found "not a trading day".
  - **Fix:** the `offline` fixture now patches that fallback to its own SimClock, as its other tests already pass theirs.
- **Next:**
  - Phase 4 is done. Phase 5 starts in plan mode.
  - Claude will propose this entry's fixes as its first tasks: the review pack's labels (the day's move, the trades' fees, the veto count) and the foreign-order race.
  - The human's questions above: `max_turns`; leveraged and inverse ETFs; re-vetting a premarket verdict; the stop's width.
- **Spec updates:** PLAN (G4b ticked; STATUS); HANDOFF; Q2, below.

---

## D-073 — P5.0a: the packs' market after the open, the review's money and veto counts, one standing-verdict rule, and max_turns 4
- **Date:** 2026-11-21 · **Status:** Accepted (the Phase 5 plan, approved 2026-11-21: decisions 1–3 and 5, and the human's choice of `max_turns: 4`; D-072's findings).
- **The market section** (pack version 3). `orchestrator/packs.py`'s `index_market`, a pure function, replaces `_index_gaps`.
  - **Before the open** (the brief): `{sym}_gap_pct` is the pre-market move, as before.
  - **From the open** (the vet, the review):
    - `{sym}_gap_pct` is the opening gap: today's daily bar's open against the previous session's close;
    - a new `{sym}_change_pct` is the change since that close, to the last regular-hours print, else to today's bar's close.
  - **Why a print can be skipped:** at 16:15 the latest trade can be an after-hours print. On G4b's day QQQ's was, at 611.98 against a 609.75 close.
  - **Whether the session has opened** is decided by the pack's time, not per symbol. So one pack never mixes a pre-market gap with an opening one.
  - **G4b's numbers:** SPY's review would have read gap −0.32 and change −3.51, not "gap" −3.51. The vet would have read gap −0.32 and change −0.43, where it read "gap" −0.43.
  - **The plan said `{sym}_move_pct`.** That name was taken: a review target's `spy_move_pct` is SPY over that target's window. So the market's key is `{sym}_change_pct`.
- **The review's money keys say what they hold.**
  - A trade's `pnl_usd` became `gross_pnl_usd`, beside a new `fees_est_usd` (`trades.fees_est`).
  - A book's `pnl_usd` became `net_pnl_usd`, which is `daily_stats.pnl`, after the estimated fees.
  - On G4b's day the model could then have matched −5.97 gross, less $0.06 of fees, to −6.03.
  - The smoke pack had built a net P&L into each trade. Its trades are gross now, and its books net of their fees.
- **The review's veto counts** (`stats`):
  - `b_vetoes` became `b_vetoed_triggers`: Book B's `vetoed` decisions, each a veto of a signal that triggered live. `b_vetoed_mean_shadow_r` is still their mean shadow R.
  - A new `b_veto_verdicts` counts the epoch's longs whose standing verdict is a veto, triggered or not: G4b's BILL.
  - P5.1 brings V and its interval to the same section.
- **One standing-verdict rule.** `memory/verdict_view.py` holds "the verdict Book B's decision rests on, else the day's latest". `reviews._b_verdicts` and `reports/daily._b_verdicts` each had their own copy; both now call it.
  - It works over a span of days, keyed by date and symbol, so P5.1's stats and P5.2's lessons can use it too.
  - A smoke run's verdicts never count.
- **`llm.max_turns: 4`** for every role, in `settings.yaml` (the human approved the edit) and the example. G4b's brief and review each used all 3 turns, and G4a's calls used 2.
- **The fingerprint changes.** `PACK_VERSION` and the `--max-turns` flag are both in it (D-065). So the next live boot raises a P3 `llm_fingerprint_changed` naming `pack_version` and the three roles' argv. That is expected in epoch 0.
- **What else changed:**
  - **The A+B golden's three `pack_id`s** changed (brief, vet, review), as the plan said, and nothing else in it did.
  - **Unchanged:** Book A's golden day, G3's replay and G4b's replay, which serves its recorded packs. So does the daily report: its verdict column reads the same through the shared rule.
- **Spec updates:**
  - ARCHITECTURE §2 (the layout), §4.8 (`verdict_view.py`) and §8 (the market, the money and the veto counts; version 3 in the example);
  - STRATEGY §2's news line, from D-072: the feeds that `source` names, not Benzinga;
  - PLAN: P5.0a ticked; STATUS.
- **Tests:**
  - `index_market` with G4b's numbers: at 16:15, an after-hours print gives way to the bar's close; at 09:35, the change runs to the latest print, and a pre-open print or an excluded condition falls back to the bar;
  - the vet's pack has the changes, and the brief's doesn't;
  - the pack's market keys, the money keys and the version;
  - the veto counts on BILL's case (a vet veto that never triggered), with a smoke run's veto ignored;
  - the standing rule over two days.
  - All 1,541 tests pass. Ruff, its formatter and mypy are clean.

---

## D-074 — P5.0b: a stop leg ahead of its entry's answer is not a foreign order (amends the plan's design)
- **Date:** 2026-11-21 · **Status:** Accepted (the Phase 5 plan's decision 4, amended here; D-072's finding).
- **The race (G4b):**
  - Alpaca sent ROST's OTO `held` stop leg on the stream 1 ms after the submit's REST answer, and before the entry's own event.
  - The service writes the entry's row before the POST, without a group id. The group id lands only when `_adopt` journals the answer, and the live journal's reader can run ahead of that write.
  - So `is_ours` missed the leg, logged `foreign order` and resynced. The resync then recorded the leg under its entry: harmless, but a false alarm.
- **The plan's design, tried and dropped:**
  - The plan had `_consume` wait for in-flight submits to land, then look the order up again. Built that way, it deadlocked the SimClock harness:
    - `_consume` waited on the clock;
    - the stream's queue behind it never drained;
    - and a driver (the test harness, `simdrive`) moves the clock only once the queues are idle.
  - A replay or simulated day could stick the same way. In live runs it would hold every later trade update behind the wait.
- **What was built instead.** `ExecutionService._unknown` classifies the unknown order and never waits.
  - **Our leg ahead of its answer:** a stop leg (`group_id`, and `group_role: stop_loss` or a sell stop) on the symbol of an entry still waiting for its answer (`order_id` not yet known). It is logged as `stop leg ahead of its entry's answer`, at info level.
  - **Anything else** is still a `foreign order` warning (D-037).
  - **Either way** the resync runs as before. It records our leg under its entry, and treats anything else as a difference: two looks, then a P1 halt (D-037). So nothing that protects the book changed; only the label did.
- **The replays.** G3's and G4b's replays and every golden are unchanged. In a replay the entry's answer lands before the leg's recorded event.
- **Tests** (SimBroker, threaded journal, `structlog.testing.capture_logs`):
  - **Alpaca's order:** the leg's event first, and the answer only after the service has looked the leg up. The result: no `foreign order`, one `stop leg ahead…` line, the leg under its entry, and no incident.
  - **Another symbol's** OTO placed by hand during our submit is still foreign.
  - **An unknown order with no entry waiting** is foreign.
  - **Two broken variants are caught:** no order counted as a stop leg, or any symbol accepted.
  - **Results:** 1,543 of 1,544 tests passed. The one failure, the journal's table count, came from P5.1's migration 0010, written while the run was going (D-075). Ruff, its formatter and mypy are clean.
- **Spec updates:** PLAN (P5.0b ticked; STATUS); `docs/PHASE5_PLAN.md`'s decision 4 stands amended by this entry.

---

## D-075 — P5.1: the metrics, the veto value and its population, intervals by resampled days, and the epoch record
- **Date:** 2026-11-22 · **Status:** Accepted (the Phase 5 plan: decisions 6, 7 and 9, and the human's choice to resample whole days; held items 2 and 3 of 2026-10-09).
- **`memory/stats.py`:** one read of the journal (`read_epoch`), then pure functions.
  - **Each book** (`book_metrics`):
    - trades, win rate (R > 0), mean R;
    - expectancy in dollars, gross and net of the estimated fees;
    - profit factor, on net P&L;
    - the days' net P&L;
    - max drawdown, from the first day's start equity through each day's end;
    - the daily Sharpe (sample standard deviation, × √252);
    - exposure: the share of session time with a position open, counting overlapping trades once and holding an unexited trade to the close;
    - mean entry slippage and delay;
    - **live R minus shadow R** per trade, which measures D-072's stop-fill bias before anyone changes the shadow rule.
  - **The veto value** (`veto_value`), in STRATEGY §7.3's words: the epoch's longs whose shadow triggered (a SIP-only trigger counts), with a shadow R and a standing verdict.
    - `allow` and `neutral` are allowed; `veto` is vetoed.
    - Triggered longs with no verdict are counted and left out.
    - Beside V and each side's n stands the vetoed side's own mean shadow R, with its interval (held item 3: V can be positive while the vetoes lose).
  - **Book B − Book A** (`b_minus_a`), their mean R per trade, with its interval: H2's number.
  - **SPY buy-and-hold** (`spy_buy_and_hold`), from `regime_daily`:
    - bought at the close before the books' first session, valued at the last close through their last;
    - `covered` says whether that close is the last session's. A Friday's arrives at Monday's boot. P5.4's report fills the gap.
- **The intervals** (`day_bootstrap`; the human's decision, 2026-11-21).
  - Each resample draws as many whole trading days as there are, with replacement, and adds up their sums and counts.
  - The statistic is a function of those totals. A resample where it is undefined (a side with no signal) is skipped and counted.
  - Percentile intervals at `lessons.ci_level` (90%), with `lessons.bootstrap_resamples` (10,000) resamples from `lessons.bootstrap_seed`. The draws use numpy's `default_rng`, so they don't depend on the hash seed.
  - Days are drawn one at a time, so memory stays at resamples × statistics.
- **The review's `stats`** gain V from `stats.py`: `v_r`, `v_ci_low`, `v_ci_high`, `v_n_allowed`, `v_n_vetoed` and `v_vetoed_mean_r`. The A+B golden's review `pack_id` changed again, and nothing else did.
- **The epoch record** (held item 2), in migration 0010:
  - **The `epochs` table:** epoch, start, reason, DECISIONS id, the note that led to it, and the hashes of the settings, risk and events files and of the LLM fingerprint.
    - Epoch 0's row says "the build and its shakedown, before P7.1's freeze", under this entry.
    - `lessons.status_reason` comes with it, for P5.2.
  - **`memory/epochs.py`:** `start_epoch` writes the row and bumps `meta.eval_epoch` in one transaction. It refuses:
    - any number but the next one;
    - a DECISIONS id that isn't one;
    - an empty reason;
    - an unknown note.
  - **The CLI:**
    - `trader epoch show` lists the epochs;
    - `trader epoch start N --decision D-xxx --reason "…" [--note ID]` is the human's. It holds `run-day.lock` while it writes, since run-day stamps every row with the epoch, so it refuses while a day runs.
  - `current_epoch` moved here from `reviews.py`.
- **Spec updates:**
  - STRATEGY §7.3 (the population, the intervals, the comparisons) and §9 (the record);
  - ARCHITECTURE §2, §4.8 (`stats.py`, `epochs.py`), §4.11 (`epoch`), §6 (migration 0010) and §8 (the review's V);
  - CLAUDE.md's Commands (`epoch`, as the plan said);
  - PLAN: P5.1 ticked; STATUS.
- **Tests** (`tests/unit/test_stats.py`; a multi-day fixture builder, `tests/fixtures/journal_days.py`, for P5.1–P5.5):
  - **the bootstrap:** seeded, with the skipped draws counted; wider by days than by signals when a day moves together;
  - **a book's numbers,** worked by hand, and a drawdown from the first day's start;
  - **V's population edges:** neutral, a SIP-only trigger, a veto that never triggered, no verdict, another epoch, a smoke run's verdict;
  - **the rest:** the review's V; Book B − Book A; SPY covered or not; an epoch summary from the journal; the epoch record and its CLI, the lock included.
  - **12 mutations, all caught:** neutral not allowed; exposure without the union; drawdown without the start; gross profit factor; a flipped live − shadow; undefined resamples kept; SPY always covered; B − A swapped; the epoch not bumped; a population SD in the Sharpe; a missing verdict counted as allowed; days ignored in resampling.
  - All 1,558 tests pass, so P5.0b's code passes too (D-074). Ruff, its formatter and mypy are clean.

---

## D-076 — P5.2: the lesson DSL's types, rows of pre-decision features, forward-only one-look promotion, and monthly demotion
- **Date:** 2026-11-22 · **Status:** Accepted (the Phase 5 plan: decisions 10 and 11, and the human's choice that lessons count only later signals; held item 1 of 2026-10-09).
- **`memory/lessons.py`.**
  - **The query** (`validate`): the schema's whitelist, plus a type for every field, written into STRATEGY §6.2. Any other key, a bool for a number, a non-finite number, an unknown value of a category, or 0 or 5 filters is refused, with the reason.
    - An import-time check makes sure every whitelisted field has exactly one type, so a field added to the schema can't slip through untyped.
    - Queries are applied in Python. No SQL is ever built from one.
  - **Numbers don't take `in`.** The plan gave them `in`, but the schema's `in` value is a list of strings. A range does the same job with `gte` and `lte`.
  - **The rows** (`read_features`): the epoch's triggered longs with a shadow R, each with the S§6.2 fields as the engine knew them before the outcome.
    - Book B's fields come from the standing verdict (D-073).
    - `price` is the trigger price, and `minutes_to_trigger` the whole minutes from the arming to the SIP trigger minute.
    - A field the engine didn't have is unknown, and a row is on neither side of a query that names one of its unknown fields: a long Book B never judged isn't "not vetoed".
  - **The test** (`judge`, written into STRATEGY §6.3):
    - only rows dated after the proposal's day count;
    - the effect is the subset's mean shadow R minus the complement's, with an interval from resampled days (D-075).
    - **One look:** a proposal waits until each side has `min_n_per_side` (20) rows, then passes once or fails once.
      - It passes if |effect| ≥ `min_effect_r` (0.10 R) and the whole interval lies on the claimed side of 0.
      - A rejected or demoted lesson is never judged again.
    - **The month's first run** (`meta.lessons_demotion_month`) re-tests promoted lessons on all their later rows, and demotes one whose interval now reaches 0. Other runs only refresh a promoted lesson's numbers.
- **Writing:** `evaluate_lessons(write=True)` stores each outcome in the lesson's row: status, `status_reason` (migration 0010), n's, effect, interval, and when it was evaluated, promoted or demoted. It also records the month's check, all in one transaction.
- **The CLI:**
  - `trader lessons evaluate` prints what each lesson would become and writes nothing;
  - `--promote` stores it, and is the Saturday job's (Phase 6);
  - `trader lessons list` shows every lesson, its statement made printable.
- **Spec updates:**
  - STRATEGY §6.2 (types and meanings) and §6.3 (how it is applied);
  - ARCHITECTURE §4.8 and §4.11;
  - CLAUDE.md's Commands;
  - PLAN: P5.2 ticked; STATUS.
- **Tests** (`tests/unit/test_lessons.py`, 37):
  - every type rule, and 25 refused queries: SQL in a field and in a value, a Cyrillic look-alike field, `shadow_r` (an outcome), a nested object, a bool, NaN, a string number, `in` with 0 or 11 values, `contains` on a category, an unknown flag, extra keys, 0 or 5 filters, a wrong metric, non-JSON;
  - the rows' fields, and unknown fields on neither side;
  - a planted effect promoting; only later signals counting; a null effect rejected and never looked at again; the claimed side; the 0.10 R floor; an invalid query rejected with its reason while its statement, an injection, is never obeyed;
  - the monthly re-test; nothing written without `--promote`; the CLI, the model's text made printable.
  - **12 mutations, all caught:** a bool as a number; no forward confirmation; rejected lessons looked at again; no effect floor; either direction; monthly always; monthly never; unknown counted as the complement; `in` unbounded; `contains` anywhere; a dry run that writes; minutes rounded up.
  - All 1,595 tests pass. Ruff, its formatter and mypy are clean.

---

## D-077 — P5.3: the playbook in every pack, the vet's matches (not the brief's), runtime/playbook.md, and the shared rendering
- **Date:** 2026-11-22 · **Status:** Accepted (the Phase 5 plan: decisions 12 and 13; amended below for the brief).
- **`memory/playbook.py`:**
  - **Which lessons:** `playbook` takes the promoted lessons that have their numbers, ranked by n × |effect| (n is the signals the lesson's conditions matched in its test), capped at `lessons.max_playbook_lessons` (15). A promoted lesson whose stored query no longer validates is skipped.
  - **In the packs:** `LivePacks` reads them at each pack (`Common.playbook`), so every role's `playbook` shows them in A§8's shape. The conditions code validated, enum values and numbers, sit outside `untrusted`; the statement, a model's, inside. No prompt changed: the prompts already describe both sections.
  - **The vet's matches** (`matching`, `vet_features`): a vet candidate's `matching_lessons` names the lessons whose conditions hold on its features known at arming: the gap, relative volume and rank, the opening range's numbers, the trigger price, ATR%, and the regime and calendar tags.
    - Conditions on Book B's own fields (catalyst, flags, verdict, its source, confidence) are the model's to apply, so code skips them.
    - A condition on any other field the candidate lacks, such as `minutes_to_trigger`, means no match, since code can't confirm it.
    - A lesson matches only if code checked at least one condition.
  - **`runtime/playbook.md`** is the human's copy, rewritten by `trader lessons evaluate --promote`. A statement is rendered as an inert code span.
- **Amended: the brief's candidates carry no `matching_lessons`.** The plan gave both roles matches, but before the open a candidate's `gap_pct` is the pre-market move, not the opening gap the lessons' rows hold.
  - A match on it would assert something code can't know, and the rest of the brief's features (ATR%, the tags) would rarely decide one.
  - The brief still reads the whole playbook with its conditions.
  - So the brief's pack is unchanged when no lesson is promoted, and the A+B golden doesn't move.
- **Pack version 3 stays** (plan decision 12): no live session has run since P5.0a.
- **`reports/render.py`** now holds what P5.4's plan moved there: STRATEGY §10's limitations, `ModelText`, `md_cell`, `model_md` and `md_row`, all taken out of `daily.py`. The playbook's Markdown needed `model_md`. The daily report's output is unchanged, and its tests pass as they were.
- **Spec updates:**
  - STRATEGY §6.4 (how the playbook reaches Claude);
  - ARCHITECTURE §2 (the stale `templates/`), §4.8, §4.10 (`render.py`) and §8 (`conditions`, and matches on vet candidates only);
  - PLAN: P5.3 ticked; STATUS.
- **Tests** (`tests/unit/test_playbook.py`):
  - the ranking, the cap, and what's left out (not promoted, no numbers, an invalid query);
  - the vet's matches (a model field skipped, an unknown field refusing, nothing checked refusing);
  - the vet's and the brief's packs through `LivePacks`: the playbook in both, the matches on the vet's alone, an injected statement under `untrusted`;
  - a full playbook within the pack's size;
  - the Markdown kept inert;
  - `lessons evaluate --promote` writing the file.
  - **5 mutations, all caught:** ranking by n alone; no cap; the model's fields treated as unknown; unknown fields ignored; a match with nothing checked.

---

## D-078 — P5.4: the weekly report, its equity curves, and SPY's missing closes
- **Date:** 2026-11-22 · **Status:** Accepted (the Phase 5 plan: decision 14; decisions 7–8, 13).
- **`reports/weekly.py`:** `build_weekly` reads one ISO week and the epoch to date in one journal read. It renders through `reports/render.py` (D-077):
  - **Curves:** equity for Book A, Book B and SPY, each indexed to 100 at the epoch's start (SPY at the close before its first session). The HTML draws them as an inline SVG, with no new dependency; the Markdown shows a table.
  - **Each book,** the week's and the epoch's: `memory/stats.py`'s metrics (D-075), live R − shadow R and its n among them.
  - **Book B's vetoes,** for the week and the epoch: V with its interval, each side's n, the vetoed side's own mean shadow R with its interval, the triggered longs with no verdict, and Book B − Book A with its interval.
  - **Lessons:** the count by status, and the promoted ones.
  - **The factor tally** to the week's end, with S§6.5's caveat that factors are hindsight labels.
  - **The rest:** the week's `bad_process` reviews; the open issues and notes, oldest and most recurring first; the week's incidents; each epoch's record with Book B − Book A in it; and the limitations footer.
  - **Model text stays inert:** in the Markdown as a code span with its pipes escaped, and in the HTML escaped as every cell is. That covers Book B's notes, the issues and notes, the incidents' messages and the lessons' statements.
- **SPY's missing closes** (decision 8):
  - A session's `regime_daily` row, which holds SPY's close, is written at the next session's boot, so on a Saturday Friday's isn't there yet.
  - `trader report weekly` first runs the boot job's own `ensure_regime` for each of the week's sessions without one: one SIP daily-bars request each.
  - With `--offline`, or when a request fails, the report says how far SPY's closes reach instead of showing a partial return as whole.
- **`trader report weekly [--week YYYY-Www] [--offline]`** defaults to the last complete ISO week: this week from Saturday, otherwise the one before. It writes `weekly-YYYY-WW.{md,html}` beside the daily reports. The Saturday job that runs it is P6.1's.
- **Spec updates:**
  - ARCHITECTURE §4.10 (the report's contents) and §4.11 (the command);
  - CLAUDE.md's Commands;
  - PLAN: P5.4 ticked; STATUS.
- **Tests** (`tests/unit/test_weekly.py`, on a two-week journal):
  - the weeks, across a year;
  - everything a week gathers, its incidents only, and SPY's last close missing;
  - a week without Book B, and an empty week;
  - the model's text inert in both formats;
  - the SVG: no line for a series with no points, a single session;
  - the files' names;
  - SPY's missing closes filled through `ensure_regime` with a fake market, and left missing in an outage;
  - the command, and a malformed `--week`.

  The daily report's tests pass unchanged after `render.py` took its helpers.

---

## D-079 — P5.5: the evaluation memo, S§8's inconclusive row, and how each line is computed
- **Date:** 2026-11-22 · **Status:** Accepted (the Phase 5 plan: decision 15; the human's decision 3 of 2026-11-21 pre-registered the inconclusive row). One reading, the "keep running" of outcome 2 below, is for the human to confirm or overrule.
- **`reports/evaluation.py`:** `build_memo` judges one epoch, through a given session, by STRATEGY §8 as registered. Each line is pass, fail or insufficient data, with its numbers.
  - **The minimum data:**
    - trading days are the epoch's sessions with a `daily_stats` row;
    - triggered longs are the epoch's longs whose shadow triggered, including a trigger seen on SIP bars alone (D-075's population). The research-only shadow shorts never count: no book can trade them and no verdict covers them. §8 says "triggered signals", and this reading is now written there;
    - vetoes are triggered longs with a standing veto, H1's population. So the inconclusive row's "H1 has fewer than 20 vetoes" is part of "the minimum data isn't reached", and the code checks it once.
  - **The hypotheses:**
    - H1 passes when V's interval lies above 0.
    - H2 passes when Book B's mean R per trade minus Book A's is at least 0. §8 states H2 as "≥", so the estimate decides; its interval is shown beside it.
    - H3: on Book B's `reduced` and `no_trade` days (its `day_modes` row), the triggered longs' mean shadow R minus that on its `normal` days must have an interval below 0.
    - H4: ranks 1–5 minus ranks 16–20 must have an interval above 0. A Spearman correlation of rank against shadow R is shown beside it, ties averaged, computed with numpy (no scipy).
    - H1, H2 and criteria 1–3 read insufficient data until the minimum data is met. H3 and H4, which are secondary, need `min_n_per_side` (20) longs on each side instead.
  - **The criteria:**
    1. V's interval's lower bound is above 0.
    2. Book B's trades' mean R after fees is above 0, each trade's being (P&L − estimated fees) / (qty × (entry − stop)).
    3. Book B's max drawdown, from `daily_stats`, is at most 15%.
    4. Insufficient data until Phase 6 marks runs as unattended. Meanwhile it shows the longest run of sessions with no P1 incident.
    5. Always "for the human".
  - **The outcome,** in this order (written into §8):
    1. inconclusive, as the row says, from the 90th trading day;
    2. keep running while the minimum data isn't reached, or while V's interval spans 0 by more than 0.5 R. This is the reading for the human to confirm: the row calls that case inconclusive at 90 days, so an earlier look shouldn't call it a fail;
    3. both books negative, or H1 fails with Book A positive, by each book's net P&L;
    4. the criteria code can check pass, when criteria 1–3 do, and the go/no-go stays the human's (G7);
    5. criteria not met.
- **`trader report evaluation [--epoch N] [--through YYYY-MM-DD]`** judges the current epoch through today by default. It writes `evaluation-epoch-N.{md,html}` beside the other reports and prints the outcome with its counts. It is offline: it reads only the journal. A malformed `--through` gets a usage line.
- **An epoch-0 memo says it is a dry run.** Epoch 0 is the build and its shakedown (S§9), and some of its data, G3's and G4b's days, predates the inconclusive row. So the memo claims only that its rules were pre-registered, not that they came before the epoch's data.
- **On G4b's real journal:** 1 trading day, 11 triggered longs (10 live, and VNT's on SIP bars alone) and 0 vetoes, so "keep running". There is no V, since BILL's veto never triggered (D-072). Claude's review counted 10 triggered longs, the live ones.
  - The G4b golden test now renders the weekly report and the memo from its replay's journal, as P5.4's plan said it would. Until now the reports were tested only on hand-written rows.
- **Spec updates:**
  - STRATEGY §8: the inconclusive row as the human approved it, "How the memo computes each line", and which longs count;
  - ARCHITECTURE §4.10 and §4.11;
  - CLAUDE.md's Commands;
  - PLAN: P5.5 ticked; STATUS.
- **Tests** (`tests/unit/test_evaluation.py`, 25; and the G4b golden test):
  - the minimum data exactly at 60 days, 150 longs and 20 vetoes, and one short of each;
  - each hypothesis's and criterion's pass, fail and insufficient data, edges included:
    - a V of exactly 0 fails;
    - Book B level with Book A passes H2;
    - a drawdown of exactly 15% passes;
    - H3 and H4 fail when their estimate points the predicted way but their interval reaches 0;
  - H4's buckets: ranks 5 and 20 are in, ranks 6 and 15 out;
  - every outcome:
    - keep running, for each reason;
    - the criteria code can check pass;
    - H1 fails, Book A positive;
    - criteria not met, including H1 failing while Book A is down;
    - both books negative;
    - inconclusive for each trigger, at exactly 90 days, while 89 days keeps running;
    - a narrow interval around 0 at 90 days, which is an answer (H1 fails), not inconclusive;
  - a P1 breaking the run of sessions; Spearman's ties; "n/a" where nothing can be computed; both renderings; the command, and a bad date.
  - **30 mutations, all caught.** Each rewrites one site of the module's source and re-executes it:
    - the minimum data: `>` for `≥`; `any` for `all`; H1 not waiting for it;
    - H1 by `≥ 0`, and by its upper bound;
    - H2 strict, and by its interval;
    - H3 by the wrong bound, without `no_trade`, and its n by `≤`;
    - H4: rank 6 in; rank 15 in; ranks 5 and 20 out; the wrong bound; its n by `≤`;
    - Spearman's ties not averaged;
    - criterion 1 by `≥ 0`; criterion 2 adding the fees, ignoring them, or counting Book A's trades; criterion 3 strict; criterion 4 passing on a P1-free run; a P1 ignored;
    - the outcome: 90 days strict; the width ignored; a wide interval not inconclusive; a wide interval not kept running; both books negative when either is; H1 failing whatever Book A did; the criteria passing when any does.
  - **Three survived the first pass,** and each gained a test. Every interval in the tests was a single point, so judging H3 or H4 by the wrong end changed nothing. And no test had H1 failing while Book A was down.
  - **All 1,635 tests pass,** P5.4's (D-078) included; P5.3's run had passed 1,601 (D-077). Ruff, its formatter and mypy are clean.

---

## D-080 — P6.1a: Windows Task Scheduler under S4U, the scheduled-job wrapper, `trader schedule`, and `trader weekly-job`
- **Date:** 2026-11-23 · **Status:** Accepted. The Phase 6 plan (`docs/PHASE6_PLAN.md`, decisions 1–5) was approved the same day, with the human's four decisions.
- **The human's Phase 6 decisions (2026-11-23):**
  1. **The watchdog runs every minute,** 09:25–16:05 ET, where RISK §6 said every 5 min.
     - A dead engine is noticed within about 2.5 min, not 6.
     - The runs at 15:58 and 15:59 can still sell for the late check (close − 2 min).
     - RISK §6's text changes with P6.2a.
  2. **Task Scheduler runs the tasks under S4U,** whether or not anyone is logged on.
     - Microsoft's docs say S4U has "no access to the network or encrypted files". We expect that to mean network logons, not outbound HTTPS, and `trader schedule check` proves it at G6a.
     - `--logon interactive` is the fallback.
  3. **Windows only:** no cron or launchd until the bot moves.
  4. **Retention keeps recordings and LLM artifacts through the current epoch** (P6.3).

  Approving the plan also approved its computation of S§8 criterion 4 (P6.1b).
- **`src/trader/ops/` replaces A§2's `scripts/install_scheduler_{cron,launchd,windows}.py`.** Code under `src/` is type-checked and tested, and the CLI is the one entry point.
- **The tasks** (`ops/scheduler.py`, in the folder `\ai-day-trader\`), in ET:

  | Task | Trigger | Command | Settings |
  |---|---|---|---|
  | `run-day` | Mon–Fri at `schedule.boot` − 5 min (08:25) | `run-day --books A,B --scheduled` | 10 h limit, wakes the PC, priority 4 |
  | `watchdog` | Mon–Fri 09:25, repeated PT1M for PT6H40M (through 16:05) | `watchdog` | 4 min limit, wakes the PC, priority 4 |
  | `weekly-job` | Sat 10:00 | `weekly-job` | 1 h, priority 7 |
  | `universe` (with `--with-universe` only) | Mon–Fri 20:00 | `universe build` | 1 h, priority 7 |

  - **Every task:**
    - IgnoreNew;
    - StartWhenAvailable;
    - DisallowStartIfOnBatteries and StopIfGoingOnBatteries false (both default to true);
    - RunOnlyIfNetworkAvailable false;
    - LeastPrivilege.
  - **Priority:** 4 is Windows' normal process priority. Task Scheduler's default of 7 is below normal, which is too low for the engine and the watchdog.
  - **Each job reads the trading calendar itself,** so holidays and early closes need no entries:
    - run-day exits on a holiday;
    - the watchdog (P6.2a) checks its window against the day's session.
  - **Time zones.**
    - Task Scheduler fires on the local wall clock, so a `StartBoundary` without an offset follows DST.
    - Install accepts the Windows zones that change their clocks on US Eastern's dates (Eastern, Central, Mountain, Pacific, Alaskan), shifted by their constant offset.
    - It refuses any other zone, and a zone with DST switched off, which `tzutil /g` reports as `…_dstoff`.
    - This PC is on Eastern time.
  - **The first trigger is the next day,** so nothing fires on the day of installing.
- **Checked against Task Scheduler itself.** I used the COM API's validate-only flag (`TASK_VALIDATE_ONLY` = 0x1), which registers nothing; the folder was checked afterwards.
  - All ten definitions are valid: five tasks (the probe included), under both logons.
  - A priority of 42, a PT30S interval and an unknown element are each rejected, so the check isn't vacuous. This also confirms the 1-minute minimum.
  - **What the tools write when redirected:**
    - schtasks writes the console's code page with `\r\r\n` line ends, and a missing task exits 1. So tasks are read back with `/Query /XML`, which isn't localized, and compared field by field. An element left out counts as Task Scheduler's default.
    - `tzutil /g` printed `Eastern Standard Time`.
- **The wrapper** (`ops/scheduled.py`, `python -m trader.ops.scheduled --root <root> <command>`).
  - **Output goes to the log first:** file descriptors 1 and 2, Windows' standard handles and `sys.stdout`/`stderr` all go to `runtime/logs/scheduled/<job>-<date>.log`.
    - This happens before trader's CLI is imported, because `configure_logging` binds `sys.stderr` when its module loads.
    - A test asserts that importing the wrapper loads neither module.
    - The log gets a start line (pid, arguments) and an `exit N` line.
  - **pythonw** (the interactive fallback) first gets a hidden console. Console children then don't each open a window, and CTRL_BREAK can still reach them.
  - **Typer 0.27 vendors click.** Without standalone mode it returns an `Exit`'s code instead of raising it. `cli._run` turns most failures into `Exit(1)`, so a failure here is:
    - a non-zero code;
    - an exception;
    - `typer.Abort`;
    - or a `TyperException`, such as a usage error (code 2).
  - **A failure is journaled once per job per day** as a P2 `scheduled_job_failed` (component `scheduler`).
    - The message names the log and gives its last error line, redacted.
    - It is pushed at once, unless a fresh heartbeat shows an engine running. That engine's pump then sends it, so two pumps never race.
  - **run-day's special cases:**
    - A session already DONE, or another engine holding the run lock, is a quiet skip.
    - A failure before BOOT is retried every 2 min until 09:20 ET.
    - A failure after BOOT isn't retried: a crashed engine isn't restarted automatically.
- **`orchestrator/heartbeat.py`** is the plan's decision 12, built now for the wrapper; the watchdog uses it in P6.2a.
  - A read is tried three times, 200 ms apart, which tells absent from unreadable.
  - A timestamp up to 5 s in the future counts as fresh; further ahead counts as unreadable.
  - It is stale after 60 s.
- **`trader schedule`** (`ops/schedule_cmds.py`):
  - **`show`** changes nothing. It prints:
    - the tasks in ET, and on the PC's clock when that differs;
    - the command lines and the logon;
    - whether each task is installed as shown;
    - every reason install would refuse.
  - **`install` (G6a) refuses** when:
    - the zone can't be followed;
    - notifications are off: `notify.provider: none`, or its secret missing in `.env`. A P1 at night must reach someone (H0.5);
    - the virtual environment is missing.

    Otherwise it prints everything, asks for `INSTALL`, then:
    - writes `runtime/schedule/<task>.xml` in UTF-16, as Task Scheduler exports;
    - registers each task with `schtasks /Create /XML /F`;
    - removes a task an earlier install made and this one doesn't;
    - records `runtime/schedule.json`: the logon, the account, the zone, and each task's hash.
    - A refusal from schtasks stops it, with the hint to run elevated.
  - **`uninstall`** deletes only the tasks it knows by name, after a yes.
  - **`check` (G6a):**
    - registers a one-off `\ai-day-trader\probe` with the installed logon and the same action, and starts it;
    - waits up to 3 min for its log's `exit` line, then prints the log;
    - always deletes the task.

    The probe runs `trader schedule probe`: doctor, then a test push.
- **`trader weekly-job`** runs `lessons evaluate --promote`, then `report weekly`. Each runs even if the other fails, and the job exits 1 if either did.
- **`run-day --scheduled`** (hidden) is what the run-day task passes. P6.1b journals it.
- **Tests:**
  - `tests/unit/test_scheduler.py` (50):
    - the entries;
    - the zones;
    - the watchdog's XML, golden;
    - quoting;
    - escaping;
    - the probe;
    - fingerprints with Task Scheduler's defaults;
    - schtasks' command lines;
    - decoding;
    - show, install's refusals and its run, reinstalling, uninstall, and check (passing, failing, silent, unable to start);
    - the CLI.
  - `tests/unit/test_scheduled.py` (24):
    - the run's rules;
    - the CLI in-process;
    - run-day's state;
    - the alert: once a day, redacted, and left to a running engine to pump;
    - the real process: imports, a job into its log, a failure journaled once.
  - conftest now refuses any real schtasks, tzutil or powercfg, as it refuses the real claude.
- **Spec updates:**
  - ARCHITECTURE: §1 (every minute; the wrapper), §2 (`ops/`; `scripts/`), §4.11 and §11;
  - CLAUDE.md's Commands;
  - PLAN: Phase 6 rewritten per the plan, P6.1a ticked, and STATUS.
- **Verified:** all 1,709 tests pass (1,635 before P6.1a); ruff, its formatter and mypy are clean.

## D-081 — P6.1b: unattended sessions and S§8 criterion 4, keeping the PC awake, and doctor's sleep and notification checks
- **Date:** 2026-11-23 · **Status:** Accepted (the Phase 6 plan's decisions 6–10). §8 is pre-registered, and the human approved criterion 4's computation with the plan.
- **What the journal now records:**
  - **`run-day --scheduled`** (the task's command, D-080) writes a `day_runs` note `scheduled` holding the books asked for.
  - **These commands each write a `day_runs` row** of kind `human`, named for the command and dated the day it ran: a run-day started by hand, `trader halt`, `trader flatten`, `trader resume` (only once it lifts a halt) and `trader smoke paper` (even one refused for the day).
  - **Neither reaches a replay:** `live.run_day`'s paper path and the CLI write them, never `DayOrchestrator`. A session already DONE gets neither: run-day then does nothing, so it touches nothing.
- **`memory/unattended.py`:**
  - **A session is unattended when all of these hold:**
    - the `scheduled` note is there;
    - `run_status` is DONE;
    - there was exactly one BOOT (no restart);
    - there is no `human` row;
    - no `kill_switch` halt was created (the KILL file is a person's act);
    - there is no `scheduled_job_failed` for run-day;
    - both books have `daily_stats` rows, and there is no `book_b_off` note.
  - **Weekends and holidays:** a `human` row, a kill-switch halt or a P1 dated on one counts against the next session.
  - **`session_runs`** gives each session's verdict and the first reason it fails, with its P1 count.
  - **`longest_clean_run`** gives the most consecutive sessions that were unattended and free of P1s.
  - **`epoch_sessions`** gives the epoch's calendar sessions (`trading_days`), from its first with `daily_stats` through the given day. A session that never ran is in it, so it breaks the streak.
  - **Strict by design:** a run-day retried after a failure isn't unattended, though no one stepped in. Criterion 4 measures whether the system runs without help.
- **S§8 criterion 4:**
  - **It passes** when some 20 consecutive sessions in the epoch are clean.
  - **Otherwise** it reads insufficient data while the epoch has fewer than 60 sessions, and fail after that. The P1-only count from P5.5 (D-079) is gone.
  - **Its numbers:** "longest unattended run without a P1: N sessions; U of S sessions unattended".
  - **The outcome:** "the criteria code can check pass" now needs criteria 1–4, and its next step reads "Criteria 1-4 pass; the go/no-go is the human's (G7)".
- **Where it shows:**
  - the weekly report's Operations table: each session of the week, unattended or why not, and its P1s;
  - the epoch's longest clean run, among the report's notes;
  - `trader schedule show`'s last five sessions, read with sqlite3 alone, since an engine may hold the journal.
- **Keeping the PC awake** (`ops/power.py`, RISK §9):
  - `keep_awake()` wraps `run-day`'s day, paper or sim but not a dry run. It calls `SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)` and clears it with `ES_CONTINUOUS` alone, even when the day raises. Nothing is cleared when Windows refuses.
  - **Microsoft's docs:**
    - the flags are 0x80000000 and 0x1;
    - it returns NULL on failure;
    - it "cannot be used to prevent the user from putting the computer to sleep" (the lid, the power button).
  - This PC sleeps in S3, per `powercfg /a`. Modern Standby, which the docs don't cover, isn't a concern here.
- **Doctor's two new checks:**
  - **`machine.sleep`** reads the active scheme's AC standby timeout and wake-timer setting through powrprof: `PowerGetActiveScheme`, then `PowerReadACValueIndex` into an `LPDWORD`, then `LocalFree`, all signatures from Microsoft's docs.
    - The GUIDs match `powercfg /aliases`: SUB_SLEEP, STANDBYIDLE and RTCWAKE.
    - On this PC it read standby 0 (never) and wake timers 1 (on), as `powercfg /query` shows.
    - It passes when the PC never sleeps on AC power, or when wake timers are on, so the 08:25 task can wake it. It warns otherwise, and when the settings can't be read.
  - **`notify`** warns while `notify.provider` is `none` or its secret is missing.
- **Tests:**
  - `test_unattended.py` (15): every disqualifier, another job's failure, a P1, weekend events, a session with no run, the epoch's sessions, `note_human`;
  - `test_power.py` (4);
  - `test_doctor.py`: `machine.sleep`'s five cases and `notify`'s three. The `green` fixture now has ntfy, so "everything green" includes both;
  - `test_evaluation.py`: criterion 4's pass, insufficient data and fail; the outcome needing 1–4; a session with no run;
  - who started the session, and keep-awake around the day (`test_run_day_cli.py`);
  - the CLI's `human` rows (`test_cli_kill.py`, `test_smoke.py`);
  - the weekly Operations table;
  - `schedule show`'s recent sessions.
  - conftest now stubs `set_execution_state` for every test (`power_calls`).
- **Spec updates:**
  - STRATEGY §8: criterion 4 and the outcome's criteria 1–4;
  - RISK §9;
  - ARCHITECTURE: §2, §4.10 (the weekly report, unattended sessions), §4.11 (doctor);
  - PLAN: P6.1b ticked; STATUS.
- **Verified:** all 1,742 tests pass (1,709 after P6.1a); ruff, its formatter and mypy are clean.

## D-082 — P6.2a: the watchdog (every minute), its rules, and the shared flatten
- **Date:** 2026-11-23 · **Status:** Accepted (the Phase 6 plan's decisions 11–14). RISK §6 is rewritten to match; its every-minute schedule is the human's decision of 2026-11-23 (D-080).
- **`orchestrator/watchdog.py`, one run per process:**
  - **The window:** a trading day from the open − 5 min to the close + 5 min, early closes included. Outside it, or on a holiday, a run exits without a broker call.
  - **The heartbeat** (`orchestrator/heartbeat.py`, D-080) is fresh while younger than 60 s.
    - One that is absent, unreadable or stale is read again 25 s later before anything is done. A beat can be skipped, a read can collide with the engine's replace on Windows, and the clock can step.
    - A heartbeat that comes back inside those 25 s is left alone.
  - **Coverage is the engine's own code.**
    - `service.stop_works` is the engine's R-020 rule, moved out of the service unchanged. `exit_at_work` and `overnight_stop` are now public too.
    - Shares are covered by working stops or a market sell at work. A held leg counts only once its entry is final.
    - After the close, only a working GTC sell stop covers them.
    - A short is never the watchdog's: R-002, and reconcile reports it.
  - **The rules** (`assess`, pure; RISK §6):
    - **Heartbeat stale:**
      - in the session, each book with anything open (positions or orders) is flattened, with a P1 `engine_down_flattened`; with nothing open, a P2 `engine_down`;
      - before the open, a P2, and bare shares get GTC stops (P1);
      - after the close, shares without a GTC stop get one (P1 `watchdog_overnight`), else a P2, since the day's EOD waits.
    - **Heartbeat fresh, before close − 2 min:**
      - bare shares in a book the engine runs get `partial_fill_cancel_max_s` + 10 s (30 s), then a fresh read, then a flatten (P1 `watchdog_unprotected`);
      - this stands aside within 40 s of the flatten time, while the engine says it is flattening the book, and under someone's `flattening` marker;
      - a book missing from the heartbeat (no engine runs it) is flattened without waiting;
      - before the open, GTC stops are placed instead of selling.
    - **From close − 2 min, either way:** every book holding shares is flattened (P1 `watchdog_late`). There is no P1 when `not_flat` was already raised for the book that day, but the flatten runs anyway: two flatteners are benign (below).
    - **The KILL file with the heartbeat stale:** every open book is flattened under a `kill_switch` halt (P2 `watchdog_kill_file`).
  - **Alerts:**
    - Each incident (component `watchdog`) carries a `key` in `detail_json`. One with a key already journaled today isn't journaled again.
    - The keys are an engine-down episode (the stale heartbeat's timestamp) per book, or a book per day. A P1 for an episode stops that episode's P2.
    - The watchdog pumps alerts only while the heartbeat is stale, so it never races a live engine's pump.
    - A broker that can't be read is a P2 `watchdog_blind`, once per book per day. The other book is still checked.
  - **`runtime/watchdog.json`** records each run: the verdict, the phase, the heartbeat, the books, the actions and how each flatten ended.
- **`execution/flatten.py`, `flatten_books`,** shared by `trader flatten`, `trader halt --flatten` and the watchdog:
  - it halts each book with the caller's reason (`manual`, `p1_incident`, `kill_switch`);
  - it renews the `flattening` marker on every pass and clears it at the end;
  - it flattens the books concurrently;
  - a book whose flatten raised maps to None and is reported;
  - first it settles any sell still `submitting` or `uncertain` 30 s after it was sent: adopted if the broker has it, marked lost if not. Otherwise a flatten killed mid-submit (the watchdog's 4-minute limit) would leave a row the engine's reconcile skips as in flight all day.
  - `trader flatten`'s output and its `not_flat` P1 are unchanged.
- **`trader watchdog`:**
  - **Its own log file,** `runtime/logs/watchdog-<date>.jsonl`, through `configure_logging`'s new `prefix`. About 390 runs a day appending to the engine's file from another process could tear its lines.
  - **Its own OS lock,** `runtime/watchdog.lock` (`RunLock`'s message names its holder now). A run that finds it held stands down.
  - **The calendar** comes from the journal, synced from Alpaca (one request) only when it doesn't cover today.
  - **The books** are each book with Alpaca keys.
  - **It prints one line** for the scheduled log.
- **Two flatteners at once are benign,** as the plan review found and the tests confirm:
  - cancels are quiet;
  - a market sell at work is never cancelled;
  - a second sell of the same shares is rejected for insufficient quantity (D-033), and its row is marked rejected;
  - fills and ledger rows are booked once.
- **A known limitation** (the plan's "not in this plan"): a halt set by another process doesn't set the engine's `_closing`. An entry whose submit lands after the watchdog's cancel pass can rest; R-020 then protects it, and the late check flattens it.
- **Tests** (`tests/unit/test_watchdog.py`, 42):
  - the window's edges, and an early close;
  - coverage, case by case;
  - every rule above;
  - on SimBroker:
    - a dead engine's book flattened and alerted once, with fills booked once and the marker cleared;
    - one P2 per episode, and a new one when the engine dies again;
    - a P1 stopping its episode's P2;
    - a heartbeat back within 25 s;
    - bare shares flattened after the wait, or left when covered during it;
    - a book no engine runs flattened without waiting;
    - the late check, with and without the engine's `not_flat`;
    - GTC stops after the close;
    - the KILL file;
    - a blind broker;
    - a stuck sell settled;
  - beside a live ExecutionService on the same broker and journal:
    - the watchdog flattening while the service runs: no stop after the marker, no mismatch, one sale;
    - both flattening at once: one sale, at most one refused, one ledger row;
  - the command: its own log, and its lock.
  - The watchdog tests use the journal inline: `run_on_clock` moves time as soon as anything waits on the clock, and a second coroutine's write on the journal's thread could still be pending (D-047).
- **Spec updates:**
  - RISK §6 (rewritten) and §11 (the codes);
  - ARCHITECTURE §2, §4.9 and §4.11;
  - CLAUDE.md's Commands;
  - PLAN: P6.2a ticked; STATUS.
- **Verified:** all 1,784 tests pass (1,742 after P6.1b); ruff, its formatter and mypy are clean. The two tests beside a live engine passed five runs in a row.

## D-083 — P6.2b: the engine's side: the heartbeat's pid and flattening, watchdog_silent, claude children in a job object, smoke paper --hold, and the review's fixes
- **Date:** 2026-11-24 · **Status:** Accepted (the Phase 6 plan's decisions 15–18).
- **The heartbeat gains two fields:**
  - **`pid`:** the engine's interpreter, the fire drill's `Stop-Process` target. uv's launcher is its parent and dies with it.
  - **Per book, `flattening`:** the new `service.flattening`, true while a flatten pass works on the book.
  - **Why:** the engine's own flattens set no `flattening` marker (only `trader flatten` and the watchdog do). Without the flag, the watchdog's stop check would catch the engine's 15:55 flatten mid-cancel, raise a false P1 and become a second flattener.
  - Replays write the heartbeat too, but no golden snapshots it.
- **The heartbeat is also written once as BOOT begins** (`tasks.boot_beat`), with each book unhalted, holding nothing and not flattening.
  - **Why:** the 10 s heartbeat starts only once every book has started. Until then, a restart mid-session looked dead to the watchdog. Its flatten and `engine_down` P1 would then hit books the new engine was adopting.
  - The review below found this.
- **The engine watches the watchdog back.**
  - With the schedule installed, a watchdog that hasn't run for 10 minutes between open + 10 min and the close is a P2 `watchdog_silent`, once per silence. "Installed" means `runtime/schedule.json` lists `\ai-day-trader\watchdog`.
  - The engine reads `runtime/watchdog.json`'s `ts` once a minute. A file that is missing or half-written reads as "never".
  - **Where it runs:** `DayDeps.watchdog_status` is set on the live paper path only, and only after G6a. It is None in replays, sims, dry runs and tests, so their journals never change.
- **`claude` children join a kill-on-close job object** (`ops/containment.py`).
  - **The problem:** uv's launcher lets the interpreter's own children break away from its job (the plan review read uv's source). So a killed engine (the fire drill, or Task Scheduler's time limit) left its `claude` call running, which could still write into the sandbox the next call needs empty.
  - **The fix:** each `ClaudeCliTransport` holds a job object with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` and assigns each child to it as it starts. The handle isn't inheritable, so when the engine's process ends, however it ends, Windows closes it and the children die.
  - **Checked against Microsoft's docs:**
    - `CreateJobObjectW` returns NULL on failure;
    - `SetInformationJobObject` takes class 9, `JobObjectExtendedLimitInformation`;
    - the struct members follow the docs' order;
    - the flag is 0x2000;
    - `AssignProcessToJobObject` needs a process handle with `PROCESS_SET_QUOTA` (0x0100) and `PROCESS_TERMINATE` (0x0001). Since Windows 8, a process already in a job can join another.
  - **Checked for real on this PC:** a test puts a sleeping child in a job and closes it, and the child dies at once; a child never put in a job outlives it.
  - **Best effort:**
    - a failed assignment is logged, not raised;
    - a grandchild started before its parent's assignment can stay outside the job;
    - on other systems, nothing happens.
  - The existing stop path (CTRL_BREAK, then `taskkill /T /F`) is unchanged.
- **`smoke paper` beside a running engine.**
  - **The problem:** the smoke starts a full ExecutionService on Book A, with its own monitor, reconcile and trade stream. During a session it would compete with the engine for the book, and without `--hold` it even flattens the book.
  - **So it refuses while an engine's heartbeat is fresh.**
  - **The exception is `--marketable --hold`,** the fire drill's step 1. It needs a fresh heartbeat that lists Book A: beside `run-day --books B`, no engine would sell the share at 15:55. Then:
    - its service runs `recover()` for the gate's start-of-day equity but starts no loops;
    - one OTO goes through the gate;
    - its own order is read until it fills;
    - it prints the stop leg and exits holding the share.
  - **The running engine then owns the share:** it protects it, and its 15:55 flatten, the watchdog or `trader flatten` sells it. `--hold` without `--marketable` is refused, since a resting order never fills.
  - **Its `human` row is written only after the refusals,** so a refused smoke doesn't disqualify the session.
- **Amends D-082's rules after a mutation check** (48 mutants of the watchdog's rules, the shared flatten, the unattended rule and criterion 4):
  - Three survived at first. Two only lacked tests: the KILL file in the last two minutes, and a heartbeat going stale during the 30 s wait. The second is left to the next run, which reads it twice before acting.
  - The third exposed a gap. After the close, with the heartbeat fresh, D-082 left every book to the engine's after-close pass, but that pass protects only the engine's own books.
    - Shares in a book it doesn't run (Book B on a `--books A` day) whose day stop expired at 16:00 would have had no stop overnight.
    - Such a book now gets GTC stops, with a P1 `watchdog_overnight`.
  - All 48 were then caught, and the identity control survived.
- **An adversarial review of P6.2's diff** found 3 major and 10 minor defects. All are fixed except the known limitations below.
  - **Major:**
    1. **`trader flatten` and `trader halt --flatten` no longer halted first,** a regression in P6.2a.
       - **The cause:** they fetched the calendar and built every book's broker before the halt. A failed calendar request, or Book B without keys, left no `manual` halt, and a running engine kept entering.
       - **Now:** each book is halted before any network call.
       - Each book's broker is built on its own. Missing B keys print "halted, but not flattened", and Book A is still flattened.
       - The calendar comes from the journal, where run-day caches it, and every broker is closed whatever fails.
    2. **After the close, with the heartbeat fresh, a book no engine runs got no stop.** This is the gap the mutation check found, fixed above.
       - A book the engine runs stays with its after-close pass.
       - That pass retries, then raises a P1 `not_flat` naming any shares with no stop (D-051), so its failure is never silent.
    3. **Reconcile's in-flight skip became permanent.**
       - **The cause:** it counted `lost` orders, and orders from every day. A stuck sell that a flatten settled as `lost` hid its symbol from reconcile for good.
       - **Now:** only today's `submitting` and `uncertain` orders put a symbol in flight (`reconcile.IN_FLIGHT`).
       - This also covers the engine's own lost orders, which had the same effect before this phase. A lost order that surfaces later is still recognized by its client order id.
  - **Minor:**
    - **Alerts are keyed tighter:**
      - a flatten's alert by its halt, so a new incident after a `trader resume` alerts again;
      - the late check is silenced only by a `not_flat` raised at or after the flatten time, not by a morning `trader flatten`'s;
      - an `engine_down` P2 is dropped when any alert already carries its episode, the P1s before the open and after the close included.
    - **The `engine_down` P2** names any book it couldn't read, rather than saying nothing is open.
    - **A book no engine runs** is read a second time 2 s later. Its positions and its orders are read separately, so a stop filling between the two looks like bare shares. Actions that need no second read are taken at once, without waiting for the other books' reads.
    - **A failed stuck-sell lookup** is logged, and the flatten goes on.
    - **The KILL file's sells** carry the `kill_switch` purpose.
      - The rule still applies only in regular hours and the late check. Before the open and after the close, a market sell can't fill, and the engine's own kill switch halts the books when it starts.
      - With the file there, a dead engine's flatten is a P2, not a P1: a person asked for it.
      - RISK §6 now says both.
    - **Coverage:** the watchdog counts a market sell at work for as long as it works, and the engine for a limited time after it was sent. RISK §6 and ARCHITECTURE §4.9 no longer call the rules identical.
    - **Shorting.** Two flatteners' sells racing for the same shares could open a short where the account allows one. SimBroker can't short, so no test can show it.
      - Doctor now warns when an account's `shorting_enabled` is true.
      - Alpaca's docs (checked 2026-11-24): `shorting_enabled` is the "Flag to denote whether or not the account is permitted to short".
      - The `no_shorting` account configuration: "If true, account becomes long-only mode".
      - Margin and shorting need $2,000 of equity. The docs don't say whether that holds on paper.
    - **The BOOT heartbeat,** and **`--hold`'s Book A and its `human` row** (above).
    - **A test** wrote `"\ai-day-trader\run-day"` without `r`. So `\a` and `\r` became control characters, and the test never used the real task names.
    - **The plan's copy** says "open orders only: cancel, P2", while RISK §6 and D-082 flatten and send a P1. The copy's header already says that DECISIONS govern.
  - **Known limitations** (now in RISK §6):
    - **Two flatteners on one book.** The first to finish clears the `flattening` marker, and the other renews it on its next pass. A running engine may place a stop in that gap; the flatten's next pass cancels it.
    - **`--hold`'s share passes the gate in its own process.** D-035's atomicity holds only within one process, so the engine can count the same capacity for R-006 and R-009 at that moment. The drill's R-020 sees only its own bare shares. It is one share, on a day that doesn't count.
    - **A halt set by another process doesn't set the engine's `_closing`** (D-082).
- **The mutation check, again after the fixes:** 59 mutants, plus the identity control.
  - Six of the first 48 were retargeted, where the fixes had rewritten their sites.
  - Eleven are new, one or two for each fix: the alert keys, the late check's window, the KILL file's purpose, the unread books, the second read of a book no engine runs and its 2 s wait, the failed lookup, reconcile's in-flight view (two), the BOOT beat and doctor's shorting warning.
  - All 59 are caught, and the identity control survives.
  - Removing the alert dedup was caught by only one test. A second test now covers it: a flatten that keeps failing is tried again on each run but alerts once.
- **Tests:**
  - the heartbeat's `pid` and `flattening`, and the beat written as BOOT begins;
  - `watchdog_silent` over a whole simulated day: heard from 11:00 to 13:00 only, it gives two P2s, at 09:40 and 13:00. A watchdog heard all day gives none;
  - the live wiring: no reader before a watchdog task is installed, and an unreadable status reads as never;
  - the job object, for real: closing kills, and an unassigned child lives on. Also each child of the transport is assigned;
  - `--hold` beside a running engine (the Harness's service): the share stays under its leg for 30 s of the engine's loops, with no incident;
  - the smoke's refusals, including an engine that doesn't run Book A;
  - **the review's fixes:**
    - a failed calendar still leaves both books halted and closes their brokers;
    - missing B keys still flatten A;
    - reconcile counts only today's unconfirmed orders as in flight;
    - **the watchdog:**
      - shares sold between an unmanaged book's two reads;
      - a new incident after a resume;
      - a morning `not_flat`;
      - a P1 after the close silencing its episode's P2;
      - the KILL file's purpose;
      - unread books named in the P2;
      - a failed lookup;
      - a flatten that keeps failing, tried each run and alerted once;
    - doctor's shorting warning.
- **Spec updates:**
  - **RISK §6:** the engine watches back, the second reads, the alert keys, the KILL file's hours and purpose, coverage, and the known limitations;
  - **RISK §11:** `watchdog_silent`;
  - **ARCHITECTURE:** §4.6 (reconcile's in-flight orders), §4.9 (coverage, the shared flatten, alert keys), §4.11 (`smoke paper --hold`, doctor's shorting check), §7's stop path and §11's heartbeat;
  - **PLAN:** P6.2b ticked; STATUS.
- **Verified:** 1,811 tests pass (1,810 in the full run, and one added during it, run with its file); ruff, its formatter and mypy are clean. The goldens (A+B, Book A, G3, G4b) and the daily report are unchanged.

## Open questions to resolve during the build (record answers as new entries)

- **Q1 (P1.2):** Is Alpaca's `movers` (top market movers) endpoint available on the free plan? If not, use the IEX snapshot gap scan. **Partly answered: D-024** (the 09:00 scan uses snapshots either way); doctor's `data.movers` check records availability once keys exist. **Answered by doctor on 2026-10-10: available** (`data.movers` PASS; the screener returned 10 gainers).
- **Q2 (P1.5):** The concurrent market-data WebSocket connection limit on the free plan, and whether a trading-status/halts channel is available on the IEX feed. **Partly answered: D-072.** The free plan allows two market-data sockets at once: the stock stream and the news stream ran together all of G4b's day. Still open: whether channels count toward the 30 symbols (15 symbols with trades and quotes drew no 405), and whether IEX sends `statuses`.
- **Q3 (P2.1):** Exact alpaca-py request classes for OTO with a `stop_loss` leg; confirm that close-position cancels the related open orders. **Answered: D-030** (the trading client uses httpx, not alpaca-py; `DELETE /v2/positions/{symbol}` does not cancel open orders, so flattening cancels first).
- **Q4 (G4a):** Does `--tools ""` work with `--json-schema`? What are the envelope fields and the exit codes on usage-limit or auth errors? **Answered: D-067.** Yes, under every isolation flag; D-067 lists the envelope's 27 keys. No call hit a usage-limit or auth error, so those exit codes are still unseen. The runner doesn't depend on them.
- **Q5 (P0.2):** Current SEC Section 31 fee rate and FINRA TAF per share, for fee estimates (set in `fees:`). **Answered: D-019.** D-058 and D-060: the CAT fee, which D-019 judged negligible, costs a cent a trading day once Alpaca rounds it up; the estimates leave it out.
- **Q6 (P1.4):** Typical count of eligible symbols, and the IEX OR-volume distribution, to sanity-check the liquidity guards. **Partly answered: D-042's results** (about 1,675 eligible symbols a day in 2026-04..10); the IEX OR-volume distribution is still to look at.
- **Q7 (P1.2):** Can the human's Alpaca account type fetch SIP historical bars (older than 15 min)? If not: open a regular account, or record a new decision to fall back to IEX history with rescaled ADV thresholds and IEX-based shadow outcomes (noisier). **Doctor's `data.sip_history` check answers this once keys exist.** **Answered by doctor on 2026-10-10: yes** (`data.sip_history` PASS: SPY daily SIP bars through 2026-10-09), so no IEX fallback is needed.
- **Q8 (P2.3):** Should the cash ledger settle on Alpaca's `settlement_date`, which skips bank holidays (D-022), or on T+1 trading days as RISK.md §4 says? Alpaca's date matches real cash-account behavior; the human decides. **Answered: D-034** (the human chose Alpaca's settlement date).
