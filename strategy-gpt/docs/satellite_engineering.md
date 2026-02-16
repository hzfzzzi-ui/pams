# Satellite Engineering Notes (v1.1)

## Data Sources (CN A-share)
- Prices/OHLCV: TusharePro (primary), AkShare/Eastmoney (fallback)
- Fundamentals: TusharePro / Eastmoney financials (Wind optional)
- Announcements/events: CNINFO + SSE/SZSE feeds (fallback Eastmoney announcements)
- Optional flows: Northbound flow via Eastmoney/Wind (use as explanation, not hard gates)

## Scheduling & Triggers
### EOD Universe + Technical Scan (daily)
- Run universe filters + trend gate across universe/watchlist.
- If pass trend gate AND price enters pullback zone (MA20 ± 1 ATR), mark as CANDIDATE.

### Intraday Refresh (15m, candidates only)
- For CANDIDATE symbols, update indicators and check trigger day conditions.
- If trigger met, compute entry/stop/valid_until and create order_draft.

### Event-Driven Veto (near-real-time)
- Poll/subscribe announcements and regulatory/earnings warnings.
- On major negative event, flag MAJOR_NEGATIVE_EVENT_WITHIN_TPLUS1 => hard veto.

## Fundamental Gate (low-frequency + event-driven)
- Refresh quarterly fundamentals at report releases.
- Maintain blacklist table with expiry windows (e.g., 20 trading days).

## Flow Confirm (computed only when needed)
- Compute only when order_draft is about to be created or awaiting confirmation.
- Outputs PASS/WEAK/FAIL + score + reason codes.

## Sentiment (multi-source)
- Structured (hard rules): breadth, limit up/down counts, big red frequency.
- Semantic (soft/explanatory): Bloomberg/Reuters/Caixin/Jin10 + search trends top topics + social heat.
- Output sent_state + sent_score + top_topics + topic_concentration; only sent_state affects sizing gates.

## Cross-market Reuse
- Overseas single stocks: same framework; swap liquidity floor (ADV USD) + event risk (earnings/SEC filings).
- Crypto: keep pullback tech; replace flow/sentiment with funding/OI/liquidations; keep harder circuit breakers.
