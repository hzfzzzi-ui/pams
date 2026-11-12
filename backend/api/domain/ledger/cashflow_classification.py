"""Classification decisions for redesigned cash facts; unknown stays unknown."""
from __future__ import annotations

from functools import cache
from pathlib import Path
import re

import yaml


PERSONAL_LOAN_CATEGORY = '个人借款'
PERSONAL_LOAN_PRINCIPAL = {
    'personal_loan_drawdown': ('debt_drawdown', '个人借入本金'),
    'personal_loan_repayment': ('debt_repayment', '归还借款本金'),
}


def personal_loan_principal(row):
    """Identify an explicitly classified principal movement by its original direction."""
    amount = row.get('amount')
    if (not amount or row.get('classification_status') != 'confirmed'
            or row.get('category') != PERSONAL_LOAN_CATEGORY
            or row.get('classification_source') != 'manual'
            or row.get('source_evidence_status') != 'verified'):
        return None
    kind = 'personal_loan_drawdown' if amount > 0 else 'personal_loan_repayment'
    cashflow_type, subcategory = PERSONAL_LOAN_PRINCIPAL[kind]
    if row.get('cashflow_type') == cashflow_type and row.get('subcategory') == subcategory:
        return kind
    return None


def _normalize(text):
    return ' '.join(str(text or '').casefold().split())


def _keyword_pattern(keyword):
    term = _normalize(keyword)
    left = r'(?<![a-z0-9])' if term[0].isascii() and term[0].isalnum() else ''
    right = r'(?![a-z0-9])' if term[-1].isascii() and term[-1].isalnum() else ''
    return re.compile(left + re.escape(term) + right)


@cache
def _keyword_dictionary():
    path = Path(__file__).with_name('cashflow_keywords.yaml')
    data = yaml.safe_load(path.read_text(encoding='utf-8'))
    rules = [
        (rule['direction'], rule.get('account_type'),
         {key: rule[key] for key in ('cashflow_type', 'category', 'subcategory')},
         [(keyword, _keyword_pattern(keyword)) for keyword in rule.get('keywords', ())],
         [(label, _normalize(label)) for label in rule.get('summary_labels', ())])
        for rule in data['rules']
    ]
    return rules, [_keyword_pattern(keyword) for keyword in data['exclude_keywords']]


def classification_hint(row):
    """Return a read-only suggestion for verified, unclassified cashflow."""
    if row.get('source_evidence_status') != 'verified' or row.get('classification_status') != 'pending':
        return None
    amount = row.get('amount')
    if not amount:
        return None
    direction = 'inflow' if amount > 0 else 'outflow'
    texts = [_normalize(row.get(field)) for field in ('summary', 'description', 'counterparty')]
    rules, exclusions = _keyword_dictionary()
    if any(pattern.search(text) for pattern in exclusions for text in texts):
        return None
    matches = {}
    summary_label = texts[0].partition(' ')[0]
    for rule_direction, account_type, result, keywords, summary_labels in rules:
        if rule_direction not in (direction, 'both'):
            continue
        if account_type and account_type != row.get('account_type'):
            continue
        matched = next((label for label, normalized in summary_labels if normalized == summary_label), None)
        if matched is None:
            matched = next((keyword for keyword, pattern in keywords
                            if any(pattern.search(text) for text in texts)), None)
        if matched is not None:
            identity = (result['cashflow_type'], result['category'], result['subcategory'])
            matches.setdefault(identity, {**result, 'matched_keyword': matched})
    return next(iter(matches.values())) if len(matches) == 1 else None


def suggest(cur, *, cashflow_id: int, cashflow_type: str, category: str | None = None,
            subcategory: str | None = None, source: str, confidence=None) -> bool:
    if cur.connection.autocommit:
        raise ValueError('classification requires a transaction')
    if not source or not cashflow_type:
        raise ValueError('classification suggestion requires type and source')
    cur.execute('''INSERT INTO public.cashflow_classification
        (cashflow_id,cashflow_type,category,subcategory,status,classification_source,confidence)
        VALUES (%s,%s,%s,%s,'suggested',%s,%s)
        ON CONFLICT (cashflow_id) DO UPDATE SET cashflow_type=EXCLUDED.cashflow_type,
            category=EXCLUDED.category,subcategory=EXCLUDED.subcategory,status='suggested',
            classification_source=EXCLUDED.classification_source,confidence=EXCLUDED.confidence,updated_at=now()
        WHERE public.cashflow_classification.status <> 'confirmed'
        RETURNING cashflow_id''', (cashflow_id,cashflow_type,category,subcategory,source,confidence))
    return cur.fetchone() is not None


def confirm(cur, *, cashflow_id: int, cashflow_type: str, category: str | None = None,
            subcategory: str | None = None, note: str | None = None) -> None:
    if cur.connection.autocommit or not cashflow_type:
        raise ValueError('manual classification requires transaction and type')
    cur.execute('''INSERT INTO public.cashflow_classification
        (cashflow_id,cashflow_type,category,subcategory,status,classification_source,note)
        VALUES (%s,%s,%s,%s,'confirmed','manual',%s)
        ON CONFLICT (cashflow_id) DO UPDATE SET cashflow_type=EXCLUDED.cashflow_type,
        category=EXCLUDED.category,subcategory=EXCLUDED.subcategory,status='confirmed',
        classification_source='manual',confidence=NULL,note=EXCLUDED.note,updated_at=now()''',
        (cashflow_id,cashflow_type,category,subcategory,note))
