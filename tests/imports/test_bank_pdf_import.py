"""Bank PDFs share the source draft, cash preview and publication contract."""
from contextlib import contextmanager
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

from PIL import Image
import psycopg2
import pytest
from psycopg2.extras import RealDictCursor

from tests.imports.test_source_record_http import SchemaCursor
from tests.imports.test_redesigned_fact_schema import install_redesign
from tests.support.account_import_support import scratch_database
from collectors import bank_statement_parser, santander_statement_pdf as santander
from domain.imports import account_import_statement, document_stage_v2, source_records_v2
from domain.imports.cash_preview_v2 import preview_cash


CHEQUES_PAGE = """PERIODO DEL 01-ENE-2026 AL 31-ENE-2026
SUPERCUENTA CHEQUES-SALDO PROM 11-00000420-9   100.00 100.00%   75.00 100.00%
SUPERCUENTA CHEQUES-SALDO PROM 11-00000420-9
SALDO FINAL DEL PERIODO ANTERIOR: $100.00
FECHA FOLIO DESCRIPCIÓN DEPOSITO RETIRO SALDO
02-ENE-2026|0000001 Transferencia   25.00   125.00
02-ENE-2026 0000002 Aviso            0.00   125.00
"""
CONTINUATION = """PERIODO DEL 01-ENE-2026 AL 31-ENE-2026
FECHA | FOLIO | DESCRIPCIÓN DEPOSITO RETIRO SALDO
03-ENE-2026 Compra de prueba          50.00    75.00
TOTAL                     25.00       50.00
Detalles de movimientos Dinero Creciente Santander.
03-ENE-2026 0000003 Movimiento de otra cuenta  50.00 50.00
"""


def test_santander_rejects_amount_gaps_and_wrong_account():
    profile = {"profile_code": "santander", "account_code": "santander", "account_tail": "4209"}

    def parse(page=CHEQUES_PAGE, continuation=CONTINUATION):
        return santander._parse_pages([(1, page, True), (2, continuation, True)], Path("test.pdf"), profile)

    assert [r["amount"] for r in parse().rows] == [Decimal("25.00"), Decimal("-50.00")]
    with pytest.raises(ValueError, match="逐笔余额"):
        parse(continuation=CONTINUATION.replace("75.00", "74.00"))
    with pytest.raises(ValueError, match="小计"):
        parse(continuation=CONTINUATION.replace("25.00", "26.00"))
    with pytest.raises(ValueError, match="期初或期末余额"):
        parse(page=CHEQUES_PAGE.replace("75.00 100.00%", "76.00 100.00%"))
    with pytest.raises(ValueError, match="账户号"):
        parse(page=CHEQUES_PAGE.replace("420-9", "421-9"))


@pytest.mark.parametrize("account,currency,amounts,method", [
    ("gf_7443", "CNY", [-100], "template"),
    ("gf_9711", "USD", [20], "template"),
    ("santander", "MXN", [25, -50], "ocr"),
])
def test_bank_pdf_stages_source_and_publishes_only_cash(monkeypatch, account, currency, amounts, method):
    profile = {"profile_code": account, "account_code": account, "currency": currency}
    if account == "santander":
        profile.update(statement_template="santander_mx_account_csv", account_tail="4209")
        ocr_pages = iter((CHEQUES_PAGE, CONTINUATION))
        monkeypatch.setattr(santander, "_ocr_page", lambda *_: next(ocr_pages))
    else:
        tail = account.split("_")[1]
        profile.update(statement_template="guangfa_credit_pdf", card_tail=tail)
        text = ("账单周期 2026/01/01 - 2026/01/31\n卡号：1234********7443\n"
                "2026/01/02 2026/01/03 100.00 人民币 100.00 人民币\n卡号：1234********9711\n"
                "2026/01/02 2026/01/04 (退货)示例 -140.00 墨西哥比索 -20.00 美元\n")

        @contextmanager
        def mock_pdf(*args, **kwargs):
            yield SimpleNamespace(pages=[SimpleNamespace(extract_text=lambda: text,
                extract_tables=lambda: [[[tail, "0.00"]]])])

        monkeypatch.setattr(bank_statement_parser.pdfplumber, "open", mock_pdf)
    monkeypatch.setattr(account_import_statement, "profiles", lambda *_: {account: [profile]})
    pdf = BytesIO()
    page = Image.new("RGB", (60, 60), color="white")
    page.save(pdf, "PDF", save_all=True, append_images=[page])

    with scratch_database() as params, psycopg2.connect(**params) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        install_redesign(cur)
        cur.execute("INSERT INTO redesign_test.pams_accounts VALUES (%s)", (account,))
        db = SchemaCursor(cur)
        staged = document_stage_v2.stage_document(db, account_code=account, source_name="synthetic",
                                                   filename="statement.pdf", content=pdf.getvalue())
        assert staged["excluded_count"] == 1
        source_id, revision = UUID(staged["source_id"]), staged["revision"]
        assert preview_cash(db, source_id=source_id, expected_revision=revision)["counts"]["pending"] == len(amounts)
        cur.execute("""SELECT source_item_id,parsed_fields,parse_method,source_position,raw_fields
            FROM redesign_test.source_items WHERE source_id=%s AND review_status='pending'""", (str(source_id),))
        for item in cur.fetchall():
            assert item["parse_method"] == method
            evidence = item["raw_fields"]["source"]["statement_row"]
            if account == "santander":
                assert item["source_position"]["page"] in (1, 2)
            if account == "gf_9711":
                assert (evidence["original_currency"], evidence["original_amount"]) == ("MXN", "-140.00")
            revision = source_records_v2.review_item(db, source_id=source_id, item_id=item["source_item_id"],
                expected_revision=revision, parsed_fields=item["parsed_fields"], review_status="confirmed")
        assert preview_cash(db, source_id=source_id, expected_revision=revision)["can_publish"]
        result = source_records_v2.publish_cash_source(db, source_id=source_id, expected_revision=revision)
        assert result["published"] == len(amounts)
        cur.execute("""SELECT f.amount,f.currency,f.posting_date,c.status
            FROM redesign_test.pams_cashflow_fact f JOIN redesign_test.cashflow_classification c USING(cashflow_id)
            WHERE f.account_code=%s""", (account,))
        facts = cur.fetchall()
        assert sorted(f["amount"] for f in facts) == sorted(amounts)
        assert {(f["currency"], f["status"]) for f in facts} == {(currency, "pending")}
        expected_posting = {"gf_7443": "2026-01-03", "gf_9711": "2026-01-04"}.get(account)
        assert {f["posting_date"].isoformat() if f["posting_date"] else None for f in facts} == {expected_posting}


def test_cash_distinct_review_does_not_bypass_existing_facts():
    source = source_records_v2
    with scratch_database() as params, psycopg2.connect(**params) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        install_redesign(cur)
        db = SchemaCursor(cur)
        fields = {"business_date": "2026-10-26", "net_cash": "1.00", "currency": "USD"}

        def draft(account, content):
            sid = uuid4()
            source.create_source(db, source_id=sid, account_code=account, source_name="synthetic",
                                 acquisition_method="file", document_type="cash", filename="test.pdf",
                                 file_format="pdf", content=content)
            item_ids = [uuid4(), uuid4()]
            for idx, item in enumerate(item_ids, 1):
                source.add_item(db, source_id=sid, item_id=item, parse_method="template",
                    source_position={"row": idx}, raw_fields={"source": {"statement": True, "row": idx}},
                    parsed_fields=fields)
            return sid, item_ids

        def review(sid, item, revision, distinct):
            return source.review_item(db, source_id=sid, item_id=item, expected_revision=revision,
                parsed_fields={**fields, **({"duplicate_decision": "distinct"} if distinct else {})},
                review_status="confirmed")

        sid, (first, second) = draft("bank-a", b"synthetic original")
        revision = review(sid, first, 3, True)
        preview = preview_cash(db, source_id=sid, expected_revision=revision)
        assert not preview["can_publish"] and preview["counts"]["duplicate"] == 1
        assert all(row["issues"][0]["code"] == "possible_duplicate" for row in preview["rows"])
        revision = review(sid, second, revision, True)
        assert preview_cash(db, source_id=sid, expected_revision=revision)["counts"]["ready"] == 2
        assert source.publish_cash_source(db, source_id=sid, expected_revision=revision)["published"] == 2

        other, (first, second) = draft("bank-a", b"another synthetic original")
        revision = review(other, first, 3, True)
        revision = review(other, second, revision, True)
        preview = preview_cash(db, source_id=other, expected_revision=revision)
        assert not preview["can_publish"] and preview["counts"]["duplicate"] == 2
        assert all("issues" not in row for row in preview["rows"])
