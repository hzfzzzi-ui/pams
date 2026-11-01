"""Synthetic template matching; a parsed file is not necessarily a match."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend/api'))
from domain.imports.template_assessment import assess_template


def test_native_and_complete_templates_match_without_asserting_fact_validity():
    native = {'rows': [{'source': {'profile': 'ths_broker_ledger_v1'}}], 'warnings': []}
    assert assess_template(native, 'trade') == 'matched'
    tabular = {'rows': [{}], 'suggested_mapping': {'business_date': '日期', 'symbol': '代码',
               'action': '方向', 'gross_amount': '金额'}, 'warnings': []}
    assert assess_template(tabular, 'trade') == 'matched'


def test_changed_headers_missing_fields_and_empty_files_never_report_success():
    assert assess_template({'rows': [], 'warnings': []}, 'trade') == 'unmatched'
    assert assess_template({'rows': [{}], 'suggested_mapping': {'symbol': '代码'}}, 'trade') == 'needs_mapping'
    assert assess_template({'rows': [{'source': {'profile': 'ths_broker_ledger_v1'}}],
                            'warnings': [{'code': 'headers_changed'}]}, 'trade') == 'needs_mapping'
