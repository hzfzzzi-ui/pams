"""Synthetic THS export contract. Never commit the user's financial records."""
import csv
from io import StringIO
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend/api"))
from domain.imports import account_import_parse as parser
from domain.imports.account_import_preflight import source_key

HEADERS = ["成交日期", "证券代码", "证券名称", "操作", "成交数量", "成交均价", "成交金额", "发生金额",
           "手续费", "印花税", "其他杂费", "本次金额", "合同编号", "成交编号", "交易市场", "成交时间", "余额", "定位串", "市场代码", ""]


def event(operation="证券买入", **changes):
    row = dict(zip(HEADERS, ["20251020", "000001", "Synthetic stock", operation, "100", "10.0000", "1000.000", "-1003.100",
                            "3.000", "0.000", "0.100", "9000.000", "contract", "execution", "深圳Ａ股", "09:31:02", "100", "20251020!line1", "1", ""]))
    if operation not in {"证券买入", "证券卖出"}:
        row.update({"成交均价": "0.0000", "成交金额": "0.000", "手续费": "0.000", "印花税": "0.000", "其他杂费": "0.000", "成交编号": "0"})
    if operation in {"银行转证券", "证券转银行", "利息归本"}:
        row.update({"证券代码": "", "证券名称": "", "成交数量": "0"})
    row.update(changes)
    return row


def export(*rows, encoding="gb18030", headers=None):
    columns = headers or HEADERS
    output = StringIO(newline="")
    csv.writer(output, delimiter="\t").writerows([columns, *[[row.get(k, "") for k in columns] for row in rows]])
    return output.getvalue().encode(encoding)


def parse(*rows, **kwargs):
    return parser.parse_document(export(*rows), "table.xls", "tonghuashun", "trade", **kwargs)


@pytest.mark.parametrize("encoding", ["gb18030", "utf-8-sig"])
@pytest.mark.parametrize("filename", ["table.xls", "different.XLS", "broker.tsv", "broker.csv"])
def test_native_container_names_and_encodings_need_no_conversion(encoding, filename):
    content = export(event(), encoding=encoding)
    result = parser.parse_document(content, filename, "tonghuashun", "trade")
    row = result["rows"][0]
    assert row["fields"]["symbol"] == "000001"
    assert row["fields"]["gross_amount"] == "1000.000"
    assert row["fields"]["net_cash"] == "-1003.100"
    assert row["fields"]["price"] == "10.0000"
    assert row["fields"]["fee_amount"] == "3.100" and row["fields"]["tax_amount"] == "0.000"
    assert row["fields"]["asset_type"] is None
    assert row["source"]["profile"] == "ths_broker_ledger_v1"
    assert row["fields"]["source_ref"] == "20251020!line1"
    assert row["source"]["fee_components"]["其他杂费"] == "0.100"
    assert row["source"]["raw_text"].split("\t")[0] == "20251020"
    assert not any(i["severity"] == "error" for i in row["issues"])


@pytest.mark.parametrize("operation,action,net,gross", [
    ("红利入账", "dividend", "30.00", "30.00"), ("红利税补扣", "tax", "-2.10", "2.10"),
    ("利息归本", "interest", "2.20", "2.20"), ("银行转证券", "deposit", "500.00", "500.00"),
    ("证券转银行", "withdraw", "-100.00", "100.00"),
])
def test_mixed_ledger_actions_are_not_fake_stock_buys(operation, action, net, gross):
    raw = event(operation, **{"发生金额": net, "成交金额": gross if action == "dividend" else "0.000"})
    row = parse(raw)["rows"][0]
    fields = row["fields"]
    assert fields["action"] == action and fields["status"] == "confirmed"
    assert fields["net_cash"] == net and fields["gross_amount"] == gross
    assert fields["quantity"] is fields["price"] is None
    if action in {"interest", "deposit", "withdraw"}:
        assert fields["asset_type"] == "cash" and fields["symbol"] == "CASH_CNY"
    if action == "tax":
        assert fields["tax_amount"] == "2.10" and fields["fee_amount"] == "0.000"


@pytest.mark.parametrize("component", ["手续费", "印花税", "其他杂费"])
@pytest.mark.parametrize("value", ["", "--", "not-a-number", "-1"])
def test_unknown_or_negative_charges_never_become_zero(component, value):
    row = parse(event(**{component: value}))["rows"][0]
    assert row["fields"]["fee_amount"] is None
    assert any(i["severity"] == "error" for i in row["issues"])


def test_identity_is_scoped_and_does_not_use_placeholder_execution_zero():
    one = parse(event("红利入账", **{"成交金额": "30", "发生金额": "30"}))["rows"][0]
    two = parse(event("红利入账", **{"成交金额": "30", "发生金额": "30", "成交日期": "20251021", "定位串": "20251021!line1"}))["rows"][0]
    batch = {"account_code": "tonghuashun", "kind": "trade"}
    assert one["fields"]["source_ref"] == "20251020!line1"
    assert source_key(batch, one) != source_key(batch, two)
    again = parser.parse_document(export(event("红利入账", **{"成交金额": "30", "发生金额": "30"}), encoding="utf-8"), "renamed.tsv", "tonghuashun", "trade")["rows"][0]
    assert source_key(batch, one) == source_key(batch, again)
    no_id = parse(event(**{"成交编号": "000", "定位串": ""}))["rows"][0]
    assert no_id["fields"]["source_ref"] is None and "identity" not in no_id["source"]


@pytest.mark.parametrize("content", [b"\xd0\xcf\x11\xe0" + b"\x00" * 100, b"PK\x03\x04evil", b"<html><script>evil</script></html>", b"not a table"])
def test_xls_is_not_a_bypass_for_binary_html_or_unknown_files(content):
    with pytest.raises(ValueError):
        parser.parse_document(content, "table.xls", "tonghuashun", "trade")


def test_unknown_operation_and_misleading_column_mapping_stay_visible():
    row = parse(event("未知业务"))["rows"][0]
    assert any(i["code"] == "unsupported_broker_action" for i in row["issues"])
    assert row["fields"]["quantity"] == "100"
    row = parse(event(), mapping={"net_cash": "本次金额"})["rows"][0]
    assert row["fields"]["net_cash"] == "9000.000"
    assert any(i["code"] == "broker_mapping_conflict" for i in row["issues"])


def test_explicit_pending_status_cannot_be_promoted_by_a_known_operation():
    raw = event(**{"状态": "待确认"})
    result = parser.parse_document(export(raw, headers=[*HEADERS, "状态"]), "table.xls", "tonghuashun", "trade")
    assert result["rows"][0]["fields"]["status"] == "pending"


def test_changed_operation_keeps_source_identity_for_conflict_detection():
    before = parse(event())["rows"][0]
    changed = parse(event("证券卖出"))["rows"][0]
    batch = {"account_code": "tonghuashun", "kind": "trade"}
    assert source_key(batch, before) == source_key(batch, changed)
