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
