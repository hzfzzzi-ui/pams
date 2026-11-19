"""Display text from a verified import, independent of classification and cash facts."""

import re


def statement_display_summary(fields):
    for key in ("summary", "description", "counterparty"):
        value = fields.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    # Older transcriptions kept the original statement caption inside a provenance note.
    note = fields.get("note")
    if isinstance(note, str):
        match = re.match(r"^截图人工转录；明细顺序\d+；(.+?)；交易后余额", note)
        if match:
            return match.group(1).strip()
    return None
