"""Unit tests for daily-once IMAP bill watcher (offline MIME extraction and SHA256 dedup)."""

import importlib.util
from email.message import EmailMessage
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[2] / "backend" / "scheduler" / "imap_bill_watcher.py"
spec = importlib.util.spec_from_file_location("imap_bill_watcher", MODULE_PATH)
imap_mod = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(imap_mod)


def test_poll_imap_bills_skips_when_unconfigured(monkeypatch):
    monkeypatch.delenv("PAMS_IMAP_HOST", raising=False)
    monkeypatch.delenv("PAMS_IMAP_USER", raising=False)
    monkeypatch.delenv("PAMS_IMAP_PASSWORD", raising=False)
    result = imap_mod.poll_imap_bills()
    assert result["skipped"] is True
    assert result["reason"] == "imap_not_configured"


def test_extract_and_save_new_attachments_deduplicates(tmp_path: Path):
    msg = EmailMessage()
    msg["Subject"] = "广发银行2026年11月对账单"
    msg["From"] = "creditcard@cgbchina.com.cn"
    msg.set_content("请查收附件")
    msg.add_attachment(
        b"%PDF-1.4 sample guangfa",
        maintype="application",
        subtype="pdf",
        filename="2026-11_广发银行信用卡.pdf",
    )
    msg.add_attachment(
        b"ignore me",
        maintype="text",
        subtype="plain",
        filename="readme.txt",
    )

    extracted = imap_mod.extract_attachments(msg.as_bytes())
    assert len(extracted) == 1
    assert extracted[0][0] == "2026-11_广发银行信用卡.pdf"

    inbox_dir = tmp_path / "_Inbox"
    state_file = tmp_path / "imap_seen.json"
    first = imap_mod.save_new_attachments(extracted, inbox_dir=inbox_dir, state_file=state_file)
    assert first == ["2026-11_广发银行信用卡.pdf"]
    assert (inbox_dir / "2026-11_广发银行信用卡.pdf").exists()

    second = imap_mod.save_new_attachments(extracted, inbox_dir=inbox_dir, state_file=state_file)
    assert second == []
