from datetime import date, datetime, timedelta

from domain.accounts import sync_status
from domain.accounts.sync_status import _compact_summary


def test_summary_uses_confirmed_sources_and_preserves_unknown_balance():
    recorded = datetime.fromisoformat("2026-11-06T09:00:00+08:00")
    published = recorded + timedelta(days=1)
    row = {
        "active": True,
        "source_data": {"as_of": "2026-11-02", "synced_at": recorded},
        "cashflow": {"as_of": date(2026, 11, 1)},
        "investment": {"as_of": date(2026, 10, 30)},
        "balance": {"as_of": date(2026, 11, 3)},
    }
    assert _compact_summary(row, published, liability=False, now=published) == {
        "status": "normal", "balance_amount": None, "latest_trade_on": "2026-11-02",
        "last_synced_at": published, "review_reasons": [],
        "latest_source_on": "2026-11-02", "latest_cashflow_on": date(2026, 11, 1),
    }


def test_staleness_uses_calendar_month_and_anomalies_take_precedence():
    synced = datetime.fromisoformat("2026-01-31T09:00:00+08:00")
    deadline = datetime.fromisoformat("2026-02-28T09:00:00+08:00")
    row = {"active": True, "source_data": None, "cashflow": {}, "investment": {},
           "balance": {"amount": "1286.40", "synced_at": synced}}
    assert _compact_summary(row, None, liability=True, now=deadline)["status"] == "normal"
    late = deadline + timedelta(seconds=1)
    assert _compact_summary(row, None, liability=True, now=late)["status"] == "stale"
    row["source_data"] = {"duplicate_count": 1}
    result = _compact_summary(row, None, liability=True, now=late)
    assert result["status"] == "review"
    assert result["review_reasons"] == [{"kind": "duplicates", "count": 1}]
    assert result["balance_amount"] == "-1286.40"
    row["active"] = False
    assert _compact_summary(row, None, liability=True, now=late)["status"] == "inactive"


def test_no_successful_sync_does_not_report_normal():
    now = datetime.fromisoformat("2026-11-08T09:00:00+08:00")
    row = {"active": True, "source_data": None, "cashflow": {}, "investment": {}, "balance": {}}
    result = _compact_summary(row, None, liability=False, now=now)
    assert result["status"] == "review"
    assert result["review_reasons"] == [{"kind": "no_sync"}]


def test_account_center_keeps_legacy_sync_date_separate_from_import_and_balance(monkeypatch):
    imported_at = datetime.fromisoformat("2026-11-15T11:24:00+08:00")
    reference_day = date(2026, 6, 2)
    sync_day = date(2026, 10, 30)

    class Clock:
        @staticmethod
        def now(zone):
            return imported_at.astimezone(zone)

    class Cursor:
        def execute(self, query, params=None):
            self.rows = ([{"account_code": "recovered_fund", "synced_at": imported_at}]
                         if "FROM public.source_records" in query else [])

        def fetchall(self):
            return self.rows

    monkeypatch.setattr(sync_status, "datetime", Clock)
    monkeypatch.setattr(sync_status, "summaries", lambda cur, active: {})
    monkeypatch.setattr(sync_status, "SourceOriginals", lambda cur: None)
    monkeypatch.setattr(sync_status, "load_cash_rows", lambda *args, **kwargs: [])
    monkeypatch.setattr(sync_status, "statement_balances", lambda *args, **kwargs: {})
    monkeypatch.setattr(sync_status, "_manual_position_value", lambda *args: None)
    fund = {"account_type": "fund_platform", "truth_source": "manual", "reconcile_method": "position_nav",
            "historical_sync_as_of": sync_day, "manual_balance": {"amount": "100", "as_of": reference_day}}
    account_reference = {"amount": "250.00", "currency": "CNY", "as_of": reference_day, "source": "archive"}
    benefit = {"account_type": "benefit", "truth_source": "official_statement", "reconcile_method": "balance",
               "account_balance_reference": account_reference}
    config = {"accounts": {
        "recovered_fund": fund,
        "reference_only": fund,
        "inactive": {"active": False},
        "benefit": benefit,
        "future_reference": {**benefit, "account_balance_reference": {**account_reference, "as_of": date(2026, 11, 16)}},
    }}

    groups = sync_status.read_account_sync(Cursor(), config)["groups"]
    rows = {row["account_code"]: row["summary"] for group in groups for row in group["rows"]}
    assert set(rows) == {"recovered_fund", "reference_only", "inactive", "benefit", "future_reference"}
    assert rows["inactive"]["status"] == "inactive"
    for code in ("recovered_fund", "reference_only"):
        assert rows[code]["status"] == "stale"
        assert rows[code]["last_synced_at"] is None
        assert rows[code]["last_synced_on"] == sync_day
        assert rows[code]["balance_as_of"] == reference_day
        assert rows[code]["balance_amount"] == "100"
    assert rows["benefit"] == {
        "status": "stale", "balance_amount": "250.00", "latest_trade_on": None,
        "last_synced_at": None, "last_synced_on": reference_day, "review_reasons": [],
        "balance_as_of": reference_day, "balance_currency": "CNY",
        "balance_basis": "historical_account_reference", "balance_label": "历史参考余额",
    }
    assert rows["future_reference"]["balance_amount"] is None
    assert rows["future_reference"]["status"] == "review"

    def recorded_balance(cur, candidates, cfg):
        if cfg is benefit:
            return {"status": "recorded", "amount": "300.00", "as_of": sync_day, "synced_at": imported_at}
        return {"status": "uninitialized"}

    monkeypatch.setattr(sync_status, "_balance", recorded_balance)
    groups = sync_status.read_account_sync(Cursor(), config)["groups"]
    summary = next(row["summary"] for group in groups for row in group["rows"] if row["account_code"] == "benefit")
    assert (summary["balance_amount"], summary["balance_basis"], summary["status"]) == (
        "300.00", "confirmed_snapshot", "normal",
    )
    assert summary["last_synced_at"] == imported_at


def test_account_center_uses_the_same_amount_field_for_snapshot_and_statement():
    now = datetime.fromisoformat("2026-11-08T09:00:00+08:00")
    day = date(2026, 11, 2)
    row = {"active": True, "source_data": None, "cashflow": {}, "investment": {}, "balance": {}}
    source = {"amount": "-125.30", "as_of": day, "basis": "credit_card_statement"}
    result = _compact_summary(row, now, liability=True, now=now, source_balance=source)
    assert result["balance_amount"] == "-125.30"
    assert result["balance_as_of"] == day
    bank = {"amount": "75.00", "as_of": day, "basis": "bank_statement"}
    result = _compact_summary(row, now, liability=False, now=now, source_balance=bank)
    assert (result["balance_amount"], result["balance_as_of"], result["balance_label"]) == ("75.00", day, "账单余额")
    row["balance"] = {"amount": "100.50", "as_of": day}
    result = _compact_summary(row, now, liability=True, now=now, source_balance=source)
    assert result["balance_amount"] == "-100.50"
    assert result["balance_basis"] == "confirmed_snapshot"


def test_other_review_reasons_use_same_evidence_as_status_and_list_only_flagged_rows(monkeypatch):
    now = datetime.fromisoformat("2026-11-08T09:00:00+08:00")
    row = {"active": True, "source_data": None, "cashflow": {"has_unverified": True},
           "investment": {"status": "review"}, "balance": {"status": "review"}}
    result = _compact_summary(row, now, liability=False, now=now)
    assert result["review_reasons"] == [{"kind": "cashflow"}, {"kind": "investment"}, {"kind": "balance"}]

    def load_cash_rows(cur, *, start, end, base_currency, account_codes):
        assert account_codes == ["fixture"] and base_currency == "CNY"
        rows = [{"fact_id": n, "transaction_date": date(2026, 11, 1), "currency": "CNY",
                 "amount": "1.25", "source_evidence_status": status, "summary": "示例",
                 "raw_fields": {"private": True}}
                for n, status in enumerate(("unknown", "verified", "unknown"), 1)]
        return rows

    monkeypatch.setattr(sync_status, "load_cash_rows", load_cash_rows)
    records = sync_status.review_records(None, {"accounts": {"fixture": {"base_currency": "CNY"}}},
                                         "fixture", "cashflow", 1)
    assert records == [{"record_id": 1, "day": date(2026, 11, 1), "currency": "CNY",
                        "amount": "1.25", "summary": "示例"}]
