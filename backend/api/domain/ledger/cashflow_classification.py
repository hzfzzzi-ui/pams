"""Classification decisions for redesigned cash facts; unknown stays unknown."""
from __future__ import annotations


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
