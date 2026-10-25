# IBKR Fee Settlement Evidence

## Scope And Handoff

L-level additive diagnostic, locally self-tested, not user acceptance. Only
`backend/api/services/ibkr_fee_settlement.py`, its native pytest file, and this
document belong to this change. Integration, state/nightly records, final CI,
PR, merge and release belong to LEAD. No endpoint, schema, sync, production DB,
frontend, configuration, accounting formula or persisted financial value changes.

```python
from services.ibkr_fee_settlement import review_fee_settlement

# xml_text and quality must come from the existing selected-report read transaction.
fee_review['settlement_evidence'] = review_fee_settlement(
    xml_text=xml_text, quality=quality,
)
```

The function performs no I/O and does not mutate inputs. Keep existing fee/unit,
fact-currency and lineage checks. Join records by `source_account_id` and
`execution_id` (`trade_id:...`, falling back to `transaction_id:...`), not list
position or symbol. This service reads official XML only; it does **not** prove
that stored trades/facts agree with that XML. A caller must not clear existing
blockers merely because one evidence record is sufficient.

`report_sha256` fingerprints the supplied UTF-8 text, not necessarily the original
file bytes if a caller has stripped a BOM or otherwise normalized the text. The
existing caller remains responsible for verifying the selected file's publication
hash/quality. `funds_line_indices` are zero-based positions among all
`StatementOfFundsLine` elements in document order, anchored to this text hash.
Each classified record also retains `trade_evidence_period` and
`funds_evidence_periods` (including line indices): these are the intersection of
its explicit statement/container ancestor periods, not the enclosing report span.

## Rules

- Require verified current report quality and matching expected/as-of dates.
  `using_previous` reports are refused even with otherwise matching quality dates.
  Every statement, including a single statement, needs explicit valid ordered
  from/to dates. Multiple statements are accepted only for one explicit account
  with non-overlapping periods; report as-of is their maximum explicit end date.
- Before any commission/zero classification, inspect each consumed row and its
  entire ancestor chain for contradictory account IDs/aliases or model scope.
  FlexStatement, AccountInformation and empty trade/funds containers are gated too.
  Explicit container periods may narrow but never expand statement coverage.
  Trade.tradeDate and StatementOfFundsLine.date must lie within their own effective
  ancestor period; sibling statements and gaps provide no substitute coverage.
  Missing/invalid dates or contradictory scope leave the report unverified and
  its total unknown. Settlement target/date rules below are unchanged.
- Every statement must explicitly include Trades. Missing Trades is unknown
  (`total=null`), not zero. Only explicitly empty Trades across all statements
  produces `total=0` / `explicit_empty_trade_section`; unsupported nonempty
  trade-section content cannot establish an empty result.
- Require an explicit unique execution account and trade ID or transaction ID.
  Every supplied secondary identity must agree. Duplicate executions, reused
  secondary IDs, or multiple matching funds lines are conflicts, never deduplicated
  or first-selected. Account is always part of identity.
- Require explicit currencies, finite signed Decimal fees, execution detail,
  instrument/direction/activity semantics and exact transaction/settlement dates.
  No parser defaults, nearest-date matching, inferred FX or currency conversion.
  A settlement date later than report as-of is not completed settlement evidence.
- `included`: one same-currency non-FX funds line has the same signed commission,
  amount = debit + credit = gross + commission + tax, correctly signed cash legs,
  matching official proceeds/taxes/netCash, and a consistent principal direction.
- `separate`: one linked commission-only line in the explicit fee currency has
  zero gross/tax and amount equal to the signed commission, with the same cash
  checks. Generic ADJ activity is accepted only in this commission-only shape;
  an arbitrary OFEE/description/amount match is not trade commission proof.
- `explicit_zero`: an explicit official zero commission proves only the reported
  fee, **not** individual cash settlement. A conflicting linked commission is
  rejected. Missing cash components remain missing, including a blank inactive
  debit/credit leg; they never become implicit zero. If both official and linked
  commissions explicitly equal zero but cash components are incomplete, the
  zero-only scope remains sufficient and `cash_evidence_reason` records the gap.
- Cross-currency translated principal and FX principal legs never establish
  individual settlement merely by row arithmetic. FX `netCash=0` is not a missing
  fee. A complete directly linked fee-only cash line can be separate evidence.
- Daily CashReport commissions are not read or allocated as individual proof.
  Exact Decimal arithmetic does not use float/tolerance or the caller's precision;
  numerical ranges requiring over 4096 digits are left unverified.

Each record contains `status` (`sufficient`, `unverified`, `conflict`),
`classification`, machine `reasons`, `evidence_scope`, and the independent
`individual_cash_settlement_verified` flag. `sufficient` is limited to that scope.
`report_component_arithmetic` retains the narrower amount/gross/commission/tax
check even when cash-leg evidence is incomplete; it is not settlement approval.
Adequately scoped reports remain `review`; invalid/missing report scope remains
`unverified`. Every execution/deduction flag is false, and no value is supplied
to valuation or repairs.

## Limitations And Validation

Related/original transaction links, adjustments with principal, multiple cash-leg
rows and ambiguous or incomplete fields remain unverified. Strictly requiring
explicit debit/credit values can leave valid-looking report rows unverified when
the unused leg is blank. Native cash evidence does not establish actual bank
clearing, fee persistence, historical completeness, NAV, PnL or full-account
approval. No real account/financial rows or XML are included in fixtures.
Strict parent-period gating can leave late-posted rows outside the containing
statement's business-date window unverified; reportDate is not substituted for
tradeDate/date to make them pass.

Synthetic pytest covers arbitrary accounts/IDs/currencies, all classifications,
signed rebates/zero/missing/nonfinite values, duplicate/secondary identities,
dates, namespaces, misleading FX/translated principal, aggregates, blank cash
legs, exact arithmetic, multi-period reports and input immutability. Review fixes
add parameterized parent/gap/ancestor coverage, model/account contradictions,
using_previous and missing-versus-empty Trades regressions, including zero fees.
Run the targeted pytest file, full Ruff for the service / E9/F for tests, and mypy with
`--ignore-missing-imports --follow-imports=skip --check-untyped-defs` on the service.
No DB integration/build/browser/release validation is claimed by this component.

Field semantics are based on IBKR's official
[Statement of Funds Flex reference](https://www.ibkrguides.com/reportingreference/reportguide/statement%20of%20fundsfq.htm).
Conservative proof rules and unsupported scopes above are this diagnostic's policy,
not a claim that IBKR guarantees individual native settlement from every funds row.
