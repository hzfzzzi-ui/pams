"""Verified, account-bound period cash-flow evidence for IBKR reports.

An observed set of transactions does not establish complete flow coverage.
Only a published original report with a verified account scope and a strict
ChangeInNAV bridge can prove the period. Unknown evidence remains unknown.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Context, Decimal, InvalidOperation, localcontext
import os
from typing import Any
from zoneinfo import ZoneInfo

from domain.investments.snapshot_coverage import load_account_snapshot_coverage


_TOLERANCE = Decimal("0.01")


class IBKRSourceConflict(ValueError):
    """Published account evidence is ambiguous, invalid, or contradictory."""


def _source_present(cur, account_code: str, day: date) -> bool:
    cur.execute("""SELECT EXISTS(SELECT 1 FROM source_batches
              WHERE account_code=%s AND workflow='ibkr_holding_review' AND status='published'
                AND date_from=%s AND date_to=%s)
        OR EXISTS(SELECT 1 FROM account_snapshot_version WHERE account_code=%s AND as_of=%s
            AND scope_attestation->>'kind'='ibkr_reviewed_complete_account') AS present""",
                (account_code,day,day,account_code,day))
    row=cur.fetchone()
    if row is None:
        raise IBKRSourceConflict('source presence unavailable')
    return bool(row['present'])


def _view_date() -> date:
    zone_name = os.environ.get("PAMS_TIMEZONE", os.environ.get("TZ", "Asia/Shanghai"))
    try:
        zone = ZoneInfo(zone_name)
    except Exception:
        zone = ZoneInfo("Asia/Shanghai")
    return datetime.now(zone).date()


def _source(cur, account_code: str, day: date) -> dict[str, Any] | None:
    """Read only a current, fully verified account scope and its original bytes."""
    proof = load_account_snapshot_coverage(cur, {(account_code, day)}).get((account_code, day), {})
    if proof.get("status") != "complete" or not proof.get("version_id"):
        return None
    cur.execute("""SELECT v.source_batch_id,v.scope_attestation,b.*
        FROM account_snapshot_version v JOIN source_batches b ON b.batch_id=v.source_batch_id
        WHERE v.version_id=%s""", (proof["version_id"],))
    row = cur.fetchone()
    if not row:
        return None
    batch = dict(row)
    # Multiple published account reviews for the same day are ambiguous even
    # if one happens to be the latest snapshot version.
    cur.execute("""SELECT COUNT(*) AS count FROM source_batches
        WHERE account_code=%s AND workflow='ibkr_holding_review' AND status='published'
          AND date_from=%s AND date_to=%s""", (account_code,day,day))
    published = cur.fetchone()
    if not published or published['count'] != 1:
        return None
    att = batch.get("scope_attestation") or {}
    nav = att.get("nav_evidence") or {}
    if (att.get("kind") != "ibkr_reviewed_complete_account" or
        batch.get("workflow") != "ibkr_holding_review" or batch.get("status") != "published" or
        batch.get("account_code") != account_code or att.get("account_code") != account_code or
        att.get("as_of") != day.isoformat() or not isinstance(nav, dict)):
        return None
    cur.execute("SELECT * FROM source_items WHERE batch_id=%s AND source_uid='scope'", (batch["batch_id"],))
    scope = cur.fetchone()
    if not scope or scope["review_status"] != "confirmed":
        return None
    raw = scope.get("raw_fields") or {}
    account_id, digest = raw.get("source_account_id"), raw.get("source_sha256")
    if not isinstance(account_id, str) or not account_id or digest != att.get("source_sha256"):
        return None
    from domain.imports.ibkr_holding_source import _read_source_report
    try:
        content = _read_source_report(cur, batch, scope, locking=False)
        return {"account_id":account_id, "digest":digest, "content":content, "nav":nav}
    except (TypeError, ValueError, KeyError):
        return None


def _verified_daily_flow_cny(cur, *, content: bytes, digest: str, account_id: str,
                             base: str, start: date, end: date, opening: Decimal,
                             closing: Decimal, aggregate_flow: Decimal):
    """Use every day's original bridge and that day's sourced FX, or unknown."""
    # The reporting parser permits omitted weekends; for currency conversion
    # we require an explicit bridge for every date, including weekends.
    days = (end-start).days
    if not 1 < days <= 366:
        return None
    from domain.brokers.ibkr.ibkr_daily_close import _document, _daily_result
    from domain.brokers.ibkr.ibkr_daily_close_read import historical_fx
    evidence, observed_digest, error = _document(content.decode("utf-8"), account_id, digest)
    if error or evidence is None or observed_digest != digest:
        return None
    previous = opening
    total_base = Decimal(0)
    total_cny = Decimal(0)
    rows = []
    for offset in range(1, days+1):
        day = start+timedelta(days=offset)
        result = _daily_result(evidence, digest, day, period_start=day)
        period = result.get("period") or {}
        if (result.get("status") != "ok" or result.get("account_id") != account_id
            or result.get("base_currency") != base or result.get("source_sha256") != digest
            or result.get("as_of") != day.isoformat() or period.get("from_date") != day.isoformat()
            or period.get("to_date") != day.isoformat()
            or period.get("basis") != "nav_change_less_external_flows"
            or period.get("evidence_basis") != "exact_change_in_nav"):
            return None
        before, after, flow = (Decimal(str(period[name])) for name in
                               ("opening_nav_base", "closing_nav_base", "net_external_flow_base"))
        if not all(value.is_finite() for value in (before,after,flow)) or abs(before-previous) > _TOLERANCE:
            return None
        fx = historical_fx(cur, currency=base, base_currency="CNY", as_of=day)
        if fx.get("status") != "ok" or not str(fx.get("source") or "").strip():
            return None
        rate = Decimal(str(fx["rate"]))
        if not rate.is_finite() or rate <= 0:
            return None
        with localcontext(Context(prec=160, Emax=512, Emin=-512)):
            converted = flow*rate
            total_base += flow
            total_cny += converted
        rows.append({"date":day.isoformat(), "external_flow_base":str(flow),
                     "opening_nav_base":str(before), "closing_nav_base":str(after),
                     "fx_rate":str(rate), "fx_source":fx["source"], "external_flow_cny":str(converted)})
        previous = after
    if abs(previous-closing) > _TOLERANCE or abs(total_base-aggregate_flow) > _TOLERANCE:
        return None
    return total_cny, rows


def _independent_daily_sources_agree(cur, *, account_code: str, account_id: str,
                                      base: str, start: date, end: date, rows: list[dict[str, str]]) -> bool:
    """Do not use a composite report over a conflicting published daily review."""
    from domain.brokers.ibkr.ibkr_daily_close import parse_daily_close_evidence
    by_day = {row['date']:row for row in rows}
    for offset in range(1,(end-start).days):
        day=start+timedelta(days=offset)
        if not _source_present(cur,account_code,day):
            continue
        source=_source(cur,account_code,day)
        expected=by_day.get(day.isoformat())
        if not source or not expected or source['account_id']!=account_id or source['nav'].get('base_currency')!=base:
            return False
        result=parse_daily_close_evidence(source['content'].decode('utf-8'),account_id=account_id,
                                         as_of=day,period_start=day,expected_sha256=source['digest'])
        period=result.get('period') or {}
        if (result.get('status')!='ok' or result.get('account_id')!=account_id or result.get('as_of')!=day.isoformat()
            or result.get('base_currency')!=base or result.get('source_sha256')!=source['digest']
            or period.get('from_date')!=day.isoformat() or period.get('to_date')!=day.isoformat()
            or period.get('evidence_basis')!='exact_change_in_nav'
            or period.get('basis')!='nav_change_less_external_flows'):
            return False
        for key in ('opening_nav_base','closing_nav_base','net_external_flow_base'):
            expected_key='external_flow_base' if key=='net_external_flow_base' else key
            actual=Decimal(str(period[key])); expected_amount=Decimal(str(expected[expected_key]))
            if not actual.is_finite() or abs(actual-expected_amount)>_TOLERANCE:
                return False
        nav=Decimal(str(source['nav']['nav_base']))
        if not nav.is_finite() or abs(nav-Decimal(str(period['closing_nav_base'])))>_TOLERANCE:
            return False
        expected['independent_source_sha256']=source['digest']
    return True


def _verified_daily_scope_chain(cur, *, account_code: str, account_id: str, base: str,
                                start: date, end: date, opening_nav: Decimal,
                                closing_nav: Decimal):
    """Join individually reviewed daily originals, preserving each source hash."""
    days = (end-start).days
    if not 1 < days <= 366:
        return None
    from domain.brokers.ibkr.ibkr_daily_close import parse_daily_close_evidence
    from domain.brokers.ibkr.ibkr_daily_close_read import historical_fx
    previous = opening_nav
    total = Decimal(0)
    rows = []
    for offset in range(1,days+1):
        day = start+timedelta(days=offset)
        source = _source(cur, account_code, day)
        if not source or source['account_id'] != account_id or source['nav'].get('base_currency') != base:
            return None
        result = parse_daily_close_evidence(source['content'].decode('utf-8'), account_id=account_id,
            as_of=day, period_start=day, expected_sha256=source['digest'])
        period = result.get('period') or {}
        if (result.get('status') != 'ok' or result.get('account_id') != account_id
            or result.get('base_currency') != base or result.get('source_sha256') != source['digest']
            or result.get('as_of') != day.isoformat() or period.get('from_date') != day.isoformat()
            or period.get('to_date') != day.isoformat() or period.get('basis') != 'nav_change_less_external_flows'
            or period.get('evidence_basis') != 'exact_change_in_nav'):
            return None
        before,after,flow = (Decimal(str(period[name])) for name in
                             ('opening_nav_base','closing_nav_base','net_external_flow_base'))
        official = Decimal(str(source['nav']['nav_base']))
        if (not all(value.is_finite() for value in (before,after,flow,official))
            or abs(before-previous)>_TOLERANCE or abs(after-official)>_TOLERANCE):
            return None
        fx = historical_fx(cur, currency=base, base_currency='CNY', as_of=day)
        if fx.get('status') != 'ok' or not str(fx.get('source') or '').strip():
            return None
        rate = Decimal(str(fx['rate']))
        if not rate.is_finite() or rate<=0:
            return None
        with localcontext(Context(prec=160,Emax=512,Emin=-512)):
            converted=flow*rate
            total+=converted
        rows.append({'date':day.isoformat(),'external_flow_base':str(flow),'fx_rate':str(rate),
                     'opening_nav_base':str(before),'closing_nav_base':str(after),
                     'fx_source':fx['source'],'external_flow_cny':str(converted),
                     'source_sha256':source['digest']})
        previous=after
    if abs(previous-closing_nav)>_TOLERANCE:
        return None
    return total,rows


def _composite_report_agrees_with_daily_chain(*, content: bytes, digest: str, account_id: str,
                                               base: str, start: date, end: date,
                                               rows: list[dict[str, str]]) -> bool:
    """Compare any overlapping daily statement in the closing original."""
    from domain.brokers.ibkr.ibkr_daily_close import _document, _daily_result
    evidence, observed_digest, error = _document(content.decode('utf-8'), account_id, digest)
    if error or evidence is None or observed_digest != digest:
        return False
    for expected in rows:
        day=date.fromisoformat(expected['date'])
        if day == end or day not in evidence.by_day:
            continue
        result=_daily_result(evidence,digest,day,period_start=day)
        period=result.get('period') or {}
        if (result.get('status')!='ok' or result.get('account_id')!=account_id or result.get('as_of')!=day.isoformat()
            or result.get('base_currency')!=base or result.get('source_sha256')!=digest
            or period.get('from_date')!=day.isoformat() or period.get('to_date')!=day.isoformat()
            or period.get('evidence_basis')!='exact_change_in_nav'
            or period.get('basis')!='nav_change_less_external_flows'):
            return False
        for key in ('opening_nav_base','closing_nav_base','net_external_flow_base'):
            expected_key='external_flow_base' if key=='net_external_flow_base' else key
            actual=Decimal(str(period[key])); expected_amount=Decimal(str(expected[expected_key]))
            if not actual.is_finite() or abs(actual-expected_amount)>_TOLERANCE:
                return False
    return True


def _opening_scope_agrees_with_closing_report(*, content: bytes, digest: str, account_id: str,
                                               base: str, start: date, opening_nav: dict[str, Any]) -> bool:
    """An explicit opening-day statement in the closing original may not conflict."""
    from domain.brokers.ibkr.ibkr_daily_close import _document, _daily_result
    evidence, observed_digest, error=_document(content.decode('utf-8'),account_id,digest)
    if error or evidence is None or observed_digest!=digest:
        return False
    if start not in evidence.by_day:
        return True
    result=_daily_result(evidence,digest,start)
    if (result.get('status')!='ok' or result.get('account_id')!=account_id
        or result.get('as_of')!=start.isoformat() or result.get('base_currency')!=base
        or result.get('source_sha256')!=digest):
        return False
    for actual_key,expected_key in (('nav_base','nav_base'),('cash_base','cash_base'),
                                    ('position_value_base','positions_base')):
        actual=Decimal(str(result[actual_key])); expected=Decimal(str(opening_nav[expected_key]))
        if not actual.is_finite() or not expected.is_finite() or abs(actual-expected)>_TOLERANCE:
            return False
    return True


def verified_ibkr_period(cur, *, account_code: str, start: date, end: date,
                         ending_gross_cny: Decimal, raise_on_conflict: bool = False) -> dict[str, Any] | None:
    """Prove opening/closing scope and official net external flow for a period.

    The period starts immediately after the opening snapshot and ends at the
    closing snapshot. Non-CNY multi-day flows are unavailable without each
    flow's dated FX; one-day flows use the exact closing-day reference FX.
    """
    if (account_code != "ibkr" or type(start) is not date or type(end) is not date
        or not start < end or end > _view_date() or cur.connection.autocommit):
        return None
    cur.execute("SELECT current_setting('transaction_isolation') AS isolation")
    mode = cur.fetchone()
    if not mode or mode["isolation"] not in {"repeatable read", "serializable"}:
        return None
    from domain.accounts.flow_coverage import has_unvalued_non_cash_transfer
    if has_unvalued_non_cash_transfer(cur, account_code, start+timedelta(days=1), end):
        return None
    opening, closing = _source(cur, account_code, start), _source(cur, account_code, end)
    if not opening or not closing:
        if raise_on_conflict and ((_source_present(cur,account_code,start) and not opening)
                                  or (_source_present(cur,account_code,end) and not closing)):
            raise IBKRSourceConflict('invalid or ambiguous published endpoint')
        return None
    if opening["account_id"] != closing["account_id"]:
        if raise_on_conflict:
            raise IBKRSourceConflict('published account identities conflict')
        return None
    try:
        if raise_on_conflict and (end-start).days > 366:
            raise IBKRSourceConflict('published source interval exceeds conflict-check limit')
        from domain.brokers.ibkr.ibkr_daily_close import parse_daily_close_evidence
        if raise_on_conflict and (end-start).days > 1:
            previous_source: dict[str, Any] | None = opening
            previous_day = start
            for offset in range(1,(end-start).days+1):
                day=start+timedelta(days=offset)
                present = day==end or _source_present(cur,account_code,day)
                source = closing if day==end else _source(cur,account_code,day) if present else None
                if present and not source:
                    raise IBKRSourceConflict('invalid or ambiguous intermediate published source')
                if source and source['account_id'] != opening['account_id']:
                    raise IBKRSourceConflict('published account identities conflict')
                if source and previous_source:
                    base_currency=str(previous_source['nav'].get('base_currency') or '')
                    if (source['nav'].get('base_currency') != base_currency or
                        not _opening_scope_agrees_with_closing_report(content=source['content'],
                            digest=source['digest'],account_id=source['account_id'],base=base_currency,
                            start=previous_day,opening_nav=previous_source['nav'])):
                        raise IBKRSourceConflict('adjacent published sources conflict')
                    daily=parse_daily_close_evidence(source['content'].decode('utf-8'),
                        account_id=source['account_id'],as_of=day,period_start=day,
                        expected_sha256=source['digest'])
                    if daily.get('status')=='ok':
                        prior=Decimal(str((daily.get('period') or {})['opening_nav_base']))
                        if abs(prior-Decimal(str(previous_source['nav']['nav_base'])))>_TOLERANCE:
                            raise IBKRSourceConflict('adjacent daily NAV endpoints conflict')
                previous_source=source
                previous_day=day
        from domain.brokers.ibkr.ibkr_daily_close_read import historical_fx
        open_nav, close_nav = opening["nav"], closing["nav"]
        base = str(close_nav["base_currency"])
        if open_nav["base_currency"] != base:
            return None
        if not _opening_scope_agrees_with_closing_report(content=closing['content'],digest=closing['digest'],
            account_id=closing['account_id'],base=base,start=start,opening_nav=open_nav):
            if raise_on_conflict:
                raise IBKRSourceConflict('opening account evidence conflicts with closing report')
            return None
        if base != "CNY" and (end-start).days > 1:
            prior_nav = Decimal(str(open_nav["nav_base"]))
            final_nav = Decimal(str(close_nav["nav_base"]))
            if prior_nav.is_finite() and final_nav.is_finite():
                chain = _verified_daily_scope_chain(cur, account_code=account_code,
                    account_id=closing['account_id'], base=base, start=start, end=end,
                    opening_nav=prior_nav, closing_nav=final_nav)
                if chain:
                    if not _composite_report_agrees_with_daily_chain(content=closing['content'],
                        digest=closing['digest'], account_id=closing['account_id'], base=base,
                        start=start, end=end, rows=chain[1]):
                        if raise_on_conflict:
                            raise IBKRSourceConflict('daily published sources conflict with closing report')
                        return None
                    before_fx = historical_fx(cur, currency=base, base_currency='CNY', as_of=start)
                    after_fx = historical_fx(cur, currency=base, base_currency='CNY', as_of=end)
                    if all(item.get('status')=='ok' and str(item.get('source') or '').strip()
                           for item in (before_fx,after_fx)):
                        before_rate,after_rate=(Decimal(str(item['rate'])) for item in (before_fx,after_fx))
                        opening_cny,closing_cny=prior_nav*before_rate,final_nav*after_rate
                        if abs(closing_cny-ending_gross_cny)<=_TOLERANCE:
                            return {'start':start,'end':end,'opening_gross_cny':opening_cny,
                                'closing_gross_cny':closing_cny,'external_flow_gross_cny':chain[0],
                                'source_sha256':closing['digest'],'flow_base_currency':base,
                                'flow_fx_as_of':None,'daily_flow_fx_evidence':chain[1],
                                'basis':'reviewed_daily_statement_chain'}
        result = parse_daily_close_evidence(closing["content"].decode("utf-8"),
            account_id=closing["account_id"], as_of=end, period_start=start+timedelta(days=1),
            expected_sha256=closing["digest"])
        period = result.get("period") or {}
        if (result.get("status") != "ok" or result.get("account_id") != closing["account_id"] or
            result.get("as_of") != end.isoformat() or result.get("base_currency") != base or
            result.get("source_sha256") != closing["digest"] or
            period.get("basis") != "nav_change_less_external_flows" or
            period.get("evidence_basis") not in {"exact_change_in_nav", "complete_daily_change_in_nav"} or
            period.get("from_date") != (start+timedelta(days=1)).isoformat() or
            period.get("to_date") != end.isoformat()):
            return None
        prior, latest, flow = (Decimal(str(period[name])) for name in
                               ("opening_nav_base", "closing_nav_base", "net_external_flow_base"))
        if (not all(value.is_finite() for value in (prior, latest, flow)) or
            abs(prior-Decimal(str(open_nav["nav_base"]))) > _TOLERANCE or
            abs(latest-Decimal(str(close_nav["nav_base"]))) > _TOLERANCE):
            return None
        before_fx = historical_fx(cur, currency=base, base_currency="CNY", as_of=start)
        after_fx = historical_fx(cur, currency=base, base_currency="CNY", as_of=end)
        if any(item.get("status") != "ok" or not str(item.get("source") or "").strip()
               for item in (before_fx, after_fx)):
            return None
        before_rate, after_rate = (Decimal(str(item["rate"])) for item in (before_fx, after_fx))
        opening_cny, closing_cny = prior*before_rate, latest*after_rate
        if abs(closing_cny - ending_gross_cny) > _TOLERANCE:
            return None
        daily_evidence = None
        if base != "CNY" and (end-start).days != 1:
            daily = _verified_daily_flow_cny(cur, content=closing["content"], digest=closing["digest"],
                account_id=closing["account_id"], base=base, start=start, end=end,
                opening=prior, closing=latest, aggregate_flow=flow)
            if daily is None:
                return None
            flow_cny, daily_evidence = daily
            if not _independent_daily_sources_agree(cur, account_code=account_code,
                account_id=closing['account_id'], base=base, start=start, end=end, rows=daily_evidence):
                if raise_on_conflict:
                    raise IBKRSourceConflict('closing report conflicts with published daily sources')
                return None
        else:
            flow_cny = flow*after_rate
        return {"start":start, "end":end, "opening_gross_cny":opening_cny,
                "closing_gross_cny":closing_cny, "external_flow_gross_cny":flow_cny,
                "source_sha256":closing["digest"], "flow_base_currency":base,
                "flow_fx_as_of":end.isoformat() if daily_evidence is None else None,
                "daily_flow_fx_evidence":daily_evidence, "basis":period.get("evidence_basis")}
    except IBKRSourceConflict:
        raise
    except (KeyError, ValueError, TypeError, UnicodeError, InvalidOperation, OverflowError):
        return None


def latest_verified_ibkr_period(cur, *, account_code: str, end: date,
                                ending_gross_cny: Decimal, raise_on_conflict: bool = False) -> dict[str, Any] | None:
    """Use the most recent prior account-snapshot day; never skip an unknown day."""
    if account_code != "ibkr":
        return None
    cur.execute("""SELECT MAX(as_of) AS previous_day FROM account_snapshot_version
        WHERE account_code=%s AND as_of<%s""", (account_code,end))
    row = cur.fetchone()
    start = row.get("previous_day") if row else None
    if type(start) is not date:
        return None
    return verified_ibkr_period(cur, account_code=account_code, start=start, end=end,
                                ending_gross_cny=ending_gross_cny, raise_on_conflict=raise_on_conflict)
