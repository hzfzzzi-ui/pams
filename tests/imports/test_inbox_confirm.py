"""Unit tests for import_inbox period-key duplicate detection helper."""

from endpoints.import_inbox import _filename_period_key


def test_filename_period_key_extracts_year_month():
    assert _filename_period_key("2026-11_广发7443电子账单.csv") == "2026-11"
    assert _filename_period_key("2026年11月综合对账单打印版.pdf") == "2026-11"
    assert _filename_period_key("2026-10_Santander.pdf") == "2026-10"
    assert _filename_period_key("table.xls") is None
