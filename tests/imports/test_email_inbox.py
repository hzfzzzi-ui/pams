"""Unit tests for Gmail IMAP bill fetcher and Guangfa HTML e-bill parser."""

from email.message import EmailMessage

from domain.imports.account_import.email_inbox import (
    extract_bill_artifacts,
    fetch_gmail_bill_artifacts,
    parse_guangfa_html_ebill,
)
from domain.imports.account_import.inbox_router import route_file_to_accounts


SAMPLE_GUANGFA_HTML = """
<html><body>
<p>以下是您2026年11月的信用卡账单：</p>
<table>
  <tr><td>卡号：5369********9711</td></tr>
  <tr><td>交易日期</td><td>入账日期</td><td>交易摘要</td><td>交易金额</td><td>交易货币</td><td>入账金额</td><td>入账货币</td></tr>
  <tr><td>2026/10/31</td><td>2026/11/02</td><td>(消费)DLO*SERV UBER EATS CA</td><td>552.13</td><td>墨西哥比索</td><td>29.41</td><td>美元</td></tr>
  <tr><td>2026/10/30</td><td>2026/10/30</td><td>(还款)MC rebate USD1</td><td>-1.00</td><td>美元</td><td>-1.00</td><td>美元</td></tr>
  <tr><td>卡号：6225********7443</td></tr>
  <tr><td>2026/10/25</td><td>2026/10/26</td><td>(消费)美团外卖</td><td>45.50</td><td>人民币</td><td>45.50</td><td>人民币</td></tr>
</table>
</body></html>
"""


def test_parse_guangfa_html_ebill_splits_7443_and_9711():
    parsed = parse_guangfa_html_ebill(SAMPLE_GUANGFA_HTML)
    assert set(parsed.keys()) == {"gf_7443", "gf_9711"}
    csv_9711 = parsed["gf_9711"].decode("utf-8")
    assert "business_date,action,currency,net_cash,note" in csv_9711
    assert "2026-10-31,expense,USD,-29.41,(消费)DLO*SERV UBER EATS CA [552.13 墨西哥比索]" in csv_9711
    assert "2026-10-30,transfer,USD,1.00,(还款)MC rebate USD1" in csv_9711
    csv_7443 = parsed["gf_7443"].decode("utf-8")
    assert "2026-10-25,expense,CNY,-45.50,(消费)美团外卖" in csv_7443


def test_extract_bill_artifacts_handles_guangfa_html_and_santander_pdf():
    gf_msg = EmailMessage()
    gf_msg["From"] = "creditcard@cgbchina.com.cn"
    gf_msg["Subject"] = "广发信用卡 2026年11月电子账单"
    gf_msg.add_alternative(SAMPLE_GUANGFA_HTML, subtype="html")

    gf_artifacts = extract_bill_artifacts(gf_msg.as_bytes())
    names = {a["filename"] for a in gf_artifacts}
    assert names == {"2026-11_广发7443电子账单.csv", "2026-11_广发9711电子账单.csv"}
    assert route_file_to_accounts("2026-11_广发7443电子账单.csv").accounts == ("gf_7443",)
    assert route_file_to_accounts("2026-11_广发9711电子账单.csv").accounts == ("gf_9711",)

    san_msg = EmailMessage()
    san_msg["From"] = "notificaciones@santander.com.mx"
    san_msg["Subject"] = "Estado de Cuenta Santander Integral (PDF y XML)"
    san_msg.set_content("Envio de Estado de Cuenta")
    san_msg.add_attachment(b"%PDF-1.4 santander", maintype="application", subtype="pdf", filename="2026-10_Santander.pdf")
    san_artifacts = extract_bill_artifacts(san_msg.as_bytes())
    assert len(san_artifacts) == 1
    assert san_artifacts[0]["filename"] == "2026-10_Santander.pdf"


def test_fetch_gmail_bill_artifacts_skips_when_unconfigured(monkeypatch):
    monkeypatch.delenv("PAMS_IMAP_USER", raising=False)
    monkeypatch.delenv("PAMS_IMAP_PASSWORD", raising=False)
    res = fetch_gmail_bill_artifacts()
    assert res["configured"] is False
    assert res["skipped"] is True
