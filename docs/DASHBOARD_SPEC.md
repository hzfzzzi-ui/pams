# Dashboard v1 Spec (PAMS)

Target: `http://pams.local`

## Top bar
- Brand: PAMS
- Last updated timestamp (UTC-6)
- Data completeness indicator: Firefly / Prices / Screenshots (CCB/LCT/THS) / Binance / IBKR

## KPI row (5 cards)
1) Total assets
2) Net worth (assets - liabilities)
3) Cash
4) Liabilities
5) Net expense today (expense - income)

Each card shows delta vs yesterday (absolute + % if available).

## Controls
- Time range: Today / 7D / 30D / This week / This quarter
- Dimension mode: Overview / By platform / By asset class

## Section: Asset overview (2 columns)
### Left: Asset structure (pie)
- Categories: Cash / Funds / A-shares / US-stocks / Wealth Mgmt / Gold / Crypto
- Show amount + share

### Right: Platform distribution (bar + breakdown)
Platforms: CCB / Licaitong / Tonghuashun / IBKR / Binance
For each platform:
- Total
- Cash
- Liabilities
- Breakdown by asset categories (stacked bar or expandable list)

## Section: Investment signals (middle, prominent)
A prioritized list of alerts:
- Risk rule: cost-basis drawdown <= -30% triggers "reduce position to 70%" suggestion
- Position cap alerts (single asset cap; category/platform caps as defined)
- Data missing alerts (today's screenshots missing; Binance/IBKR sync missing; etc.)

Each alert includes: reason, impact, suggested action.

## Section: Cashflow (income/expense) chart
- Main chart: line chart with Income / Expense / Net
- Table below: breakdown by category (and optionally platform/source) for selected time range

## Search
- Global search box
- Query types: symbol/name, platform, category
- Results: positions, transactions, categories, alerts

## Footer: Data sources status
- Firefly: ok/missing
- Prices: ok/missing
- Screenshots: CCB/LCT/THS ok/missing
- Binance: ok/missing
- IBKR: ok/missing

## Update schedule
- Daily refresh: UTC-6 09:00 (batch)
- Optional manual refresh button (later)
