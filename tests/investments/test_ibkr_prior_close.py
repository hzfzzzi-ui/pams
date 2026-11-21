from datetime import date
from decimal import Decimal
import xml.etree.ElementTree as ET

from domain.brokers.ibkr import ibkr_daily_close as close


def _xml():
    account='synthetic'
    root=ET.Element('FlexQueryResponse')
    stmt=ET.SubElement(root,'FlexStatement',accountId=account,fromDate='20260101',toDate='20260101')
    ET.SubElement(stmt,'AccountInformation',accountId=account,currency='USD')
    section=ET.SubElement(stmt,'EquitySummaryInBase')
    for day in ('20251231','20260101'):
        fields={key:'0' for key in (*close.COMPONENT_FIELDS,'total')}
        fields.update({key+suffix:'0' for key in list(fields) for suffix in ('Long','Short')})
        ET.SubElement(section,'EquitySummaryByReportDateInBase',accountId=account,currency='USD',reportDate=day,**fields)
    ET.SubElement(stmt,'OpenPositions')
    flows={key:'0' for key in close.FLOW_FIELDS}
    ET.SubElement(stmt,'ChangeInNAV',accountId=account,currency='USD',fromDate='20260101',toDate='20260101',startingValue='0',endingValue='0',**flows)
    return root


def test_explicit_adjacent_opening_is_a_reconciled_dated_nav():
    root=_xml()
    result=close.parse_daily_close_series(ET.tostring(root,encoding='unicode'),account_id='synthetic')
    assert result['daily_closes']['2025-12-31']['nav_base']==Decimal(0)
    assert result['daily_closes']['2025-12-31']['evidence_basis']=='next_statement_opening_nav'
    assert result['daily_closes']['2026-01-01']['status']=='ok'
    root.find('.//ChangeInNAV').set('startingValue','1')
    result=close.parse_daily_close_series(ET.tostring(root,encoding='unicode'),account_id='synthetic')
    assert '2025-12-31' not in result['daily_closes']
    root=_xml(); root.find('.//EquitySummaryByReportDateInBase').set('reportDate','20251230')
    result=close.parse_daily_close_series(ET.tostring(root,encoding='unicode'),account_id='synthetic')
    assert '2025-12-31' not in result['daily_closes']
