from __future__ import annotations

import csv
import os
import struct
import subprocess
import sys
import zlib
from io import BytesIO, StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

API_ROOT = Path(__file__).resolve().parents[2] / "backend" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from domain.imports import account_import_ocr as ocr
from domain.imports.account_import_parse import FIELDS


def image_bytes(format="PNG", size=(800, 800)):
    stream = BytesIO()
    with Image.new("RGB", size, "white") as image:
        image.save(stream, format=format)
    return stream.getvalue()


def tsv_bytes(lines, *, confidence="96.500000"):
    stream = StringIO(newline="")
    writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
    writer.writerow(["level", "page_num", "block_num", "par_num", "line_num", "word_num", "left", "top", "width", "height", "conf", "text"])
    for number, line in enumerate(lines, 1):
        for index, word in enumerate(line if isinstance(line, list) else [line], 1):
            writer.writerow([5, 1, 1, 1, number, index, 10 + (index - 1) * 180, 10 + number * 20, 150, 15, confidence, word])
    return stream.getvalue().encode("utf-8")


def install_engine(monkeypatch, lines=None, *, data=None):
    calls = []
    monkeypatch.setattr(ocr.shutil, "which", lambda name: "C:/local/tesseract.exe")

    def runner(command, *, content=None, timeout):
        calls.append((command, content, timeout))
        if command[-1] == "--list-langs":
            return b"List of available languages (2):\nchi_sim\neng\n"
        return data if data is not None else tsv_bytes(lines or [])

    monkeypatch.setattr(ocr, "_run", runner)
    return calls


def parse(monkeypatch, lines, *, kind="holding", profile="tencent_licaitong", confidence="96.500000"):
    install_engine(monkeypatch, data=tsv_bytes(lines, confidence=confidence))
    return ocr.parse_image(image_bytes(), profile, kind)


def issues(result):
    return {(issue["field"], issue["code"]) for row in result["rows"] for issue in row["issues"]}


def test_holdings_explicit_fields_context_evidence_and_no_rounding(monkeypatch):
    lines = ["基金持仓 2026-10-28", "基金名称 测试基金", "代码 000001", "份额 123.45678901",
             "持仓成本 1,234.56789元", "市值 1,300.12345", "净值 1.23456789"]
    result = parse(monkeypatch, lines)
    assert result["origin"] == "ocr" and result["ocr_status"] == "ok"
    assert result["suggested_mapping"] == {}
    row = result["rows"][0]
    fields = row["fields"]
    assert set(fields) == set(FIELDS) and all(v is None or isinstance(v, str) for v in fields.values())
    assert fields["name"] == "测试基金" and fields["symbol"] == "000001" and fields["asset_type"] == "fund"
    assert fields["quantity"] == "123.45678901" and fields["cost"] == "1234.56789"
    assert fields["market_value"] == "1300.12345" and fields["price"] == "1.23456789"
    assert fields["business_date"] == "2026-10-28" and fields["cost_basis_type"] == "holding_cost"
    assert fields["fee_amount"] is None and fields["tax_amount"] is None and fields["status"] is None and fields["currency"] is None
    assert row["acknowledged"] is False and row["require_review"] is False
    assert (None, "ocr_review_required") in issues(result)
    assert result["tsv"] == tsv_bytes(lines).decode("utf-8") and result["text"] == "\n".join(lines)
    assert result["words"][0] == {"text": lines[0], "confidence": "96.500000", "box": {"left": 10, "top": 30, "width": 150, "height": 15}, "line": 1}
    assert row["source"]["context"]["date_text"] == lines[0]
    assert row["source"]["words"][0]["text"] == lines[0]


@pytest.mark.parametrize("title,action", [("证券买入-紫金矿业", "buy"), ("证券卖出-紫金矿业", "sell")])
def test_transaction_cards_have_source_date_and_explicit_aggregate_charges(monkeypatch, title, action):
    lines = ["2026-10-28", title, "股票代码 601899", "成交时间 2026-10-28T10:11:12",
             "价格 13.500001", "股数 100", "金额(税费) 1,350.00(5.25)"]
    result = parse(monkeypatch, lines, kind="trade", profile="tonghuashun")
    fields = result["rows"][0]["fields"]
    assert fields["name"] == "紫金矿业" and fields["action"] == action and fields["symbol"] == "601899"
    assert fields["business_date"] == "2026-10-28" and fields["trade_time"] == "10:11:12"
    assert fields["price"] == "13.500001" and fields["quantity"] == "100"
    assert fields["gross_amount"] == "1350.00" and fields["fee_amount"] == "5.25"
    assert fields["fee_scope"] == "total_charges" and fields["tax_amount"] is None and fields["net_cash"] is None
    assert fields["status"] is None and ("status", "missing") in issues(result)


def test_labelled_columns_and_split_chinese_labels(monkeypatch):
    result = parse(monkeypatch, ["基金名称 测试基金", ["份", "额", "持仓成本", "市值"], ["10.1234567", "20.12345", "30.12345"]])
    fields = result["rows"][0]["fields"]
    assert fields["quantity"] == "10.1234567" and fields["cost"] == "20.12345" and fields["market_value"] == "30.12345"


def test_single_label_on_previous_line_and_labelled_date_context(monkeypatch):
    result = parse(monkeypatch, ["持仓日期 2026-10-28", "基金名称 测试基金", "代码", "000001", "份额", "12"])
    assert len(result["rows"]) == 1
    fields = result["rows"][0]["fields"]
    assert fields["symbol"] == "000001" and fields["quantity"] == "12" and fields["business_date"] == "2026-10-28"


def test_ambiguous_column_counts_no_fill_no_calculations(monkeypatch):
    result = parse(monkeypatch, ["股票名称 测试", ["份额", "持仓成本", "市值"], ["10", "20"], "价格 2"])
    fields = result["rows"][0]["fields"]
    assert fields["quantity"] is None and fields["cost"] is None and fields["market_value"] is None
    assert (None, "ambiguous_columns") in issues(result)
    assert fields["business_date"] is None


def test_repeated_legitimate_cards_not_collapsed_and_context_changes(monkeypatch):
    result = parse(monkeypatch, ["2026-10-27", "证券买入-紫金矿业", "金额 100", "流水号 ref-1",
        "证券买入-紫金矿业", "金额 100", "流水号 ref-2", "2026-10-28", "证券买入-紫金矿业", "金额 100"],
        kind="trade", profile="tonghuashun")
    assert len(result["rows"]) == 3
    fields = [row["fields"] for row in result["rows"]]
    assert [f["source_ref"] for f in fields] == ["ref-1", "ref-2", None]
    assert [f["business_date"] for f in fields] == ["2026-10-27", "2026-10-27", "2026-10-28"]


@pytest.mark.parametrize("date", ["今天", "昨天", "10-28", "2026-10-01至2026-10-28", "2026-02-30"])
def test_missing_relative_partial_range_and_invalid_dates_never_use_now(monkeypatch, date):
    result = parse(monkeypatch, [date, "证券买入-紫金矿业", "金额 100"], kind="trade", profile="tonghuashun")
    assert result["rows"][0]["fields"]["business_date"] is None


@pytest.mark.parametrize("label,basis,cost", [("报表成本", "unknown", "100"), ("成本金额", "unknown", "100"),
    ("成本价", None, None), ("摊薄成本价", "diluted_cost", None), ("摊薄成本金额", "diluted_cost", "100")])
def test_cost_types_and_available_quantity_remain_conservative(monkeypatch, label, basis, cost):
    result = parse(monkeypatch, ["基金名称 测试基金", "可用数量 10", f"{label} 100"])
    fields = result["rows"][0]["fields"]
    assert fields["quantity"] is None and fields["cost"] == cost and fields["cost_basis_type"] == basis


@pytest.mark.parametrize("label,scope,amount", [("手续费", "separate", "0"), ("税费", "total_charges", "0"),
    ("税费合计", "total_charges", "0"), ("金额税费total", "total_charges", "0")])
def test_explicit_zero_and_unknown_tax_are_distinct(monkeypatch, label, scope, amount):
    result = parse(monkeypatch, ["证券买入-紫金矿业", f"{label} {amount}"], kind="trade", profile="tonghuashun")
    fields = result["rows"][0]["fields"]
    assert fields["fee_amount"] == amount and fields["fee_scope"] == scope and fields["tax_amount"] is None


@pytest.mark.parametrize("fees", [["税费 2", "手续费 2", "印花税 1"], ["手续费 2", "税费 2", "印花税 1"]])
def test_fee_semantics_conflict_is_not_resolved_by_equal_amounts(monkeypatch, fees):
    result = parse(monkeypatch, ["证券买入-紫金矿业", *fees], kind="trade", profile="tonghuashun")
    fields = result["rows"][0]["fields"]
    assert fields["fee_amount"] is None and fields["tax_amount"] is None and fields["fee_scope"] == "total_charges"
    assert ("fee_amount", "conflicting_values") in issues(result)


def test_ocr_errors_are_not_corrected_or_computed_and_low_confidence_is_not_approval(monkeypatch):
    result = parse(monkeypatch, ["基金名称 测试基金", "代码 O00001", "份额 1O0", "市值 NaN", "持仓成本 1万元", "价格 1.25"], confidence="20")
    fields = result["rows"][0]["fields"]
    assert all(fields[f] is None for f in ("symbol", "quantity", "market_value", "cost", "gross_amount", "net_cash"))
    assert fields["price"] == "1.25" and (None, "low_confidence") in issues(result)
    assert not result["rows"][0]["acknowledged"]


@pytest.mark.parametrize("symbol", ["1", "00001", "0000010", "１２３４５６"])
def test_ocr_never_supplies_missing_code_digits(monkeypatch, symbol):
    result = parse(monkeypatch, ["基金名称 测试基金", f"代码 {symbol}"])
    assert result["rows"][0]["fields"]["symbol"] is None


def test_unrecognized_ocr_retains_original_evidence_without_guessed_rows(monkeypatch):
    result = parse(monkeypatch, ["未知页面 123 456"])
    assert result["rows"] == [] and result["ocr_status"] == "unrecognized" and result["words"] and result["text"]
    result = parse(monkeypatch, [])
    assert result["rows"] == [] and result["ocr_status"] == "empty"


@pytest.mark.parametrize("format", ["PNG", "JPEG"])
def test_local_command_stdin_languages_timeout_and_immutable_input(monkeypatch, format):
    content = image_bytes(format)
    original = content[:]
    calls = install_engine(monkeypatch, ["基金名称 测试基金", "份额 10"])
    result = ocr.parse_image(content, "tencent_licaitong", "holding")
    assert content == original and result["rows"]
    command, stdin, timeout = calls[-1]
    assert command == ["C:/local/tesseract.exe", "stdin", "stdout", "-l", "chi_sim+eng", "--psm", "6", "tsv"]
    assert stdin is content and timeout == 20 and calls[0][2] == 3


def test_runner_uses_no_shell_bounded_timeout_and_thread_count_without_mutating_env(monkeypatch):
    before = dict(os.environ)
    seen = []

    def run(command, **kwargs):
        seen.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=b"tsv", stderr=b"")

    monkeypatch.setattr(ocr.subprocess, "run", run)
    assert ocr._run(["tesseract", "stdin"], content=b"png", timeout=20) == b"tsv"
    assert seen[0][1]["shell"] is False and seen[0][1]["timeout"] == 20
    assert seen[0][1]["env"]["OMP_THREAD_LIMIT"] == "1" and seen[0][1]["input"] == b"png"
    assert dict(os.environ) == before


def test_available_requires_both_languages_and_handles_probe_failures(monkeypatch):
    monkeypatch.setattr(ocr.shutil, "which", lambda name: None)
    assert ocr.ocr_available() is False
    monkeypatch.setattr(ocr.shutil, "which", lambda name: "tesseract")
    monkeypatch.setattr(ocr, "_run", lambda *a, **kw: b"eng\n")
    assert ocr.ocr_available() is False
    monkeypatch.setattr(ocr, "_run", lambda *a, **kw: b"chi_sim\neng\n")
    assert ocr.ocr_available() is True

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("tesseract", 3)

    monkeypatch.setattr(ocr, "_run", timeout)
    assert ocr.ocr_available() is False


@pytest.mark.parametrize("error,status", [(subprocess.TimeoutExpired("tesseract", 20), "timeout"),
    (RuntimeError("failed"), "failed"), (OSError("missing"), "failed")])
def test_recognizer_failure_returns_no_partial_guesses(monkeypatch, error, status):
    monkeypatch.setattr(ocr, "_engine", lambda: "tesseract")

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(ocr, "_run", fail)
    result = ocr.parse_image(image_bytes(), "tonghuashun", "trade")
    assert result["rows"] == [] and result["ocr_status"] == status and result["warnings"]


def test_missing_engine_returns_explicit_unavailable(monkeypatch):
    monkeypatch.setattr(ocr, "_engine", lambda: None)
    result = ocr.parse_image(image_bytes(), "tonghuashun", "trade")
    assert result["ocr_status"] == "unavailable" and result["rows"] == []


@pytest.mark.parametrize("content", [b"", b"not an image", b"x" * (10 * 1024**2 + 1)], ids=["empty", "bad", "oversized"])
def test_bad_images_rejected_before_any_engine_call(monkeypatch, content):
    monkeypatch.setattr(ocr, "_engine", lambda: pytest.fail("Invalid image reached OCR"))
    with pytest.raises(ValueError):
        ocr.parse_image(content, "tonghuashun", "holding")


def test_pixel_frame_format_and_truncation_limits(monkeypatch):
    assert ocr.MAX_PIXELS == 20_000_000 and ocr.MAX_BYTES == 10 * 1024**2
    monkeypatch.setattr(ocr, "_engine", lambda: pytest.fail("Invalid image reached OCR"))
    png = image_bytes()
    ihdr = struct.pack(">II", 5000, 4001) + png[24:29]
    oversized = png[:16] + ihdr + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr)) + png[33:]
    animation = BytesIO()
    first, second = Image.new("RGB", (2, 2), "white"), Image.new("RGB", (2, 2), "black")
    first.save(animation, format="PNG", save_all=True, append_images=[second], duration=100)
    first.close()
    second.close()
    for content in (oversized, animation.getvalue(), image_bytes("GIF"), image_bytes("JPEG")[:-30], png[:-20]):
        with pytest.raises(ValueError):
            ocr.parse_image(content, "tonghuashun", "holding")


@pytest.mark.parametrize("data", [b"bad TSV", b"\xff", tsv_bytes(["基金名称 测试基金"], confidence="NaN"),
    tsv_bytes(["基金名称 测试基金"], confidence="1e-999999"), tsv_bytes(["基金名称 测试基金"], confidence="101")],
    ids=["headers", "encoding", "nan", "exponent", "range"])
def test_malformed_tsv_and_confidence_return_failed_without_candidates(monkeypatch, data):
    install_engine(monkeypatch, data=data)
    result = ocr.parse_image(image_bytes(), "tencent_licaitong", "holding")
    assert result["ocr_status"] == "failed" and result["rows"] == []


def test_tsv_boxes_words_rows_output_and_candidate_bounds(monkeypatch):
    data = tsv_bytes(["基金名称 测试基金", "份额 10"])
    for limit, value in (("MAX_WORDS", 1), ("MAX_TSV_ROWS", 1), ("MAX_OUTPUT", 50)):
        with monkeypatch.context() as patch:
            patch.setattr(ocr, limit, value)
            install_engine(patch, data=data)
            result = ocr.parse_image(image_bytes(), "tencent_licaitong", "holding")
            assert result["rows"] == [] and result["ocr_status"] == "failed"
    install_engine(monkeypatch, data=data.replace(b"\t150\t15\t", b"\t900\t15\t"))
    assert ocr.parse_image(image_bytes(), "tencent_licaitong", "holding")["ocr_status"] == "failed"
    monkeypatch.setattr(ocr, "MAX_ROWS", 1)
    result = parse(monkeypatch, ["证券买入-测试", "金额 10", "证券买入-测试", "金额 10"], kind="trade")
    assert result["rows"] == [] and result["ocr_status"] == "failed"


@pytest.mark.parametrize("profile,kind", [("other", "trade"), ("tonghuashun", "other")])
def test_unknown_profile_and_kind_rejected(profile, kind):
    with pytest.raises(ValueError):
        ocr.parse_image(b"bad", profile, kind)


def test_unit_annotations_columns_and_cost_basis_do_not_contaminate(monkeypatch):
    result = parse(monkeypatch, ["基金名称 测试基金", ["份额（份）", "持仓成本（元）", "净值（元/份）"], ["10", "20", "2"], "摊薄成本价 1"])
    fields = result["rows"][0]["fields"]
    assert fields["quantity"] == "10" and fields["cost"] == "20" and fields["price"] == "2"
    assert fields["cost_basis_type"] == "holding_cost"


def test_conflicting_asset_labels_stay_unknown_and_repeated_title_name_is_not_new_card(monkeypatch):
    result = parse(monkeypatch, ["证券买入-紫金矿业", "股票名称 紫金矿业", "基金代码 000001", "股票代码 601899", "金额 10"], kind="trade", profile="tonghuashun")
    assert len(result["rows"]) == 2  # The second explicit code starts a distinct incomplete draft.
    assert result["rows"][0]["fields"]["asset_type"] is None
    assert ("asset_type", "conflicting_values") in issues(result)


def test_runner_rejects_nonzero_and_excessive_output(monkeypatch):
    for process in (SimpleNamespace(returncode=1, stdout=b"", stderr=b"error"),
                    SimpleNamespace(returncode=0, stdout=b"x" * 5, stderr=b"")):
        monkeypatch.setattr(ocr.subprocess, "run", lambda *a, result=process, **kw: result)
        monkeypatch.setattr(ocr, "MAX_OUTPUT", 4)
        with pytest.raises((RuntimeError, ValueError)):
            ocr._run(["tesseract"], timeout=20)


def test_labelled_holding_table_with_name_column(monkeypatch):
    result = parse(monkeypatch, [["基金名称", "基金代码", "份额", "市值"], ["测试基金", "000001", "10", "20"]])
    fields = result["rows"][0]["fields"]
    assert fields["name"] == "测试基金" and fields["symbol"] == "000001"
    assert fields["quantity"] == "10" and fields["market_value"] == "20"


def spatial_tsv(bands, *, split_lines=False):
    stream = StringIO(newline="")
    writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
    writer.writerow(["level", "page_num", "block_num", "par_num", "line_num", "word_num", "left", "top", "width", "height", "conf", "text"])
    serial = 0
    for line, (top, cells) in enumerate(bands, 1):
        for number, cell in enumerate(cells, 1):
            text, left, width, *height = cell
            serial += 1
            writer.writerow([5, 1, 1, 1, serial if split_lines else line, number, left, top, width, height[0] if height else 18, "94.000000", text])
    return stream.getvalue().encode("utf-8")


def trade_bands(*, month="2026-06", amount_header="金额/税费", rows=6):
    bands = [
        (80, [("资产明细2026年04月01日", 20, 220)]),
        (110, [("盈亏", 20, 45), ("10,660.20", 180, 120)]),
        (200, [("本月操作", 20, 100), ("价格/数量", 292, 92), (amount_header, 449, 100)]),
    ]
    if month:
        bands.append((240, [("^", 20, 15), (month, 65, 90), ("+14,987.32", 185, 105), ("+3.94%", 320, 80)]))
    for index in range(rows):
        top = 300 + index * 90
        direction = "买入" if index % 2 == 0 else "证券卖出"
        amount = "-18554.83" if index == 2 else "27540.00"
        bands.extend([
            (top, [(f"{direction}-测试标的{300 + index}", 24, 210), ("137.700", 310, 85), (amount, 460, 110)]),
            (top + 32, [("买" if index % 2 == 0 else "卖", 24, 15), ("06-22", 52, 60), ("10:06", 120, 55),
                        ("200", 355, 40), ("13.77", 500, 60)]),
        ])
    return bands


def parse_spatial(monkeypatch, bands, *, split_lines=False):
    data = spatial_tsv(bands, split_lines=split_lines)
    install_engine(monkeypatch, data=data)
    result = ocr.parse_image(image_bytes(size=(600, 1000)), "tonghuashun", "trade")
    assert result["tsv"] == data.decode("utf-8")
    return result


@pytest.mark.parametrize("split_lines", [False, True])
def test_generic_two_line_trade_table_six_rows_not_header_orphan(monkeypatch, split_lines):
    result = parse_spatial(monkeypatch, trade_bands(), split_lines=split_lines)
    assert result["ocr_status"] == "ok" and len(result["rows"]) == 6
    for index, row in enumerate(result["rows"]):
        fields = row["fields"]
        assert fields["name"] == f"测试标的{300 + index}"  # Numeric name suffix is not a price.
        assert fields["price"] == "137.700" and fields["quantity"] == "200"
        assert fields["fee_amount"] == "13.77" and fields["fee_scope"] == "total_charges" and fields["tax_amount"] is None
        assert fields["business_date"] == "2026-06-22" and fields["trade_time"] == "10:06:00"
        assert fields["action"] == ("buy" if index % 2 == 0 else "sell")
        assert fields["gross_amount"] is None and fields["net_cash"] is None
        assert fields["symbol"] is None and fields["status"] is None
        assert not row["acknowledged"] and not row["require_review"]
        amount = row["source"]["amount_candidates"][0]
        assert amount["value"] == ("-18554.83" if index == 2 else "27540.00") and amount["semantics"] == "unknown"
        assert ("gross_amount", "amount_semantics_ambiguous") in {(i["field"], i["code"]) for i in row["issues"]}
        assert row["source"]["context"]["year_month"] == "2026-06"
        assert any("2026-06" in word["text"] for word in row["source"]["words"])
        assert all(result["words"][i]["text"] in row["source"]["raw_text"] for i in amount["word_ids"])


@pytest.mark.parametrize("month", [None, "2026-05", "2026-13"])
def test_table_never_borrows_summary_year_or_mismatched_month(monkeypatch, month):
    result = parse_spatial(monkeypatch, trade_bands(month=month, rows=1))
    fields = result["rows"][0]["fields"]
    assert fields["business_date"] is None and fields["trade_time"] == "10:06:00"
    assert ("business_date", "missing_date_context") in issues(result)


def test_table_month_before_header_and_year_rollover_require_explicit_groups(monkeypatch):
    bands = trade_bands(month=None, rows=1)
    bands.insert(2, (170, [("2025-12", 65, 90)]))
    bands[-1][1][1] = ("12-31", 52, 60)
    bands.extend([(400, [("2026-01", 65, 90)]),
                  (450, [("买入-另一标的", 24, 180), ("10.10", 330, 65), ("-1011.00", 460, 110)]),
                  (482, [("01-02", 52, 60), ("09:31", 120, 55), ("100", 355, 40), ("1.00", 510, 50)])])
    result = parse_spatial(monkeypatch, bands)
    assert [row["fields"]["business_date"] for row in result["rows"]] == ["2025-12-31", "2026-01-02"]


@pytest.mark.parametrize("header,field", [("成交金额/税费", "gross_amount"), ("净收付/税费", "net_cash")])
def test_only_explicit_amount_header_establishes_semantics(monkeypatch, header, field):
    result = parse_spatial(monkeypatch, trade_bands(amount_header=header, rows=1))
    fields = result["rows"][0]["fields"]
    assert fields[field] == "27540.00"
    assert fields["net_cash" if field == "gross_amount" else "gross_amount"] is None
    assert not result["rows"][0]["source"]["amount_candidates"]


def test_missing_table_cells_never_shift_next_card_values(monkeypatch):
    bands = trade_bands(rows=2)
    bands[4][1].pop(1)  # First card has no price.
    bands[5][1].pop()  # First card has no charges.
    result = parse_spatial(monkeypatch, bands)
    one, two = (row["fields"] for row in result["rows"])
    assert one["price"] is None and one["fee_amount"] is None
    assert two["price"] == "137.700" and two["fee_amount"] == "13.77"


def test_split_header_tokens_sign_and_date_time_tokens_keep_geometry(monkeypatch):
    bands = trade_bands(rows=1)
    bands[2] = (200, [("价格", 292, 35), ("/", 330, 5), ("数量", 340, 40),
                      ("金额", 449, 35), ("/", 486, 5), ("税费", 495, 40)])
    bands[4] = (300, [("证券买入-泛用名称", 24, 210), ("137.700", 310, 85), ("-", 450, 8), ("18554.83", 462, 105)])
    bands[5] = (332, [("06-2210:06", 52, 125), ("200", 355, 40), ("0.00", 500, 60)])
    result = parse_spatial(monkeypatch, bands, split_lines=True)
    row = result["rows"][0]
    assert row["fields"]["name"] == "泛用名称" and row["fields"]["fee_amount"] == "0.00"
    assert row["fields"]["business_date"] == "2026-06-22"
    assert row["source"]["amount_candidates"][0]["value"] == "-18554.83" and row["fields"]["net_cash"] is None


def test_trade_table_header_only_and_empty_label_values_emit_no_drafts(monkeypatch):
    result = parse_spatial(monkeypatch, trade_bands(rows=0))
    assert result["rows"] == []
    result = parse(monkeypatch, ["价格 / 数量 金额 / 税费"], kind="trade")
    assert result["rows"] == []


@pytest.mark.parametrize("split_lines", [False, True])
def test_actual_layout_thin_hyphens_tall_characters_and_badge_noise(monkeypatch, split_lines):
    bands = trade_bands(rows=6)
    for index, badge in enumerate(["%", "=", "2", "%", "=", "2"]):
        bands[5 + index * 2][1][0] = (badge, 28, 13, 14)
    bands[5][1][-1] = ("135.77", 500, 60)  # Preserve an OCR digit error, never infer 13.77.
    extras = []
    for index in (2, 5):
        top, cells = bands[4 + index * 2]
        direction = "买入" if index % 2 == 0 else "卖出"
        cells[:] = [("证券", 24, 50, 22), (direction, 76, 40, 39), ("测试", 139, 45, 21),
                    (f"科技{300 + index}", 185, 65, 21), *cells[1:]]
        extras.append((top + 17, [("-", 120, 8, 2)]))
    result = parse_spatial(monkeypatch, bands + extras, split_lines=split_lines)
    assert len(result["rows"]) == 6
    assert result["rows"][2]["fields"]["name"] == "测试科技302"
    assert result["rows"][5]["fields"]["name"] == "测试科技305"
    for index, row in enumerate(result["rows"]):
        fields = row["fields"]
        assert fields["business_date"] == "2026-06-22" and fields["trade_time"] == "10:06:00"
        assert fields["quantity"] == "200" and fields["price"] == "137.700"
        assert fields["fee_amount"] == ("135.77" if index == 0 else "13.77")
        assert fields["tax_amount"] is None and fields["net_cash"] is None and fields["gross_amount"] is None
    assert not any(w["code"] in {"unrecognized_trade_title", "orphan_trade_detail"} for w in result["warnings"])


def test_unrecognized_table_title_retained_as_warning_and_detail_not_misattributed(monkeypatch):
    bands = trade_bands(rows=3)
    _top, cells = bands[6]
    cells[0] = ("证券卖出 未识别分隔符", 24, 210)
    bands[7][1][-2] = ("999", 355, 40)
    result = parse_spatial(monkeypatch, bands)
    assert len(result["rows"]) == 2
    assert all(row["fields"]["quantity"] == "200" for row in result["rows"])
    warnings = {w["code"]: w for w in result["warnings"]}
    assert {"unrecognized_trade_title", "orphan_trade_detail"} <= set(warnings)
    warning = warnings["unrecognized_trade_title"]
    assert "未识别分隔符" in warning["source"]["raw_text"]
    assert all(result["words"][i]["box"] for i in warning["source"]["word_ids"])
