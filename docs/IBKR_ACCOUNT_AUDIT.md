# IBKR Independent Account Evidence Audit

## Scope And Ownership

L-level additive diagnostic module, not a replacement valuation or reconciliation
engine. `services.ibkr_account_audit.audit_ibkr_account` is a pure function: no
database, file, network, clock, collector, refresh, schema, repair or trading calls.
It does not import the financial calculation under audit. Endpoint integration,
publication selection, state/report updates, CI, PR, release and user acceptance
remain the lead's responsibility. No existing service or frontend is changed.

## Call Contract

```python
audit_ibkr_account(
    xml_text=selected_original_bytes.decode("utf-8"),
    expected_sha256=selected_report_sha256,
    account_id=official_account_id,
    as_of=statement_date,          # datetime.date, exact closing date
    base_currency=official_base,  # uppercase ASCII currency, not a display currency
    internal_state=qualified_state,
    period_start=period_start,    # optional inclusive ChangeInNAV.fromDate
    history_dates=requested_dates,  # optional exact dates; not a calendar inference
    quality=publication_quality,   # optional existing report_quality result
)
```

The caller selects the committed official report using the existing publication
contract. Hash the original bytes and decode without newline normalization. The
function verifies the supplied hash; it cannot authenticate a document as broker
issued or verify publication by itself (`publication_verified=false`). Omitting
the hash or publication quality does not establish that provenance.

`qualified_state` is an explicit normalized mapping, not an unmodified performance
response. Account aliases such as `ibkr` are not official account identities.
Required common scope:

| Key | Required Meaning |
| --- | --- |
| `account_id` | Exact official account ID |
| `as_of` | Exact official closing date, ISO string or date |
| `base_currency` | AccountInformation currency, not personal/display currency |
| `ownership_basis` | `full_account`, without personal ownership scaling |
| `valuation_basis` | `ibkr_statement_close`, not live/Yahoo or mixed-date values |

Metric-specific state:

| Keys | Basis Tag |
| --- | --- |
| `cash_base`, optional `cash_by_currency` mapping | `cash_basis=ending_cash` (trade-date cash, not settled cash) |
| `position_value_base`, optional `positions` list | `position_basis=signed_market_value` (net signed official position values, not gross exposure) |
| `net_liquidation_base` | `nav_basis=official_nav_total` (full NAV including accruals/other components) |
| `fx_rates_to_base` mapping from currency to rate | `fx_basis=statement_date_to_base` |
| optional `period` mapping | `basis=nav_change_less_external_flows`, exact `from_date` / `to_date`, plus `opening_nav_base`, `net_external_flow_base`, `closing_nav_base`, `pnl_base` |

Each internal position supplies string `conid`, `currency`, `quantity` and native
`position_value`. Its scope is inherited from the common scope; explicit conflicting
account/date/ownership/valuation overrides are refused. The period similarly inherits
the account/base/ownership/valuation scope. Do not stamp these tags onto existing
outputs merely to make them comparable: the adapter must demonstrate their origin.
Missing tags or unsupported semantics mean review and no affected comparison.

## Evidence Rules

- Select exactly one FlexStatement for the requested account and closing date.
  Other accounts/dates never contribute to its amounts. Model-level evidence or
  contradictory account IDs cannot establish full-account scope. Every consumed
  evidence node, including ConversionRate, is checked along its entire ancestor
  chain through the document root. Section/statement/custom-wrapper scope cannot
  be overridden by an apparently matching leaf. An empty OpenPositions section
  is subject to the same ownership checks before it can prove zero positions.
- Base currency must be explicit in AccountInformation; USD is not assumed.
- Native cash/position units, FX source/target units and internal currency-map
  keys must be exactly ASCII `[A-Z]{3}`. Missing values, lowercase/mixed case,
  Unicode lookalikes, whitespace and arbitrary unit strings remain review; no
  trimming, uppercasing, registry lookup or default currency is applied.
  `BASE_SUMMARY` is only the explicit cash-summary marker, never a native currency
  or an internal currency-map key. Invalid units cannot enter native amounts,
  contract identities, rate maps or their affected comparisons. Malformed current
  ConversionRate currency pairs block non-base conversions even when a position
  also supplies a rate; known base identity and independent base NAV/summary
  evidence do not depend on that conversion. Independent supplied base aggregates
  are not inferred from invalid internal detail keys.
- Cash is signed CashReportCurrency.endingCash from BASE_SUMMARY, or complete
  currency rows converted using explicit same-date official rates. Missing base
  summary is not replaced if it exists but is invalid/ambiguous. Native cash and
  the base summary are not added together. Negative cash is already included;
  no second financing deduction is proposed.
  Native fallback requires a same-date row for every native currency observed in
  the selected statement, not just currencies surviving a date filter. Uncovered
  currencies keep their raw stale/missing/invalid/future-date reason and prevent
  a partial sum from becoming complete cash. An older observation is ignored only
  when that currency also has adequate current evidence. Currency coverage and
  rate completeness are explicit, separate evidence fields.
- Positions require an OpenPositions section and SUMMARY rows with stable
  contract/currency identities and explicit quantity/value. LOT rows are never
  added to summary rows. Any lot-only contract blocks the aggregate. An explicit
  empty section can prove zero positions; a missing section cannot. Values are
  signed, and positionValue is used directly, avoiding option multiplier and
  short-sign reconstruction errors. Optional details are matched across the
  complete dataset, not the truncated response sample.
- FX uses explicit fxRateToBase or exact-date direct ConversionRate. No current
  FX, inverted or forward-filled rate is invented. Base-to-base is exactly one.
  Conflicts/missing rates block affected derived values; base summary cash and
  independently reported NAV do not require reconstructing FX.
  A cross-section cash/position rate conflict removes that rate from the usable
  map and nulls dependent aggregates before any internal comparison. Independent
  BASE_SUMMARY cash and NAV can still be compared, including reporting their real
  deltas. Native FX completeness stays false even when base-summary cash is usable.
  Internal rates must also be positive, with exact base identity one, before
  numerical tolerance is considered; invalid rate semantics are review, not a
  financial delta or a tiny-difference success.
- NAV requires EquitySummaryByReportDateInBase.total for the same account/date.
  Cash plus position values is **not** accepted as complete NAV, because accruals,
  derivatives and other balance-sheet components may differ.
- Period amount evidence requires an exact-range ChangeInNAV with explicit
  startingValue, endingValue and each of depositsWithdrawals,
  internalCashTransfers, assetTransfers, linkingAdjustments, debitCardActivity,
  billPay (including explicit zeros). Unknown transfer/linking/deposit variants
  are refused. Net flows retain their official signs; P&L equals closing NAV
  minus opening NAV minus those flows. A conflicting independent closing NAV
  blocks the P&L comparison. YTD deposits, stock-only NAV, realized trade P&L or
  starting cash do not substitute for this evidence. No percentage return,
  TWR/MWR or inception return is certified.
- Historical coverage **always remains unverified** in this narrow audit. Observed
  statement dates and missing requested statement dates are reported separately.
  Even daily statements for every requested date do not prove an entire NAV/return
  series. The report's enclosing from/to range is never treated as covered days.

Official semantic references: [cash versus settled cash](https://www.ibkrguides.com/reportingreference/reportguide/cashreport.htm),
[NAV and its components](https://www.ibkrguides.com/reportingreference/reportguide/netassetvalue_default.htm),
[Change in NAV](https://www.ibkrguides.com/reportingreference/reportguide/changeinnav_fq.htm).

## Results And Limits

All financial values/deltas/rates are finite Decimal strings or null, never floats
or missing-to-zero substitutions. Absolute comparison tolerance is `0.00000001`,
matching the existing reconciliation convention, not a dollar/count/date whitelist.
All input data is evaluated; only output issue/detail/date lists are capped at 30,
with counts/truncation flags. XML input is resource bounded at 32 MiB; DTD/entities
are refused. Numeric input representation is bounded without a business amount
threshold. Decimal arithmetic uses an independent context (precision 160,
exponents -512 to 512), covering the existing parsed magnitude bounds and their
products/sums without inheriting caller precision, exponent limits, rounding or
traps. Namespaced Flex XML is supported.

- `ok`: only the named, adequate/comparable check passed. Position aggregate
  equality does not certify absent details (`detail_comparison=not_supplied`).
- `review`: missing/stale/ambiguous evidence, semantic mismatch or unverified
  coverage. Examples: `stale_report`, `stale_internal_state`, `source_unverified`,
  `missing_evidence`, `missing_basis`, `semantic_mismatch`,
  `missing_historical_fx`, `period_evidence_incomplete`, `history_coverage_unverified`.
- `error`: comparable finite amounts differ (`financial_delta`), or XML/hash
  integrity fails. A financial delta includes official/internal/delta/tolerance.

Checks retain reason counts and bounded issue records. A review in one metric
cannot erase another metric's error. `read_only=true`, `executable=false` always.
Overall status remains review unless an error occurs, because history is not
certified. No output authorizes repair, additional fees, deductions or baseline
promotion. Production financial correctness, complete history, live endpoint
behavior, publication authenticity and deployment are not proved by unit tests.
