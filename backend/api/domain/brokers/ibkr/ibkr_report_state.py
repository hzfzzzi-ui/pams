"""Committed Flex report selection. Raw files are evidence, not publication pointers."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from functools import lru_cache
import hashlib
import io
import json
import os
import re
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import psycopg2.extras


class ReportRejected(ValueError):
    """A downloaded report must not replace the current published state."""


MAX_XML_BYTES = 32 * 1024 * 1024


def production_scope(content: bytes, as_of: date, *, strict_ancestors: bool = False) -> tuple[str, str]:
    if len(content) > MAX_XML_BYTES:
        raise ValueError("unsupported_xml")
    text = content.decode("utf-8")
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", text, re.IGNORECASE):
        raise ValueError("unsupported_xml")
    root = ET.fromstring(text)
    statements = [e for e in root.iter() if e.tag.rsplit("}", 1)[-1] == "FlexStatement"]
    accounts = {s.get("accountId") for s in statements}
    if len(accounts) != 1 or not all(accounts):
        raise ValueError("ambiguous_or_missing_account_scope")
    current = [s for s in statements if _report_date(s.get("toDate")) == as_of]
    if len(current) != 1:
        raise ValueError("ambiguous_or_missing_closing_statement")
    statement = current[0]
    information = [e for e in statement.iter() if e.tag.rsplit("}", 1)[-1] == "AccountInformation"]
    if len(information) != 1:
        raise ValueError("ambiguous_or_missing_account_scope")
    info = information[0]
    members = set(statement.iter())
    all_members = {e for s in statements for e in s.iter()}
    account, base = statement.get("accountId"), info.get("currency")
    if (not account or info not in members or info.get("accountId") != account
            or not base or not re.fullmatch(r"[A-Z]{3}", base, flags=re.ASCII)
            or _report_date(statement.get("toDate")) != as_of):
        raise ValueError("unverified_account_scope")
    # The independent auditor reports ancestor conflicts itself; publication must reject them first.
    if strict_ancestors and any(node.get("accountId") not in (None, "", account)
           or node.get("acctId") not in (None, "", account) or node.get("model") for node in root.iter()):
        raise ValueError("contradictory_account_or_model_scope")
    # The subject scans the whole document; an orphan row must not inherit this account.
    for node in root.iter():
        tag = node.tag.rsplit("}", 1)[-1]
        if tag not in {"CashReportCurrency", "OpenPosition"}:
            continue
        if node not in all_members:
            raise ValueError("unbound_subject_rows")
        field = "reportDate" if tag == "OpenPosition" else "toDate"
        if node.get(field) and _report_date(node.get(field)) == as_of and node not in members:
            raise ValueError("closing_rows_outside_selected_statement")
    return account, base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@lru_cache(maxsize=1)
def _calendar():
    return xcals.get_calendar("XNYS")


def expected_statement_date(reference: date | None = None, *, now: datetime | None = None) -> date:
    current = (now or utc_now()).astimezone(ZoneInfo("America/New_York"))
    if reference is None:
        hour, minute = map(int, os.environ.get("PAMS_NORTH_AMERICA_REFRESH_TIME", "16:30").split(":"))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ReportRejected("invalid_refresh_cutoff")
        reference = current.date() - timedelta(days=(current.hour, current.minute) < (hour, minute))
    return _calendar().date_to_session(reference.isoformat(), direction="previous").date()


def _report_date(value: Any) -> date:
    text = str(value or "")
    if len(text) == 8 and text.isdigit():
        return datetime.strptime(text, "%Y%m%d").date()
    return date.fromisoformat(text)


def inspect_report(xml_text: str) -> date:
    """Validate fields consumed by the existing performance parser, without calculating amounts."""
    root = ET.fromstring(xml_text)
    cash = [e.attrib for e in root.iter("CashReportCurrency") if e.get("currency") == "BASE_SUMMARY"]
    if not cash:
        raise ReportRejected("missing_base_cash")
    latest = max(cash, key=lambda row: _report_date(row.get("toDate")))
    observed = _report_date(latest.get("toDate"))
    current_cash = [row for row in cash if _report_date(row.get("toDate")) == observed]
    if any(row != latest for row in current_cash):
        raise ReportRejected("ambiguous_base_cash")

    def require_number(row: dict[str, str], key: str) -> None:
        try:
            value = Decimal(row[key].replace(",", ""))
            if not value.is_finite():
                raise ValueError()
        except (KeyError, InvalidOperation, ValueError):
            raise ReportRejected("missing_or_invalid_" + key) from None

    for field in ("endingCash", "depositWithdrawalsYTD"):
        require_number(latest, field)
    positions = list(root.iter("OpenPosition"))
    current = [e.attrib for e in positions if _report_date(e.get("reportDate")) == observed]
    if positions and not current:
        raise ReportRejected("cash_position_date_mismatch")
    if not positions and not list(root.iter("OpenPositions")):
        raise ReportRejected("missing_open_positions_section")
    accounts = {row.get("accountId") for row in [*current_cash, *current] if row.get("accountId")}
    accounts.update(e.get("accountId") for e in root.iter("FlexStatement") if e.get("accountId"))
    if len(accounts) > 1:
        raise ReportRejected("multiple_source_accounts_require_review")
    for row in current:
        if not row.get("symbol") or not row.get("currency"):
            raise ReportRejected("missing_position_identity")
        require_number(row, "position" if "position" in row else "quantity")
        require_number(row, "positionValue")
        if row.get("currency") != "USD":
            require_number(row, "fxRateToBase")
        for key in ("fxRateToBase", "fifoPnlUnrealized"):
            if key in row:
                require_number(row, key)
        if "fxRateToBase" in row and Decimal(row["fxRateToBase"].replace(",", "")) <= 0:
            raise ReportRejected("invalid_fx_rate")
    for elem in root.iter("Trade"):
        for key in ("fifoPnlRealized", "fxRateToBase"):
            if key in elem.attrib:
                require_number(elem.attrib, key)
    return observed


@dataclass(frozen=True)
class StoredReport:
    """Immutable bytes from the published source, not a local filesystem path."""
    content: bytes
    name: str

    def read_bytes(self) -> bytes:
        return self.content

    def open(self, mode: str = 'rb'):
        if mode != 'rb':
            raise ValueError('stored report is read only')
        return io.BytesIO(self.content)


@dataclass(frozen=True)
class PublishedReport:
    path: Path | StoredReport
    as_of: date
    run_id: int
    sha256: str
    data_sha256: str | None
    legacy: bool = False
    row_count: int | None = None


def _records(conn) -> list[dict[str, Any]]:
    """Adapt durable business publications to the existing report selection shape."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT to_regclass('public.broker_processing_state') IS NOT NULL AS exists")
        if not cur.fetchone()["exists"]:
            return []
        cur.execute("""SELECT publication_id,account_code,source_batch_id,statement_date,
                              source_sha256,replay_fingerprint,published_result_sha256,holding_count,cash_state,cash_state_sha256
            FROM broker_processing_state WHERE state_type='publication' AND account_code='ibkr'
            ORDER BY publication_id DESC""")
        rows = [dict(row) for row in cur.fetchall()]
    for row in rows:
        expected = hashlib.sha256(json.dumps(row['cash_state'],sort_keys=True,separators=(',', ':'),
            ensure_ascii=False,allow_nan=False).encode('utf8')).hexdigest()
        row['cash_state_valid'] = expected == str(row['cash_state_sha256']).strip()
    return [{"id": row['publication_id'], "status": "success", "last_attempt_unknown": True,
             "target_trade_date": row['statement_date'],
             "summary_json": {"statement_as_of":row['statement_date'].isoformat(),
                 "raw_file":'source:'+str(row['source_batch_id']),"row_count":row['holding_count'],
                 "publication":{"version":1,"state":"published","account_code":row['account_code'],
                     "report_date":row['statement_date'].isoformat(),"sha256":str(row['source_sha256']).strip(),
                     "data_sha256":str(row['replay_fingerprint']).strip(),
                     "published_result_sha256":str(row['published_result_sha256']).strip(),
                     "report_trade_sources":{"batch_id":str(row['source_batch_id'])},
                     **({'cash_events':row['cash_state']} if row.get('cash_state') is not None and row['cash_state_valid'] else {}),
                     'cash_state_valid':row['cash_state_valid']}}} for row in rows]


def _eligible_records(records: list[dict[str, Any]]) -> list[tuple[date, int, dict[str, Any]]]:
    eligible = []
    for row in records:
        summary = row.get("summary_json") or {}
        if not isinstance(summary, dict):
            continue
        publication = summary.get("publication")
        if (row["status"] != "success" or not isinstance(publication, dict)
                or publication.get("state") != "published" or publication.get("version") != 1
                or publication.get("account_code") != "ibkr"):
            continue
        try:
            observed = _report_date(summary.get("statement_as_of"))
            eligible.append((observed, int(row["id"]), row))
        except (TypeError, ValueError):
            continue
    return sorted(eligible, key=lambda item: item[:2], reverse=True)


def publication_floor(conn) -> date | None:
    """Missing or corrupt files cannot lower the committed publication high-water mark."""
    eligible = _eligible_records(_records(conn))
    return eligible[0][0] if eligible else None


def _published_source_report(conn, *, batch_id: str, observed: date, publication: dict[str, Any]) -> StoredReport:
    """Bind run, report batch, scope, original file and publication snapshot."""
    from domain.imports.fact_associations import check_all_source_links
    from domain.imports.source_fact_evidence import _reviewed_snapshot
    from domain.brokers.ibkr.ibkr_report_batch import NAMESPACE
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT current_setting('transaction_read_only')='on' AS ro,current_setting('transaction_isolation') AS level")
        mode=cur.fetchone(); readonly=mode['ro']
        if readonly and mode['level'] not in {'repeatable read','serializable'}:
            raise ValueError('stored report requires a consistent read-only snapshot')
        if not readonly:
            cur.execute("SELECT pg_try_advisory_xact_lock(hashtextextended('account-import:ibkr',0)) AS acquired")
            if not cur.fetchone()['acquired']: raise ValueError('stored report account is busy')
        suffix='' if readonly else ' FOR SHARE'
        cur.execute('SELECT *,published_result::text AS result_text FROM source_batches WHERE batch_id=%s'+suffix,(batch_id,))
        batch=cur.fetchone()
        if (batch and publication.get('published_result_sha256') and
                (batch['result_text'] is None or hashlib.sha256(batch['result_text'].encode('utf8')).hexdigest()!=publication['published_result_sha256'])):
            raise ValueError('stored publication result differs')
        if (not batch or batch['status']!='published' or batch['account_code']!='ibkr'
                or batch['institution']!='ibkr' or batch['workflow']!='ibkr_report'
                or batch['acquisition_method']!='api' or batch['business_type']!='trade' or batch['date_to']!=observed
                or not _reviewed_snapshot(cur,batch,batch_id,locking=not readonly,snapshot_cache={})):
            raise ValueError('stored report batch is not verified')
        cur.execute("SELECT * FROM source_items WHERE batch_id=%s AND source_uid='scope'"+suffix,(batch_id,))
        scope=cur.fetchone(); raw=scope['raw_fields'] if scope else None
        if (not isinstance(raw,dict) or not scope or scope['review_status']!='confirmed'
                or raw.get('kind')!='report' or raw.get('business_date')!=observed.isoformat()
                or raw.get('sha256')!=publication.get('sha256') or not raw.get('source_account_id')
                or str(scope['source_item_id'])!=str(uuid5(UUID(batch_id),'scope'))
                or str(uuid5(NAMESPACE,'batch:'+raw['source_account_id']+':'+observed.isoformat()+':'+raw['sha256']))!=batch_id
                or bool(scope.get('file_id'))==bool(scope.get('origin_file_id'))):
            raise ValueError('stored report scope is invalid')
        file_id=scope.get('file_id') or scope.get('origin_file_id')
        cur.execute('SELECT * FROM source_files WHERE file_id=%s'+suffix,(str(file_id),))
        file=cur.fetchone()
        if not file or file['file_format']!='xml' or file['content_type']!='application/xml':
            raise ValueError('stored report file is missing')
        if scope.get('file_id'):
            if str(file['batch_id'])!=batch_id or str(file_id)!=str(uuid5(UUID(batch_id),'raw-xml')):
                raise ValueError('stored report file ownership is invalid')
        else:
            cur.execute('SELECT * FROM source_batches WHERE batch_id=%s'+suffix,(str(file['batch_id']),))
            original=cur.fetchone()
            if (not original or original['status']!='published' or original['account_code']!='ibkr'
                    or original['institution']!='ibkr' or original['acquisition_method']!='api' or original['date_to']!=observed
                    or original['workflow'] not in {'ibkr_report','ibkr_cash_report','ibkr_holding_review'}
                    or original['business_type']!=({'ibkr_report':'trade','ibkr_cash_report':'cash','ibkr_holding_review':'holding'}[original['workflow']])
                    or not _reviewed_snapshot(cur,original,str(original['batch_id']),locking=not readonly,snapshot_cache={})):
                raise ValueError('stored report original owner is invalid')
            cur.execute("SELECT raw_fields,review_status,file_id FROM source_items WHERE batch_id=%s AND source_uid='scope'"+suffix,(str(original['batch_id']),))
            owner_scope=cur.fetchone(); owner_raw=owner_scope['raw_fields'] if owner_scope else {}
            if (not owner_scope or owner_scope['review_status']!='confirmed' or owner_scope['file_id']!=file_id
                    or owner_raw.get('kind')!=('scope' if original['workflow']=='ibkr_holding_review' else 'report')
                    or owner_raw.get('business_date')!=observed.isoformat()
                    or owner_raw.get('source_account_id')!=raw['source_account_id']
                    or (owner_raw.get('sha256') or owner_raw.get('source_sha256'))!=raw['sha256']):
                raise ValueError('stored report original scope is invalid')
        content=bytes(file['content']); digest=hashlib.sha256(content).hexdigest()
        if len(content)>MAX_XML_BYTES or digest!=raw['sha256'] or digest!=str(file['sha256']).strip():
            raise ValueError('stored report file hash mismatch')
        from domain.brokers.ibkr.ibkr_daily_close import _document
        _,_,error=_document(content.decode('utf8'),raw['source_account_id'],digest)
        if error: raise ValueError('stored report document is unsafe')
        records=(batch.get('published_result') or {}).get('records')
        if not isinstance(records,list): raise ValueError('stored report fact set missing')
        check_all_source_links(cur,batch_id,records)
        return StoredReport(content,'ibkr-source-'+digest+'.xml')


def select_report(conn, raw_dir: Path) -> tuple[PublishedReport | None, dict[str, Any] | None]:
    records = _records(conn)
    eligible = _eligible_records(records)
    # The publication with the highest business date is the quality reference;
    # a later state revision for an older date must not hide a damaged report.
    latest = eligible[0][2] if eligible else (records[0] if records else None)
    for observed, run_id, row in eligible:
        summary = row["summary_json"]
        publication = summary.get("publication") or {}
        try:
            reference = summary['raw_file']
            if not isinstance(reference, str) or not reference.startswith('source:'):
                continue
            batch_id = str(UUID(reference.removeprefix('source:')))
            if ((publication.get('report_trade_sources') or {}).get('batch_id') != batch_id
                    or publication.get('account_code') != 'ibkr'):
                continue
            path = _published_source_report(conn, batch_id=batch_id, observed=observed, publication=publication)
            content = path.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            if publication and (publication.get("sha256") != digest or publication.get("report_date") != observed.isoformat()):
                continue
            if inspect_report(content.decode("utf-8")) != observed:
                continue
            count = summary.get("row_count")
            count = count if isinstance(count, int) and count >= 0 else None
            report = PublishedReport(path, observed, run_id, digest, publication.get("data_sha256"), False, count)
            return report, latest
        except (OSError, ValueError, KeyError, TypeError, ET.ParseError):
            continue
    return None, latest


def report_quality(report: PublishedReport | None, latest: dict[str, Any] | None, *, expected: date | None = None) -> dict[str, Any]:
    try:
        due = expected or expected_statement_date()
    except (ValueError, TypeError):
        due = None
    using_previous = (True if report and latest and report.run_id != latest["id"] else
                      None if latest and latest.get('last_attempt_unknown') else False)
    summary = (latest or {}).get("summary_json")
    publication = summary.get("publication") if isinstance(summary, dict) else None
    last_state = publication.get("state") if isinstance(publication, dict) else None
    status, label = "ok", "有效报告"
    if report is None:
        status, label = "missing", "无有效报告"
    elif due is None or report.legacy:
        status, label = "review", "历史报告待核准"
    if report is not None and due is not None and report.as_of < due:
        status, label = "stale", "上一有效值已过期"
    elif report is not None and latest and (
        latest["status"] not in {"success", "ok"}
        or (using_previous and last_state != "unchanged")
    ):
        status, label = "review", "沿用上一有效值"
    return {
        "status": status, "label": label, "using_previous": using_previous,
        "statement_as_of": report.as_of.isoformat() if report else None,
        "expected_statement_as_of": due.isoformat() if due else None,
        "active_run_id": report.run_id if report else None,
        "active_row_count": report.row_count if report else None,
        "legacy_unverified": bool(report and report.legacy),
        "last_attempt_status": (latest.get("status") if latest and not latest.get("last_attempt_unknown") else None),
    }


def validate_candidate(observed: date, expected: date, previous: PublishedReport | None, *, now: datetime | None = None, minimum: date | None = None) -> None:
    if observed < expected:
        raise ReportRejected("stale_statement_date")
    if (previous and observed < previous.as_of) or (minimum and observed < minimum):
        raise ReportRejected("report_date_regression")
    current = now or utc_now()
    local = current.astimezone(ZoneInfo("America/New_York"))
    session = _calendar().date_to_session(local.date().isoformat(), direction="previous")
    if _calendar().session_close(session).to_pydatetime() > current:
        session = _calendar().previous_session(session)
    latest_closed = session.date()
    # Flex covers a global account: a US holiday may still contain Asian-market activity.
    if not _calendar().is_session(local.date().isoformat()):
        hour, minute = map(int, os.environ.get("PAMS_NORTH_AMERICA_REFRESH_TIME", "16:30").split(":"))
        if (local.hour, local.minute) >= (hour, minute):
            latest_closed = local.date()
    if observed > latest_closed:
        raise ReportRejected("future_or_unclosed_statement_date")
