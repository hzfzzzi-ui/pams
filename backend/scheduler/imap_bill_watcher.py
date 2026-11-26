"""Daily-once IMAP bill attachment watcher for PAMS scheduler.

Safe by default: no-op unless PAMS_IMAP_HOST, PAMS_IMAP_USER, and PAMS_IMAP_PASSWORD are set.
Downloads supported statement attachments into the local _Inbox dropzone with SHA256 dedup,
never writing directly to financial tables.
"""

from __future__ import annotations

import email
import hashlib
import imaplib
import json
import logging
import os
import re
from email.header import decode_header
from pathlib import Path
from typing import Any

logger = logging.getLogger("scheduler.imap")

SUPPORTED_EXTENSIONS = {".pdf", ".xls", ".xlsx", ".csv", ".tsv", ".zip"}
SAFE_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


def decode_mime_filename(raw: str | None) -> str:
    if not raw:
        return ""
    parts = decode_header(raw)
    chunks: list[str] = []
    for data, charset in parts:
        if isinstance(data, bytes):
            chunks.append(data.decode(charset or "utf-8", errors="replace"))
        else:
            chunks.append(str(data))
    cleaned = SAFE_FILENAME_RE.sub("_", "".join(chunks).strip())
    return cleaned[:160]


def extract_attachments(raw_message: bytes) -> list[tuple[str, bytes]]:
    """Extract supported bill attachments from a raw RFC822 email message."""
    msg = email.message_from_bytes(raw_message)
    found: list[tuple[str, bytes]] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        filename = decode_mime_filename(part.get_filename())
        if not filename:
            continue
        ext = Path(filename).suffix.lower()
        if ext not in SUPPORTED_EXTENSIONS:
            continue
        payload = part.get_payload(decode=True)
        if payload:
            found.append((filename, payload))
    return found


def _load_seen_hashes(state_file: Path) -> set[str]:
    if not state_file.exists():
        return set()
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
        return set(data.get("seen_sha256", []))
    except Exception:
        return set()


def _save_seen_hashes(state_file: Path, hashes: set[str]) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(
        json.dumps({"seen_sha256": sorted(hashes)[-5000:]}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def save_new_attachments(
    attachments: list[tuple[str, bytes]],
    *,
    inbox_dir: Path,
    state_file: Path,
) -> list[str]:
    """Persist deduplicated attachments into inbox_dir and return saved relative filenames."""
    seen = _load_seen_hashes(state_file)
    inbox_dir.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    for filename, payload in attachments:
        digest = hashlib.sha256(payload).hexdigest()
        if digest in seen:
            continue
        target = inbox_dir / filename
        if target.exists():
            stem, suffix = target.stem, target.suffix
            target = inbox_dir / f"{stem}_{digest[:8]}{suffix}"
        target.write_bytes(payload)
        seen.add(digest)
        saved.append(target.name)
    if saved:
        _save_seen_hashes(state_file, seen)
    return saved


def poll_imap_bills(
    *,
    inbox_dir: Path | None = None,
    state_file: Path | None = None,
    max_messages: int = 30,
) -> dict[str, Any]:
    """Connect to configured IMAP mailbox once per day and drop new bill attachments into _Inbox."""
    host = os.environ.get("PAMS_IMAP_HOST", "").strip()
    user = os.environ.get("PAMS_IMAP_USER", "").strip()
    password = os.environ.get("PAMS_IMAP_PASSWORD", "").strip()
    if not (host and user and password):
        return {"skipped": True, "reason": "imap_not_configured", "saved": []}

    port = int(os.environ.get("PAMS_IMAP_PORT", "993"))
    folder = os.environ.get("PAMS_IMAP_FOLDER", "PAMS-Bills").strip() or "INBOX"
    target_dir = inbox_dir or Path(os.environ.get("PAMS_IMPORT_INBOX_DIR", "/host/account-ledger/_Inbox"))
    journal = state_file or Path(os.environ.get("PAMS_IMAP_STATE_PATH", "/state/imap_seen.json"))

    attachments: list[tuple[str, bytes]] = []
    with imaplib.IMAP4_SSL(host, port) as client:
        client.login(user, password)
        status, _ = client.select(folder, readonly=True)
        if status != "OK":
            logger.warning("[IMAP] folder=%s not selectable", folder)
            return {"skipped": False, "folder": folder, "error": "folder_not_found", "saved": []}
        status, data = client.search(None, "ALL")
        if status != "OK" or not data or not data[0]:
            return {"skipped": False, "folder": folder, "saved": []}
        msg_ids = data[0].split()[-max_messages:]
        for msg_id in msg_ids:
            fetch_status, msg_data = client.fetch(msg_id, "(RFC822)")
            if fetch_status != "OK" or not msg_data:
                continue
            for item in msg_data:
                if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
                    attachments.extend(extract_attachments(item[1]))

    saved = save_new_attachments(attachments, inbox_dir=target_dir, state_file=journal)
    logger.info("[IMAP] folder=%s checked=%d saved=%d", folder, len(attachments), len(saved))
    return {"skipped": False, "folder": folder, "saved": saved}
