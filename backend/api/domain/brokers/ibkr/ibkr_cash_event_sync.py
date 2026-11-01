"""Bounded cash-fact consumption of already published daily-close evidence.

Independent of the daily-close publisher and its official confirmation state.
The caller supplies a dedicated, idle connection; sync owns its single transaction.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_HALF_UP, localcontext
import hashlib
import json
from typing import Any
import xml.etree.ElementTree as ET

import psycopg2.extras
from psycopg2.extensions import TRANSACTION_STATUS_IDLE

from domain.brokers.ibkr import ibkr_cash_events as cash
from domain.brokers.ibkr import ibkr_daily_close_read as daily
from domain.imports import isolation

BROKER_SOURCE = "ibkr_published_daily_close"
SYNC_KEY = "ibkr_cash_events"
MAX_DAYS = 3


def _cash_scope_ok(node: ET.Element, parents: dict, account: str) -> bool:
    ancestor: ET.Element | None = node
    while ancestor is not None:
        attrs = {key.lower(): value.strip() for key, value in ancestor.attrib.items()}
        if (len(attrs) != len(ancestor.attrib) or attrs.get("model")
                or any(attrs.get(key) not in (None, "", account) for key in ("accountid", "acctid"))):
            return False
        ancestor = parents.get(ancestor)
    return True


def _snapshot(broker_source: str) -> dict[str, Any]:
    base: dict[str, Any] = {"broker_source": BROKER_SOURCE, "account_code": "ibkr", "cutoff": None,
        "source_account_id": None, "source_sha256": None, "confirmed_day": None, "source_file": None,
        "historical_day_count": 0, "next_days": [], "queued_day_count": 0, "unavailable_retry_days": [],
        "retryable_pending_count": 0, "waiting_evidence_count": 0, "blocking_pending_count": 0,
        "bank_match_pending_count": 0, "pending": [], "facts_written": 0, "reasons": []}
    if broker_source != BROKER_SOURCE:
        return {**base, "status": "unavailable", "reasons": ["unsupported_broker_source"]}
    try:
        cutoff = cash.configured_cutoff()
    except ValueError:
        return {**base, "status": "unavailable", "reasons": ["invalid_cash_event_cutoff"]}
    base["cutoff"] = cutoff.isoformat() if cutoff else None
    if cutoff is None:
        return {**base, "status": "disabled", "facts_written": 0}
    evidence = daily.load_evidence()
    if evidence.get("status") != "ok":
        return {**base, "status": "unavailable", "reasons": evidence.get("reasons", ["missing_evidence"])}
    series = daily.read_series(account_id=evidence["account_id"], evidence=evidence)
    digest = evidence["sha256"]
    if series.get("source_sha256") != digest or not series.get("daily_closes"):
        return {**base, "status": "unavailable", "reasons": ["unverified_daily_series"]}
    confirmed = {day for day, close in series["daily_closes"].items()
                 if close.get("status") == "ok" and close.get("source_sha256") == digest
                 and close.get("account_id") == evidence["account_id"] and close.get("as_of") == day}
    closing = evidence["as_of"].isoformat()
    if closing not in confirmed:
        return {**base, "status": "unavailable", "reasons": ["unconfirmed_closing_day"]}
    root = ET.fromstring(evidence["xml_text"])
    parents = {child: parent for parent in root.iter() for child in parent}
    days = {}
    for statement in root.iter():
        if statement.tag.rsplit("}", 1)[-1] != "FlexStatement" or statement.get("accountId") != evidence["account_id"]:
            continue
        try:
            day = cash._business_date(statement.get("toDate", "")).isoformat()
        except ValueError:
            continue
        if day not in confirmed or day <= cutoff.isoformat():
            continue
        events = []
        for node in statement.iter():
            if node.tag.rsplit("}", 1)[-1] != "CashTransaction":
                continue
            envelope: dict[str, Any] = {"raw": dict(node.attrib), "statement_account_id": evidence["account_id"],
                "source_evidence": {"broker_source": BROKER_SOURCE, "source_sha256": digest, "confirmed_day": day}}
            if not _cash_scope_ok(node, parents, evidence["account_id"]):
                envelope["scope_error"] = True
            events.append(envelope)
        sections = [node for node in statement.iter() if node.tag.rsplit("}", 1)[-1] == "CashTransactions"]
        section_nodes = set(sections[0].iter()) if len(sections) == 1 else set()
        section_ok = len(sections) == 1 and all(node in section_nodes
            for node in statement.iter() if node.tag.rsplit("}", 1)[-1] == "CashTransaction")
        if section_ok:
            section_ok = (_cash_scope_ok(sections[0], parents, evidence["account_id"])
                          and sections[0].get("levelOfDetail", "DETAIL").upper() == "DETAIL")
        # The modern source path can only use explicitly scoped, complete
        # detail rows from this same verified statement, never a guessed ledger.
        trade_sections = [node for node in statement.iter() if node.tag.rsplit("}", 1)[-1] == "Trades"]
        trade_nodes = [node for node in statement.iter() if node.tag.rsplit("}", 1)[-1] == "Trade"]
        section_members = set(trade_sections[0].iter()) if len(trade_sections) == 1 else set()
        trades_ok = (len(trade_sections) == 1 and _cash_scope_ok(trade_sections[0], parents, evidence["account_id"])
                     and trade_sections[0].get("levelOfDetail", "DETAIL").upper() == "DETAIL"
                     and all(node in section_members and _cash_scope_ok(node, parents, evidence["account_id"])
                             for node in trade_nodes))
        trades = []
        for node in trade_nodes:
            try:
                trade_day = cash._business_date(node.get("tradeDate", ""))
            except ValueError:
                trades_ok = False
                continue
            if trade_day > date.fromisoformat(day):
                trades_ok = False
            elif trade_day == date.fromisoformat(day):
                trades.append({"raw": dict(node.attrib)})
        if day in days:
            trades_ok = False
            section_ok = False
        days[day] = {"events": events, "section_ok": section_ok, "trades": trades, "trades_ok": trades_ok,
                     "statement_sha256": hashlib.sha256(ET.tostring(statement)).hexdigest()}
    return {**base, "status": "ready", "source_account_id": evidence["account_id"],
            "source_sha256": digest, "confirmed_day": closing, "source_file": evidence["source_file"],
            "historical_day_count": sum(day <= cutoff.isoformat() for day in confirmed), "days": days,
            "_source_xml": evidence['xml_text']}


def _cash_progress_hash(*, account_hash, business_date, source_batch_id, report_sha256,
                        statement_sha256, trade_version, retryable_count, pending):
    document={'account_hash':account_hash,'business_date':str(business_date),
        'source_batch_id':str(source_batch_id),'report_sha256':report_sha256,
        'statement_sha256':statement_sha256,'trade_version':trade_version,
        'retryable_count':retryable_count,'pending':pending}
    return hashlib.sha256(json.dumps(document,sort_keys=True,separators=(',', ':'),ensure_ascii=False).encode('utf8')).hexdigest()


def _progress(cur, cutoff: str, source_account_id: str) -> dict[str, dict[str, Any]]:
    """Read the latest immutable business state only when its original report verifies."""
    from domain.imports.source_fact_evidence import _reviewed_snapshot
    from domain.imports.ibkr_holding_source import _read_source_report
    account_hash = hashlib.sha256(source_account_id.encode('utf8')).hexdigest()
    cur.execute("""SELECT DISTINCT ON (business_date) * FROM broker_processing_state
        WHERE state_type='cash_progress' AND account_hash=%s AND business_date > %s ORDER BY business_date,progress_id DESC""",
        (account_hash,cutoff))
    rows=[dict(row) for row in cur.fetchall()]
    cache: dict[tuple[str, str], bool] = {}
    snapshot_cache: dict[Any, Any] = {}
    locking=not bool(cur.connection.readonly)
    suffix=' FOR SHARE' if locking else ''
    result={}
    for row in rows:
        batch_id=str(row['source_batch_id']); report_sha=str(row['report_sha256']).strip()
        statement_sha=str(row['statement_sha256']).strip()
        expected=_cash_progress_hash(account_hash=account_hash,business_date=row['business_date'],
            source_batch_id=batch_id,report_sha256=report_sha,statement_sha256=statement_sha,
            trade_version=row['trade_version'],retryable_count=row['retryable_count'],pending=row['pending'])
        if expected != str(row['state_sha256']).strip():
            continue
        cache_key=(batch_id,report_sha)
        if cache_key not in cache:
            cur.execute('SELECT * FROM source_batches WHERE batch_id=%s'+suffix,(batch_id,))
            batch=cur.fetchone()
            cur.execute("SELECT * FROM source_items WHERE batch_id=%s AND source_uid='scope'"+suffix,(batch_id,))
            scope=cur.fetchone()
            raw=(scope or {}).get('raw_fields') or {}
            verified=bool(batch and scope and batch['status']=='published' and batch['account_code']=='ibkr'
                and batch['institution']=='ibkr' and batch['workflow']=='ibkr_cash_report'
                and batch['date_to'] is not None and row['business_date'] <= batch['date_to']
                and scope['review_status']=='confirmed' and raw.get('kind')=='report'
                and raw.get('source_account_id')==source_account_id and raw.get('sha256')==report_sha
                and raw.get('business_date')==batch['date_to'].isoformat()
                and _reviewed_snapshot(cur,batch,batch_id,locking=locking,snapshot_cache=snapshot_cache))
            if verified:
                try:
                    adapter={**scope,'raw_fields':{'source_account_id':source_account_id,'source_sha256':report_sha}}
                    verified=hashlib.sha256(_read_source_report(cur,batch,adapter,locking=locking)).hexdigest()==report_sha
                except (ValueError,TypeError,KeyError):
                    verified=False
            cache[cache_key]=verified
        if not cache[cache_key]:
            continue
        day=row['business_date'].isoformat()
        result[day]={'day':day,'statement_sha256':statement_sha,
            'raw_trade_version':row['trade_version'],'retryable_count':row['retryable_count'],
            'pending':row['pending']}
    return result


def _pending_business_entries(items):
    """Keep bounded actionable evidence, never arbitrary exception or report text."""
    import re
    result=[]
    for item in items:
        reason=str(item.get('reason') or '')
        if not re.fullmatch(r'[a-z0-9_]{1,80}',reason):
            reason='unverified_cash_event'
        entry={'reason':reason,'retryable':bool(item.get('retryable')),
               'blocking':bool(item.get('blocking',True)),
               'fact_imported':bool(item.get('fact_imported'))}
        for key in ('state','retry_on','event_kind'):
            value=item.get(key)
            if isinstance(value,str) and re.fullmatch(r'[a-z0-9_]{1,80}',value):
                entry[key]=value
        transaction_id=item.get('transaction_id')
        if isinstance(transaction_id,str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}',transaction_id):
            entry['transaction_id']=transaction_id
        result.append(entry)
    return result


def _trade_versions(cur, snapshot: dict[str, Any]) -> dict[str, str]:
    # This is a retry-input marker, never a fact identity. Rebuilt row IDs still deduplicate by source_key.
    cur.execute("""SELECT f.trade_date, md5(string_agg(to_jsonb(f)::text ||
            COALESCE(to_jsonb(i)::text, '') || COALESCE(to_jsonb(b)::text, ''), '' ORDER BY f.trade_id)) AS version
        FROM pams_investment_trade_fact f
        LEFT JOIN source_items i ON i.source_item_id=f.source_item_id
        LEFT JOIN source_batches b ON b.batch_id=i.batch_id
        WHERE f.account_code='ibkr' AND f.source_table='ibkr_flex_trade'
          AND f.trade_date=ANY(%s::date[]) GROUP BY f.trade_date""", (list(snapshot['days']),))
    return {row['trade_date'].isoformat(): row['version'] for row in cur.fetchall()}


def _stored_equal(left: Any, right: Any) -> bool:
    if isinstance(left, Decimal) or isinstance(right, Decimal):
        if left is None or right is None:
            return False
        with localcontext() as context:
            context.prec = 80
            quantum = Decimal("0.0000000001")
            return Decimal(str(left)).quantize(quantum, rounding=ROUND_HALF_UP) == Decimal(str(right)).quantize(quantum, rounding=ROUND_HALF_UP)
    return left == right


def _plan(snapshot: dict[str, Any], progress: dict[str, dict[str, Any]], limit: int,
          trade_versions: dict[str, str]) -> dict[str, Any]:
    fresh = [day for day, item in sorted(snapshot["days"].items())
             if day not in progress or progress[day].get("statement_sha256") != item["statement_sha256"]
             or progress[day].get("raw_trade_version") != trade_versions.get(day)]
    retry = [day for day in sorted(snapshot["days"]) if day not in fresh
             and progress.get(day, {}).get("retryable_count", 0) > 0]
    pending = [{"day": day, **item} for day, saved in sorted(progress.items())
               for item in saved.get("pending", [])]
    return {**{key: value for key, value in snapshot.items() if key != "days" and not key.startswith('_')},
            "next_days": (fresh + retry)[:limit], "queued_day_count": len(fresh) + len(retry),
            "unavailable_retry_days": [day for day, saved in sorted(progress.items())
                                       if saved.get("retryable_count", 0) > 0 and day not in snapshot["days"]],
            "retryable_pending_count": sum(bool(item.get("retryable")) for item in pending),
            "blocking_pending_count": sum(item.get("blocking", True) for item in pending),
            "bank_match_pending_count": sum(item.get("reason") == "bank_match_pending" for item in pending),
            "waiting_evidence_count": sum(not item.get("retryable") for item in pending), "pending": pending}


def status(conn, *, broker_source: str = BROKER_SOURCE) -> dict[str, Any]:
    """Read-only status, including independent retry state without downgrading close confirmation."""
    snapshot = _snapshot(broker_source)
    if snapshot["status"] != "ready":
        return snapshot
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT to_regclass('public.broker_processing_state') IS NOT NULL AS available")
        if not cur.fetchone()["available"]:
            return {**{key: value for key, value in snapshot.items() if key != "days" and not key.startswith('_')},
                    "status": "unavailable", "reasons": ["missing_cash_progress_table"]}
        return _plan(snapshot, _progress(cur, snapshot["cutoff"], snapshot["source_account_id"]), MAX_DAYS,
                     _trade_versions(cur, snapshot))


def _sync_unified_cash(conn, snapshot, *, max_days):
    """Publish daily cash and its original report in one isolated transaction."""
    from domain.brokers.ibkr.ibkr_report_batch import publish_report_batch, _same_report_fact
    from domain.brokers.ibkr.ibkr_report_trade_source import _row
    from domain.brokers.ibkr.ibkr_fact_source_verification import verify_ibkr_fact
    from domain.investments.investment_trade_fact import build_investment_trade_fact
    xml=snapshot.get('_source_xml')
    if not isinstance(xml,str) or hashlib.sha256(xml.encode('utf8')).hexdigest()!=snapshot['source_sha256']:
        raise ValueError('daily cash report original is missing or changed')
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if not isolation.source_enabled(cur):
                raise ValueError('daily cash publication requires isolated source mode')
            cur.execute('SELECT pg_advisory_xact_lock(7362198401)')
            cur.execute("SELECT pg_advisory_xact_lock(hashtextextended('account-import:ibkr',0))")
            progress=_progress(cur,snapshot['cutoff'],snapshot['source_account_id'])
            versions=_trade_versions(cur,snapshot)
            plan=_plan(snapshot,progress,max_days,versions)
        if not plan['next_days']:
            conn.rollback()
            return {**plan,'status':'idle','facts_written':0}
        closing=snapshot['days'].get(snapshot['confirmed_day'])
        if not closing:
            conn.rollback()
            pending=[{'day':day,'complete':False,'pending':[{'reason':'missing_or_ambiguous_trade_section',
                       'retryable':False,'state':'waiting_evidence','fact_imported':False,'blocking':True}]}
                     for day in plan['next_days']]
            return {**plan,'status':'waiting_evidence','facts_written':0,'days':pending}
        entries=[]; results=[]
        for day in plan['next_days']:
            item=snapshot['days'][day]
            if item['section_ok']:
                result=cash.import_cash_events(conn,item['events'],account_code='ibkr',
                    cutoff=date.fromisoformat(snapshot['cutoff']),statement_as_of=date.fromisoformat(day),defer_source_link=True)
                entries.extend(result.pop('_deferred_source_events',[]))
            else:
                result={'cutoff':snapshot['cutoff'],'inserted_count':0,'duplicate_count':0,'historical_count':0,
                    'retryable_count':0,'waiting_evidence_count':1,'blocking_pending_count':1,'bank_match_pending_count':0,
                    'pending':[{'transaction_id':None,'reason':'missing_or_ambiguous_cash_transactions_section',
                        'state':'waiting_evidence','retryable':False,'retry_on':'source_change','fact_imported':False,'blocking':True}]}
            result.update(day=day,cash_section_present=item['section_ok'],cash_inserted_count=result['inserted_count'],
                          statement_sha256=item['statement_sha256'],source_sha256=snapshot['source_sha256'])
            results.append(result)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            publication=publish_report_batch(cur,report_bytes=xml.encode('utf8'),source_account_id=snapshot['source_account_id'],
                day=date.fromisoformat(snapshot['confirmed_day']),report_sha256=snapshot['source_sha256'],cash_entries=entries,
                cash_pending_count=sum(r['blocking_pending_count'] for r in results),cash_only=True)
            versions=_trade_versions(cur,snapshot)
            for result in results:
                day=result['day']; item=snapshot['days'][day]; pending=[]
                if not item['trades_ok']:
                    pending.append({'event_kind':'trade','reason':'missing_or_ambiguous_trade_section',
                                    'retryable':False,'state':'waiting_evidence','fact_imported':False,'blocking':True})
                else:
                    reported_keys=set()
                    for trade in item['trades']:
                        try:
                            expected=_row(cur,trade,snapshot['source_account_id'],date.fromisoformat(day),snapshot['source_sha256'])
                            reported_keys.add(expected['source_key'])
                            fact=build_investment_trade_fact(expected); fact['fee_currency']=expected['explicit_fee_currency']
                            cur.execute("SELECT * FROM pams_investment_trade_fact WHERE source_table='ibkr_flex_trade' AND source_key=%s",(expected['source_key'],))
                            existing=cur.fetchall()
                            protected=('account_code','trade_date','symbol','currency','side','quantity','price','gross_amount',
                                       'fee_amount','fee_currency','tax_amount','net_cash','fx_rate_to_cny','net_cash_cny')
                            if len(existing)!=1 or not _same_report_fact(existing[0],fact,protected):
                                raise ValueError('trade fact differs')
                            proof=verify_ibkr_fact(cur,trade_id=existing[0]['trade_id'])
                            required={'account_code','trade_date','symbol','currency','side','quantity','price','gross_amount','fee_amount','tax_amount','net_cash'}
                            if proof.get('status') not in {'consistent','partial'} or required.intersection(proof.get('unknown_fields',[])):
                                raise ValueError('trade original is unavailable')
                        except ValueError:
                            pending.append({'event_kind':'trade','reason':'unverified_trade_source','retryable':False,
                                'state':'waiting_evidence','fact_imported':False,'blocking':True})
                    cur.execute("SELECT source_key FROM pams_investment_trade_fact WHERE account_code='ibkr' AND source_table='ibkr_flex_trade' AND trade_date=%s",(day,))
                    existing_keys=[row['source_key'] for row in cur.fetchall()]
                    if len(existing_keys)!=len(reported_keys) or set(existing_keys)!=reported_keys:
                        pending.append({'event_kind':'trade','reason':'unverified_trade_source','retryable':False,
                            'state':'waiting_evidence','fact_imported':False,'blocking':True})
                result['trade_inserted_count']=0
                result['inserted_count']+=result['trade_inserted_count']
                result['pending'].extend(pending)
                result['waiting_evidence_count']+=len(pending)
                result['blocking_pending_count']+=len(pending)
                result['complete']=result['blocking_pending_count']==0
                result['raw_trade_version']=versions.get(day)
                result['source_contributions']='not_recorded_for_cross_report_matches'
            progress.update({item['day']:item for item in results})
            output={**_plan(snapshot,progress,max_days,versions),'status':'success',
                    'facts_written':sum(r['inserted_count'] for r in results),'days':results,
                    'source_batch_id':publication['batch_id']}
            account_hash=hashlib.sha256(snapshot['source_account_id'].encode('utf8')).hexdigest()
            for item in results:
                pending = _pending_business_entries(item['pending'])
                state_hash = _cash_progress_hash(account_hash=account_hash,business_date=item['day'],
                    source_batch_id=publication['batch_id'],report_sha256=snapshot['source_sha256'],
                    statement_sha256=item['statement_sha256'],trade_version=item.get('raw_trade_version'),
                    retryable_count=item['retryable_count'],pending=pending)
                cur.execute("""SELECT state_sha256 FROM broker_processing_state
                    WHERE state_type='cash_progress' AND account_hash=%s AND business_date=%s ORDER BY progress_id DESC LIMIT 1""",
                    (account_hash,item['day']))
                previous_state=cur.fetchone()
                if not previous_state or str(previous_state['state_sha256']).strip()!=state_hash:
                    cur.execute("""INSERT INTO broker_processing_state
                        (state_type,account_hash,business_date,source_batch_id,report_sha256,statement_sha256,
                         trade_version,retryable_count,pending,state_sha256)
                        VALUES ('cash_progress',%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (account_hash,item['day'],publication['batch_id'],snapshot['source_sha256'],
                         item['statement_sha256'],item.get('raw_trade_version'),item['retryable_count'],
                         psycopg2.extras.Json(pending),state_hash))
            output['run_id']=None  # Technical run identity is not durable business evidence.
        conn.commit()
        from domain.operations.runtime_events import emit_cash_progress_result
        emit_cash_progress_result('success')
        return output
    except Exception as exc:
        conn.rollback()
        from domain.operations.runtime_events import emit_cash_progress_result
        emit_cash_progress_result('failed',exception=exc)
        raise


def sync(conn, *, broker_source: str = BROKER_SOURCE, max_days: int = MAX_DAYS) -> dict[str, Any]:
    """Import at most max_days new/retryable dates; no downloads, DDL, or balance writes."""
    if type(max_days) is not int or not 1 <= max_days <= 7:
        raise ValueError("max_days must be between 1 and 7")
    snapshot = _snapshot(broker_source)
    if snapshot["status"] != "ready":
        return {**snapshot, "facts_written": 0}
    if conn.autocommit or conn.get_transaction_status() != TRANSACTION_STATUS_IDLE:
        raise ValueError("Cash event sync requires a dedicated idle transactional connection")
    return _sync_unified_cash(conn,snapshot,max_days=max_days)
