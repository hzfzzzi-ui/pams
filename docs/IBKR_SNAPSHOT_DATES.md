# IBKR Snapshot Business Dates

- Broker report dates are source facts. Portfolio display dates must not relabel a report's holdings, cash or history.
- `as_of` and `align_to_latest_snapshot` remain accepted request fields, but a target different from the actual statement date is rejected before business writes. Default and same-date requests retain their prior values and response fields.
- The current projection must not share mutable dictionaries with historical rows. The holdings writer validates every input date and never modifies caller-owned rows.
- A rejected sync preserves the active published report and business facts; the existing failure audit may still be recorded. Tests cover earlier/future explicit overrides, alignment, rollback and valid same-date imports.

## Historical Correction Boundary

The approved 2026-10-26 operation is separate from normal sync: remove two proven extra rows and correct 32 input fields across twelve misdated snapshots, using the complete official account/day report. It does not authorize other dates, funding, fee, source-link or FX changes.

- Bind each correction to account, business date, stable source identity, original sync evidence, exact source XML hash, fresh before-state hashes and explicit approval.
- Restore the affected tables and FK dependencies into isolated PostgreSQL first. Exercise transaction rollback and idempotent retry using the actual schema, including generated PnL columns.
- Preserve matched history rows. Change only approved snapshot input fields; generated `holding_pnl` and the correction `updated_at` are expected derived/audit changes, not additional arbitrary edits.
- Lock and validate the current before-state before applying the entire batch atomically. Drift or an unexpected result aborts all changes. Keep private write-intent, original rows, approval, source evidence and committed-result receipts.
- PR #57 remains deletion-only for historical alignment. This bounded operational approval does not loosen its default authorization or add an automatically executing production repair endpoint.
- Recheck financial APIs before/after code deployment separately from the approved historical data correction. Expected historical amount changes require a source-backed bridge; current NAV, cost formulas, return formulas and UI layout are not silently rewritten.
