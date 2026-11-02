from pathlib import Path
import sys
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend" / "api"))
from domain.imports import ccb_spreadsheet as ccb
from domain.imports import account_import_statement as statement
from domain.imports import document_kind

HEADER = "交易日,入账日,信用卡卡号,类型,入账币种,入账金额,交易描述"

def credit(rows):
    return ("中国建设银行信用卡交易明细\n" + HEADER + "\n" + rows).encode("gb18030")

def test_credit_preserves_direction_description_and_raw():
    data = credit("20260101,20260102,****1234,消费,人民币,20.00,示例,商户\n"
                  "20260103,20260104,****1234,存入,人民币,-10.00,还款\n")
    parsed = ccb.parse(data, "credit.csv", "ccb_1234", "ccb_credit_pdf")
    assert [r["fields"]["net_cash"] for r in parsed["rows"]] == ["-20.00", "10.00"]
    assert parsed["rows"][1]["fields"]["action"] == "transfer"
    assert parsed["rows"][0]["fields"]["note"] == "消费 示例,商户"
    assert len(parsed["rows"][0]["source"]["statement_row"]["raw"]["row"]) == 8

@pytest.mark.parametrize("row", ["20260101,20260102,****9999,消费,人民币,1,示例", "20260101,20260102,****1234,消费,美元,1,示例", "20260101,20260102,****1234,消费,人民币,NaN,示例"])
def test_credit_rejects_wrong_account_currency_or_amount(row):
    with pytest.raises(ValueError):
        ccb.parse(credit(row), "credit.csv", "ccb_1234", "ccb_credit_pdf")

def test_debit_xls_checks_identity_and_signed_amount(monkeypatch):
    import xlrd
    rows = [["中国建设银行个人活期账户全部交易明细"], ["卡号/账号:123456781234"], [], list(ccb._DEBIT),
            ["1", "转账", "人民币元", "钞", "20260102", "-1,234.56", "100", "示例", "对方"]]
    class Sheet:
        nrows = len(rows)
        ncols = 9
        def row_values(self, i): return rows[i]
    class Book:
        nsheets = 1
        def sheet_by_index(self, i): return Sheet()
    monkeypatch.setattr(xlrd, "open_workbook", lambda **kwargs: Book())
    data = bytes.fromhex("d0cf11e0a1b11ae1") + b"synthetic"
    parsed = ccb.parse(data, "debit.xls", "ccb_1234", "ccb_debit_pdf")
    assert parsed["rows"][0]["fields"]["net_cash"] == "-1234.56"
    with pytest.raises(ValueError, match="账户"):
        ccb.parse(data, "debit.xls", "ccb_9999", "ccb_debit_pdf")

def test_detect_and_statement_adapter_use_selected_profile(monkeypatch):
    monkeypatch.setattr(statement, "profiles", lambda: {"ccb_1234": [{"statement_template":"ccb_credit_pdf"}]})
    data = credit("20260101,20260102,****1234,消费,人民币,20.00,示例")
    assert document_kind.detect(data, "credit.csv", "ccb_1234") == {"kind":"cash", "status":"detected"}
    row = statement.parse(data, "credit.csv", "ccb_1234", "cash")["rows"][0]
    assert row["source"]["statement"] is True
    assert row["fields"]["net_cash"] == "-20.00"
