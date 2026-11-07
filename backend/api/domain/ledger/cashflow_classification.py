"""Classification decisions for redesigned cash facts; unknown stays unknown."""
from __future__ import annotations


def bank_classification_hint(row):
    """Suggest only when a verified bank description names the transaction type."""
    if row.get('source_evidence_status') != 'verified' or row.get('classification_status') != 'pending':
        return None
    account = row.get('account_code')
    summary = str(row.get('summary') or '').strip()
    amount = row.get('amount')
    if amount is None:
        return None
    if ((account == 'ccb_7332' and amount < 0 and summary.startswith('信用卡约定还款 '))
            or (account == 'ccb_credit_0453' and amount > 0 and summary.startswith('存入 约定还款 '))):
        return {'cashflow_type': 'debt_repayment', 'category': '内部划转', 'subcategory': '信用卡还款'}
    if account == 'ccb_7332' and amount < 0 and summary.startswith('基金下账 基金定投申购,'):
        return {'cashflow_type': 'investment_funding', 'category': '内部划转', 'subcategory': '基金定投申购'}
    if account == 'ccb_7332' and amount > 0:
        if summary.startswith('薪金报酬 '):
            return {'cashflow_type': 'operating_income', 'category': '工资薪酬', 'subcategory': '工资'}
        if summary == '利息存入' or summary.startswith('利息存入 '):
            return {'cashflow_type': 'operating_income', 'category': '利息收入', 'subcategory': '利息'}
    if account == 'ccb_credit_0453' and amount < 0 and summary.startswith('消费 '):
        ambiguous = ('充值', 'charge', 'top up', 'top-up', 'wallet', '财付通', '支付宝', '理财', '基金',
                     '还款', '转账', '预授权', '退款', '撤销', 'refund', 'reversal')
        if not any(term in summary.casefold() for term in ambiguous):
            return {'cashflow_type': 'operating_expense', 'category': '其他支出', 'subcategory': '其他支出'}
    return None


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
