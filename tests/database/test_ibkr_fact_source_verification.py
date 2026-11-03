"""Shared synthetic reports and direct legacy evidence guard tests."""
from datetime import date
from hashlib import sha256
import re
from xml.etree import ElementTree as ET
import pytest
from tests.database.test_ibkr_report_batch import report
from domain.brokers.ibkr.ibkr_fact_source_verification import EvidenceCache, EvidenceUnavailable, _report_node

DAY=date(2026,10,22)

def original():
    return report().replace(b'tradePrice="170"',b'tradePrice="170" multiplier="1" fifoPnlRealized="0"')

def cash_report(event_day=DAY):
    cash=('<CashTransactions><CashTransaction accountId="U_TEST" transactionID="CASH1" '
          f'currency="EUR" dateTime="{event_day:%Y%m%d};120000" amount="10" type="Dividends" />'
          '</CashTransactions>').encode()
    return original().replace(b'</FlexStatement>',cash+b'</FlexStatement>')


def test_duplicate_official_identifier_cannot_be_verified():
    payload=original(); trade=re.search(rb'<Trade\b[^>]*/>',payload).group()
    duplicated=payload.replace(trade,trade+trade)
    attrs=dict(ET.fromstring(trade).attrib)
    raw={'official_attributes':attrs,'source_account_id':'U_TEST','report_evidence':{
        'broker_source':'ibkr_flex','source_sha256':sha256(duplicated).hexdigest(),
        'confirmed_day':DAY.isoformat(),'evidence_role':'source_report'}}
    with pytest.raises(EvidenceUnavailable,match='source_identity_not_unique'):
        _report_node(raw,duplicated,'ibkr_flex_trade',None)


def test_recovery_disables_cache_without_allocating_xid():
    class NoExecute:
        def execute(self,*args):
            raise AssertionError('standby must not allocate an xid')
    cache=EvidenceCache(); cache.sources['old']='stale'; cache.documents['old']='stale'
    assert not cache.bind(NoExecute(),True) and not cache.sources and not cache.documents


def test_cash_report_rejects_wrong_original_ordinal():
    payload = cash_report()
    official = ET.fromstring(payload).find('.//CashTransaction')
    assert official is not None
    metadata = {'official_attributes': dict(official.attrib), 'source_account_id': 'U_TEST',
                'cash_ordinal': 2, 'reported_cash_count': 1,
                'report_evidence': {'broker_source': 'ibkr_flex',
                                    'source_sha256': sha256(payload).hexdigest(),
                                    'confirmed_day': DAY.isoformat()}}
    with pytest.raises(EvidenceUnavailable, match='cash_original_ordinal_mismatch'):
        _report_node(metadata, payload, 'ibkr_flex_cash_transaction', None)
