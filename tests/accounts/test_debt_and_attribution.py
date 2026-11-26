import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend" / "api"))

from domain.accounts.views.debt_and_attribution import build_debt_rows, next_due_date_countdown


def test_next_due_date_countdown_rolls_to_next_month_after_due_day():
    due_iso, days_left = next_due_date_countdown(25, date(2026, 11, 26))
    assert due_iso == "2026-12-25"
    assert days_left == 29

    due_iso2, days_left2 = next_due_date_countdown(28, date(2026, 11, 26))
    assert due_iso2 == "2026-11-28"
    assert days_left2 == 2


def test_build_debt_rows_sorts_by_apr_and_computes_monthly_interest():
    rows = build_debt_rows(
        ibkr_debt_cny=180000.0,
        esop_debt_cny=310000.0,
        credit_cards=[{"account_code": "ccb_cny", "name": "建行0453信用卡", "currency": "CNY", "balance_cny": 14476.08}],
        ref_day=date(2026, 11, 26),
    )
    assert [r["id"] for r in rows] == ["ibkr_margin", "esop_loan", "cc:ccb_cny:建行0453信用卡"]
    assert rows[0]["monthly_interest_cny"] == round(180000.0 * 0.058 / 12.0, 2)
    assert rows[2]["days_until_due"] == 29
