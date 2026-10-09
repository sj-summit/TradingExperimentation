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

---

## Open questions to resolve during the build (record answers as new entries)

- **Q1 (P1.2):** Is Alpaca's `movers` (top market movers) endpoint available on the free plan? If not, use the IEX snapshot gap scan.
- **Q2 (P1.5):** The concurrent market-data WebSocket connection limit on the free plan, and whether a trading-status/halts channel is available on the IEX feed.
- **Q3 (P2.1):** Exact alpaca-py request classes for OTO with a `stop_loss` leg; confirm that close-position cancels the related open orders.
- **Q4 (G4a):** Does `--tools ""` work with `--json-schema`? What are the envelope fields and the exit codes on usage-limit or auth errors?
- **Q5 (P0.2):** Current SEC Section 31 fee rate and FINRA TAF per share, for fee estimates (set in `fees:`).
- **Q6 (P1.4):** Typical count of eligible symbols, and the IEX OR-volume distribution, to sanity-check the liquidity guards.
- **Q7 (P1.2):** Can the human's Alpaca account type fetch SIP historical bars (older than 15 min)? If not: open a regular account, or record a new decision to fall back to IEX history with rescaled ADV thresholds and IEX-based shadow outcomes (noisier).
