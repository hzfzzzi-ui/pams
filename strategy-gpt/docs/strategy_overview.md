# Strategy Overview (v1.1)

## Core Principles
- Technical = candidate generator (entry/stop/valid_until), backtestable.
- Flow/Sentiment = confirm/veto/size-down only (never flips direction).
- Fundamental = universe gate / risk minefield / holding bias only.
- Execution = order_draft; CN/US require manual confirm; crypto auto allowed with kill-switch.

## NAV Mode
- B_NAV (tradable NAV): includes assets movable within 5 trading days; excludes non-tradable assets (e.g., Huawei private equity).

## Risk Budget (B_NAV)
- Per-trade risk: defensive 0.5%, attack 0.8%
- Concurrent risk cap: defensive 2%, attack 3%
- Theme exposure cap (cross-market): defensive 25%, attack 35%
- Monthly circuit breakers: >4% pause new entries; >7% reduce-only; <3% resume.

## Core Policy (Monthly, hysteresis)
- Risk-On/Neutral/Off via monthly close vs MA10 with 2-month confirmation.
- Dry powder deployment on drawdowns: 10/20/30% => 25/35/40%.

## Satellite CN (10–30D, 2 attack positions)
- Universe gates: liquidity floor, excludes ST/delist risk/suspension; blacklist rules.
- Trend gate: MA20>MA60, close>MA60, 40D max return >=12%, reject MA tangle.
- Pullback: retrace to MA20 ± 1 ATR; trigger day close>MA20 & close>open.
- Flow confirm: shrink on pullback + pop on trigger; hard-fail big red with volume.
- Sentiment: HOT halves cap; PANIC requires flow PASS and forces defensive.
- Hard veto list: break MA60 with volume, major negative event, liquidity crash, etc.
