"""Gmail IMAP bill fetcher with attachment extraction and Guangfa HTML e-bill parsing.

Pure stdlib implementation (imaplib, email, html.parser, csv).
Safe by default: returns skipped=True unless PAMS_IMAP_USER and PAMS_IMAP_PASSWORD are set.
Never writes directly to production financial tables; returns normalized artifacts for draft staging.
"""

from __future__ import annotations

import csv
import email
import hashlib
import imaplib
import io
import os
import re
from datetime import date, timedelta
from email.header import decode_header
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

SUPPORTED_ATTACHMENT_EXTS = {".pdf", ".xls", ".xlsx", ".csv", ".tsv", ".zip"}
DEFAULT_IMAP_HOST = "imap.gmail.com"
DEFAULT_IMAP_PORT = 993
DEFAULT_IMAP_FOLDER = "INBOX"
DEFAULT_IMAP_SENDERS = (
    "service@vip.ccb.com",
    "creditcard@cgbchina.com.cn",
    "notificaciones@santander.com.mx",
)
SAFE_NAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
DATE_RE = re.compile(r"^\d{4}/\d{2}/\d{2}$")
CARD_HEADER_RE = re.compile(r"卡号[：:]\s*[0-9*]+(\d{4})")
GUANGFA_CARD_ACCOUNT = {"7443": "gf_7443", "9711": "gf_9711"}
CURRENCY_ZH_MAP = {
    "人民币": "CNY",
    "美元": "USD",
    "港元": "HKD",
    "港币": "HKD",
    "墨西哥比索": "MXN",
    "欧元": "EUR",
    "日元": "JPY",
    "英镑": "GBP",
}


class _TextLineExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"tr", "td", "th", "p", "div", "br", "li"}:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if data:
            self._chunks.append(data)

    def lines(self) -> list[str]:
        return [line.strip() for line in "".join(self._chunks).splitlines() if line.strip()]


def _decode_header_text(raw: str | None) -> str:
    if not raw:
        return ""
    parts = decode_header(raw)
    out: list[str] = []
    for data, charset in parts:
        if isinstance(data, bytes):
            out.append(data.decode(charset or "utf-8", errors="replace"))
        else:
            out.append(str(data))
    return "".join(out).strip()


def _safe_filename(name: str) -> str:
    return SAFE_NAME_RE.sub("_", name.strip())[:160]


def _extract_year_month(subject: str, html_text: str) -> str:
    for text in (subject, html_text[:2000]):
        m = re.search(r"(20\d{2})\s*年\s*(\d{1,2})\s*月", text)
        if m:
            return f"{m.group(1)}-{int(m.group(2)):02d}"
    return date.today().strftime("%Y-%m")


def parse_guangfa_html_ebill(html_text: str) -> dict[str, bytes]:
    """Parse Guangfa monthly HTML e-bill into per-card UTF-8 CSV bytes keyed by account_code."""
    parser = _TextLineExtractor()
    parser.feed(html_text)
    lines = parser.lines()

    rows_by_account: dict[str, list[list[str]]] = {}
    current_account: str | None = None
    idx = 0
    total = len(lines)

    while idx < total:
        line = lines[idx]
        card_match = CARD_HEADER_RE.search(line)
        if card_match:
            tail = card_match.group(1)
            current_account = GUANGFA_CARD_ACCOUNT.get(tail)
            idx += 1
            continue

        if (
            current_account
            and DATE_RE.match(line)
            and idx + 6 < total
            and DATE_RE.match(lines[idx + 1])
        ):
            tx_date = lines[idx].replace("/", "-")
            summary = lines[idx + 2]
            tx_amount = lines[idx + 3].replace(",", "")
            tx_currency = lines[idx + 4]
            post_amount_raw = lines[idx + 5].replace(",", "")
            post_currency_raw = lines[idx + 6]
            try:
                post_val = float(post_amount_raw)
            except ValueError:
                idx += 7
                continue
            # Credit card bill: positive posted amount = spending (expense, negative net_cash);
            # negative posted amount = repayment / rebate (positive net_cash).
            net_cash = -post_val
            if post_val > 0:
                action = "expense"
            elif "还款" in summary:
                action = "transfer"
            else:
                action = "income"
            currency = CURRENCY_ZH_MAP.get(post_currency_raw, post_currency_raw.upper())
            note = summary
            if tx_currency and tx_currency != post_currency_raw:
                note = f"{summary} [{tx_amount} {tx_currency}]"
            rows_by_account.setdefault(current_account, []).append(
                [tx_date, action, currency, f"{net_cash:.2f}", note]
            )
            idx += 7
            continue

        idx += 1

    csv_by_account: dict[str, bytes] = {}
    for account_code, tx_rows in rows_by_account.items():
        if not tx_rows:
            continue
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["business_date", "action", "currency", "net_cash", "note"])
        writer.writerows(tx_rows)
        csv_by_account[account_code] = buf.getvalue().encode("utf-8")
    return csv_by_account


def extract_bill_artifacts(raw_message: bytes) -> list[dict[str, Any]]:
    """Extract bill attachments and supported HTML e-bills from one RFC822 message."""
    msg = email.message_from_bytes(raw_message)
    sender = _decode_header_text(msg.get("From")).lower()
    subject = _decode_header_text(msg.get("Subject"))
    artifacts: list[dict[str, Any]] = []
    html_bodies: list[str] = []

    for part in msg.walk():
        if part.is_multipart():
            continue
        raw_name = _decode_header_text(part.get_filename())
        if raw_name:
            filename = _safe_filename(raw_name)
            ext = Path(filename).suffix.lower()
            if ext in SUPPORTED_ATTACHMENT_EXTS:
                payload = part.get_payload(decode=True)
                if payload:
                    artifacts.append({
                        "filename": filename,
                        "content": payload,
                        "sha256": hashlib.sha256(payload).hexdigest(),
                        "origin": "email_attachment",
                        "subject": subject,
                        "account_hint": None,
                    })
            continue

        if part.get_content_type() == "text/html":
            payload = part.get_payload(decode=True)
            if payload:
                charset = part.get_content_charset() or "utf-8"
                for enc in (charset, "gb18030", "utf-8"):
                    try:
                        html_bodies.append(payload.decode(enc))
                        break
                    except (LookupError, UnicodeDecodeError):
                        continue

    # Guangfa monthly auto e-bills arrive as HTML tables with no PDF attachment
    if not artifacts and ("cgbchina.com.cn" in sender or "广发" in subject) and "电子账单" in subject:
        combined_html = "\n".join(html_bodies)
        ym = _extract_year_month(subject, combined_html)
        parsed = parse_guangfa_html_ebill(combined_html)
        for account_code, csv_bytes in parsed.items():
            tail = account_code.split("_")[-1]
            fname = f"{ym}_广发{tail}电子账单.csv"
            artifacts.append({
                "filename": fname,
                "content": csv_bytes,
                "sha256": hashlib.sha256(csv_bytes).hexdigest(),
                "origin": "guangfa_html_ebill",
                "subject": subject,
                "account_hint": account_code,
            })

    return artifacts


def imap_status() -> dict[str, Any]:
    """Return non-secret Gmail IMAP configuration status."""
    user = os.environ.get("PAMS_IMAP_USER", "").strip()
    password = os.environ.get("PAMS_IMAP_PASSWORD", "").strip()
    host = os.environ.get("PAMS_IMAP_HOST", DEFAULT_IMAP_HOST).strip() or DEFAULT_IMAP_HOST
    folder = os.environ.get("PAMS_IMAP_FOLDER", DEFAULT_IMAP_FOLDER).strip() or DEFAULT_IMAP_FOLDER
    raw_senders = os.environ.get("PAMS_IMAP_SENDERS", "").strip()
    senders = [s.strip() for s in raw_senders.split(",") if s.strip()] if raw_senders else list(DEFAULT_IMAP_SENDERS)
    poll_time = os.environ.get("PAMS_IMAP_POLL_TIME", "08:30").strip() or "08:30"
    return {
        "configured": bool(user and password),
        "host": host,
        "user": user if user else None,
        "folder": folder,
        "senders": senders,
        "poll_time": poll_time,
    }


def _imap_search_ids(client: imaplib.IMAP4_SSL, *, folder: str, senders: list[str], lookback_days: int) -> list[bytes]:
    since_str = (date.today() - timedelta(days=max(1, lookback_days))).strftime("%d-%b-%Y")
    # If user created a dedicated Gmail label (not INBOX), scan all recent messages in that label.
    if folder.upper() != "INBOX" or not senders:
        status, data = client.search(None, "SINCE", since_str)
        return data[0].split() if status == "OK" and data and data[0] else []

    seen: set[bytes] = set()
    ordered: list[bytes] = []
    for sender in senders:
        status, data = client.search(None, "SINCE", since_str, "FROM", f'"{sender}"')
        if status == "OK" and data and data[0]:
            for msg_id in data[0].split():
                if msg_id not in seen:
                    seen.add(msg_id)
                    ordered.append(msg_id)
    ordered.sort(key=lambda raw: int(raw))
    return ordered


def fetch_gmail_bill_artifacts(
    *,
    known_hashes: set[str] | None = None,
    max_messages: int = 25,
) -> dict[str, Any]:
    """Connect to Gmail IMAP, fetch recent bank bill emails, and return deduplicated new artifacts."""
    cfg = imap_status()
    if not cfg["configured"]:
        return {
            "configured": False,
            "skipped": True,
            "reason": "imap_not_configured",
            "fetched_messages": 0,
            "skipped_duplicates": 0,
            "artifacts": [],
        }

    port = int(os.environ.get("PAMS_IMAP_PORT", str(DEFAULT_IMAP_PORT)))
    lookback_days = int(os.environ.get("PAMS_IMAP_LOOKBACK_DAYS", "45"))
    password = os.environ.get("PAMS_IMAP_PASSWORD", "").strip()
    known = {h.lower() for h in (known_hashes or set()) if h}

    new_artifacts: list[dict[str, Any]] = []
    skipped_duplicates = 0
    fetched_messages = 0

    with imaplib.IMAP4_SSL(cfg["host"], port) as client:
        client.login(cfg["user"], password)
        status, _ = client.select(f'"{cfg["folder"]}"', readonly=True)
        if status != "OK":
            raise RuntimeError(f"无法打开邮箱文件夹：{cfg['folder']}")
        msg_ids = _imap_search_ids(
            client,
            folder=cfg["folder"],
            senders=cfg["senders"],
            lookback_days=lookback_days,
        )[-max_messages:]
        for msg_id in msg_ids:
            fetch_status, msg_data = client.fetch(msg_id, "(RFC822)")
            if fetch_status != "OK" or not msg_data:
                continue
            fetched_messages += 1
            for item in msg_data:
                if not (isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes)):
                    continue
                for artifact in extract_bill_artifacts(item[1]):
                    if artifact["sha256"] in known:
                        skipped_duplicates += 1
                        continue
                    known.add(artifact["sha256"])
                    new_artifacts.append(artifact)

    return {
        "configured": True,
        "skipped": False,
        "folder": cfg["folder"],
        "fetched_messages": fetched_messages,
        "skipped_duplicates": skipped_duplicates,
        "artifacts": new_artifacts,
    }
