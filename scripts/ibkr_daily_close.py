"""Validate or explicitly publish supplemental Flex evidence; never import facts."""
from __future__ import annotations

import argparse
from datetime import date
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend" / "api"))
from domain.brokers.ibkr.ibkr_daily_close import MAX_XML_BYTES, parse_daily_close_evidence  # noqa: E402
from domain.brokers.ibkr.ibkr_daily_close_read import read_bounded  # noqa: E402
from domain.brokers.ibkr.ibkr_daily_close_publish import publish, publication_lock as _publication_lock  # noqa: E402

LOCK_WAIT_SECONDS = 10


def publication_lock(directory: Path):
    """Preserve the CLI lock adapter while API and CLI share one publisher."""
    return _publication_lock(directory, wait_seconds=LOCK_WAIT_SECONDS)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--xml", type=Path)
    source.add_argument("--download-query-id")
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--as-of", required=True, type=date.fromisoformat)
    parser.add_argument("--period-start", required=True, type=date.fromisoformat)
    parser.add_argument("--publish-dir", type=Path, help="Explicit publication; default validates only")
    args = parser.parse_args()
    try:
        if args.xml:
            if args.xml.stat().st_size > MAX_XML_BYTES:
                raise ValueError("xml_size_limit")
            content = read_bounded(args.xml, MAX_XML_BYTES)
        else:
            from collectors.ibkr_flex import request_flex_reference, fetch_flex_statement
            token = os.environ.get("IBKR_FLEX_TOKEN")
            production = {value.strip() for name in ("IBKR_FLEX_POSITIONS_QUERY_ID",
                "IBKR_FLEX_OPEN_POSITIONS_QUERY_ID", "IBKR_FLEX_QUERY_ID")
                if (value := os.environ.get(name)) and value.strip()}
            query = str(args.download_query_id).strip()
            production = {value.lstrip("0") for value in production}
            if (not token or not production or not query.isascii() or not query.isdigit()
                    or not query.lstrip("0") or query.lstrip("0") in production):
                raise ValueError("separate_audit_query_and_existing_token_required")
            reference = request_flex_reference(token, query)
            code = reference.get("reference_code")
            if not code:
                raise ValueError("missing_flex_reference")
            content = fetch_flex_statement(token, code).encode("utf-8")
        if args.publish_dir:
            result = publish(content, directory=args.publish_dir, account_id=args.account_id,
                             as_of=args.as_of, period_start=args.period_start)
        else:
            result = parse_daily_close_evidence(content.decode("utf-8"), account_id=args.account_id,
                as_of=args.as_of, period_start=args.period_start,
                expected_sha256=hashlib.sha256(content).hexdigest())
        print(json.dumps(result, default=lambda v: str(v) if isinstance(v, Decimal) else v, sort_keys=True))
        return 0 if result["status"] in {"ok", "published"} else 2
    except Exception as exc:
        # Flex/transport exceptions can include credential-bearing request URLs.
        print(json.dumps({"status": "unavailable", "error_type": type(exc).__name__,
                          "facts_written": 0}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
