"""Non-authoritative suggestions from independently verified bank source rows."""
from datetime import date
from decimal import Decimal
from typing import Any
from domain.ledger.bank_statement_rules import _row_transfer_amount, _amount_close


def _row_text(row: dict[str, Any]) -> str:
    """Return a searchable text blob for one staged bank statement row."""
    raw = row.get("raw_json") or row.get("raw") or {}
    raw_text = ""
    if isinstance(raw, dict):
        raw_text = " ".join(str(v or "") for v in raw.values())
    return " ".join(
        str(row.get(k) or "") for k in ("summary", "description", "counterparty", "category", "subcategory")
    ) + " " + raw_text


def _row_cny_transfer_amount(row: dict[str, Any]) -> Decimal:
    """Return the CNY-side amount when a statement row has foreign-currency settlement.

    Credit-card USD repayments often settle on the card in USD while the funding
    account pays a CNY amount. For matching against bank debits, prefer the
    original CNY amount when available.
    """
    original_currency = str(row.get("original_currency") or "").upper()
    original_amount = row.get("original_amount")
    if original_currency == "CNY" and original_amount not in (None, ""):
        return Decimal(str(original_amount)).copy_abs()

    raw = row.get("raw_json") or row.get("raw") or {}
    if isinstance(raw, dict):
        raw_original_currency = str(raw.get("original_currency") or raw.get("original_currency_text") or "").upper()
        if raw_original_currency in {"CNY", "人民币"} and raw.get("original_amount") not in (None, ""):
            return Decimal(str(raw.get("original_amount"))).copy_abs()

    return _row_transfer_amount(row)


def _date_delta_days(left: date, right: date) -> int:
    """Return the absolute date distance in calendar days."""
    return abs((left - right).days)


def _has_any(text: str, keywords: list[str]) -> bool:
    return any(str(k) and str(k) in text for k in keywords)


def _best_amount_date_match(
    reference: dict[str, Any],
    candidates: list[dict[str, Any]],
    *,
    used_ids: set[int],
    date_window_days: int,
    tolerance: Decimal,
    reference_amount: Decimal | None = None,
) -> tuple[dict[str, Any] | None, int]:
    ref_amount = reference_amount if reference_amount is not None else _row_transfer_amount(reference)
    best: tuple[int, int, dict[str, Any]] | None = None
    for candidate in candidates:
        if candidate["id"] in used_ids:
            continue
        if not _amount_close(ref_amount, _row_transfer_amount(candidate), tolerance):
            continue
        delta = _date_delta_days(reference["transaction_date"], candidate["transaction_date"])
        if delta > date_window_days:
            continue
        rank = (delta, int(candidate["id"]))
        if best is None or rank < best[:2]:
            best = (rank[0], rank[1], candidate)
    if best is None:
        return None, 0
    return best[2], best[0]


def _match_key(match_type: str, relation_code: str, source_row_id: int | None, target_row_id: int | None, amount: Decimal) -> str:
    source = source_row_id if source_row_id is not None else "none"
    target = target_row_id if target_row_id is not None else "none"
    cents = amount.quantize(Decimal("0.01"))
    return f"{match_type}:{relation_code}:{source}:{target}:{cents}"


def _make_match(
    *,
    match_type: str,
    relation_code: str,
    from_account_code: str,
    to_account_code: str,
    amount: Decimal,
    source_row: dict[str, Any] | None,
    target_row: dict[str, Any] | None,
    date_delta_days: int,
    confidence: Decimal,
    note: str,
) -> dict[str, Any]:
    source_id = source_row.get("id") if source_row else None
    target_id = target_row.get("id") if target_row else None
    match_date = None
    if source_row and source_row.get("transaction_date"):
        match_date = source_row["transaction_date"]
    elif target_row and target_row.get("transaction_date"):
        match_date = target_row["transaction_date"]
    currency = (source_row or target_row or {}).get("currency") or "CNY"
    return {
        "match_key": _match_key(match_type, relation_code, source_id, target_id, amount),
        "match_type": match_type,
        "relation_code": relation_code,
        "from_account_code": from_account_code,
        "to_account_code": to_account_code,
        "source_row_id": source_id,
        "target_row_id": target_id,
        "amount": amount,
        "currency": currency,
        "match_date": match_date,
        "date_delta_days": date_delta_days,
        "confidence": confidence,
        "note": note,
    }


def _build_transfer_matches(rows: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    matching_cfg = config.get("internal_transfer_matching") or {}
    date_window_days = int(matching_cfg.get("date_window_days", 2))
    tolerance = Decimal(str(matching_cfg.get("amount_tolerance", "0.01")))

    ccb_rows = [r for r in rows if r.get("account_code") == "ccb_7332"]
    wechat_rows = [r for r in rows if r.get("account_code") == "wechat"]
    used_bank_ids: set[int] = set()
    used_wechat_ids: set[int] = set()
    matches: list[dict[str, Any]] = []

    bank_investment_candidates = [
        r
        for r in ccb_rows
        if Decimal(str(r.get("amount") or "0")) < 0
        and _has_any(_row_text(r), ["财付通", "微信支付", "腾安基金", "理财通"])
    ]
    wechat_licaitong_from_bank = [
        r
        for r in wechat_rows
        if _row_transfer_amount(r) > 0
        and _has_any(_row_text(r), ["购买理财通", "理财通"])
        and _has_any(_row_text(r), ["建设银行储蓄卡(7332)"])
    ]
    for wechat_row in wechat_licaitong_from_bank:
        bank_row, delta = _best_amount_date_match(
            wechat_row,
            bank_investment_candidates,
            used_ids=used_bank_ids,
            date_window_days=date_window_days,
            tolerance=tolerance,
        )
        if not bank_row:
            continue
        amount = _row_transfer_amount(wechat_row)
        used_bank_ids.add(bank_row["id"])
        used_wechat_ids.add(wechat_row["id"])
        matches.append(
            _make_match(
                match_type="investment_funding",
                relation_code="ccb_to_tencent_licaitong",
                from_account_code="ccb_7332",
                to_account_code="tencent_licaitong",
                amount=amount,
                source_row=bank_row,
                target_row=wechat_row,
                date_delta_days=delta,
                confidence=Decimal("0.98"),
                note="建行扣款与微信理财通中性记录金额匹配",
            )
        )

    wechat_lqt_to_licaitong = [
        r
        for r in wechat_rows
        if r["id"] not in used_wechat_ids
        and _row_transfer_amount(r) > 0
        and _has_any(_row_text(r), ["购买理财通", "理财通"])
        and _has_any(_row_text(r), ["零钱通"])
        and not _has_any(_row_text(r), ["建设银行储蓄卡(7332)"])
    ]
    for row in wechat_lqt_to_licaitong:
        amount = _row_transfer_amount(row)
        used_wechat_ids.add(row["id"])
        matches.append(
            _make_match(
                match_type="investment_funding",
                relation_code="wechat_lqt_to_tencent_licaitong",
                from_account_code="wechat_lqt",
                to_account_code="tencent_licaitong",
                amount=amount,
                source_row=row,
                target_row=None,
                date_delta_days=0,
                confidence=Decimal("0.90"),
                note="微信零钱通申购理财通，微信账单为中性记录",
            )
        )

    bank_payment_candidates = [
        r
        for r in ccb_rows
        if r["id"] not in used_bank_ids
        and Decimal(str(r.get("amount") or "0")) < 0
        and _has_any(_row_text(r), ["财付通", "微信支付"])
        and not _has_any(_row_text(r), ["腾安基金", "理财通"])
    ]
    wechat_bank_card_payments = [
        r
        for r in wechat_rows
        if r["id"] not in used_wechat_ids
        and Decimal(str(r.get("amount") or "0")) < 0
        and _has_any(_row_text(r), ["建设银行储蓄卡(7332)"])
        and not _has_any(_row_text(r), ["理财通"])
    ]
    for wechat_row in wechat_bank_card_payments:
        bank_row, delta = _best_amount_date_match(
            wechat_row,
            bank_payment_candidates,
            used_ids=used_bank_ids,
            date_window_days=1,
            tolerance=tolerance,
        )
        if not bank_row:
            continue
        amount = _row_transfer_amount(wechat_row)
        used_bank_ids.add(bank_row["id"])
        used_wechat_ids.add(wechat_row["id"])
        matches.append(
            _make_match(
                match_type="payment_proxy",
                relation_code="ccb_wechat_payment_proxy",
                from_account_code="ccb_7332",
                to_account_code="wechat_balance",
                amount=amount,
                source_row=bank_row,
                target_row=wechat_row,
                date_delta_days=delta,
                confidence=Decimal("0.95"),
                note="同一笔外部消费：建行实际扣款，微信只作支付通道",
            )
        )

    payment_proxy_relations = [
        r
        for r in (config.get("account_relationships") or [])
        if r.get("relation_type") == "payment_proxy"
        and r.get("code") != "ccb_wechat_payment_proxy"
    ]
    used_proxy_source_ids = {int(m["source_row_id"]) for m in matches if m.get("source_row_id") is not None}
    used_proxy_target_ids = {int(m["target_row_id"]) for m in matches if m.get("target_row_id") is not None}
    for relation in payment_proxy_relations:
        from_account = relation.get("from_account")
        to_account = relation.get("to_account")
        from_keywords = (relation.get("match_keywords") or {}).get("from") or []
        to_keywords = (relation.get("match_keywords") or {}).get("to") or []
        source_candidates = [
            r
            for r in rows
            if r.get("account_code") == from_account
            and r["id"] not in used_proxy_source_ids
            and Decimal(str(r.get("amount") or "0")) < 0
            and (not from_keywords or _has_any(_row_text(r), from_keywords))
        ]
        target_candidates = [
            r
            for r in rows
            if r.get("account_code") == to_account
            and r["id"] not in used_proxy_target_ids
            and Decimal(str(r.get("amount") or "0")) < 0
            and (not to_keywords or _has_any(_row_text(r), to_keywords))
        ]
        target_row: dict[str, Any] | None
        for target_row in target_candidates:
            source_row, delta = _best_amount_date_match(
                target_row,
                source_candidates,
                used_ids=used_proxy_source_ids,
                date_window_days=1,
                tolerance=tolerance,
            )
            if not source_row:
                continue
            amount = _row_transfer_amount(target_row)
            used_proxy_source_ids.add(source_row["id"])
            used_proxy_target_ids.add(target_row["id"])
            if source_row.get("account_code") == "ccb_7332":
                used_bank_ids.add(source_row["id"])
            matches.append(
                _make_match(
                    match_type="payment_proxy",
                    relation_code=str(relation.get("code") or f"{from_account}_to_{to_account}_proxy"),
                    from_account_code=str(from_account),
                    to_account_code=str(to_account),
                    amount=amount,
                    source_row=source_row,
                    target_row=target_row,
                    date_delta_days=delta,
                    confidence=Decimal("0.95"),
                    note="同一笔外部消费：资金账户实际扣款，第三方支付账户只作支付通道",
                )
            )

    direct_investment_rows = [
        r
        for r in ccb_rows
        if r["id"] not in used_bank_ids
        and _has_any(_row_text(r), ["基金下账", "基金上账", "基金定投申购", "基金赎回款", "理财产品赎回", "理财赎回"])
    ]
    for row in direct_investment_rows:
        amount = _row_transfer_amount(row)
        if amount <= 0:
            continue
        is_outflow = Decimal(str(row.get("amount") or "0")) < 0
        matches.append(
            _make_match(
                match_type="investment_funding",
                relation_code="ccb_direct_investment_flow",
                from_account_code="ccb_7332" if is_outflow else "direct_investment",
                to_account_code="direct_investment" if is_outflow else "ccb_7332",
                amount=amount,
                source_row=row,
                target_row=None,
                date_delta_days=0,
                confidence=Decimal("0.75"),
                note="建行账单中的基金/理财申赎流水，当前未找到平台侧成对记录",
            )
        )

    source_only_investment_relations = [
        r
        for r in (config.get("account_relationships") or [])
        if r.get("relation_type") == "investment_funding" and r.get("match_mode") == "source_only"
    ]
    matched_source_ids = {int(m["source_row_id"]) for m in matches if m.get("source_row_id") is not None}
    for relation in source_only_investment_relations:
        from_account = relation.get("from_account")
        to_account = relation.get("to_account")
        from_keywords = (relation.get("match_keywords") or {}).get("from") or []
        for row in rows:
            if row.get("account_code") != from_account:
                continue
            if row["id"] in matched_source_ids:
                continue
            if Decimal(str(row.get("amount") or "0")) >= 0:
                continue
            if from_keywords and not _has_any(_row_text(row).upper(), [str(k).upper() for k in from_keywords]):
                continue
            amount = _row_transfer_amount(row)
            if amount <= 0:
                continue
            matched_source_ids.add(row["id"])
            matches.append(
                _make_match(
                    match_type="account_transfer",
                    relation_code=str(relation.get("code") or f"{from_account}_to_{to_account}"),
                    from_account_code=str(from_account),
                    to_account_code=str(to_account),
                    amount=amount,
                    source_row=row,
                    target_row=None,
                    date_delta_days=0,
                    confidence=Decimal("0.85"),
                    note="银行账单中的单边投资账户转账，投资平台侧到账记录缺失，暂按在途资金处理",
                )
            )

    benefit_income_relations = [
        r
        for r in (config.get("account_relationships") or [])
        if r.get("relation_type") == "benefit_income"
    ]
    matched_benefit_target_ids = {int(m["target_row_id"]) for m in matches if m.get("target_row_id") is not None}
    for relation in benefit_income_relations:
        from_account = relation.get("from_account")
        to_account = relation.get("to_account")
        to_keywords = (relation.get("match_keywords") or {}).get("to") or []
        for row in rows:
            if row.get("account_code") != to_account:
                continue
            if row["id"] in matched_benefit_target_ids:
                continue
            if Decimal(str(row.get("amount") or "0")) <= 0:
                continue
            if to_keywords and not _has_any(_row_text(row), to_keywords):
                continue
            amount = _row_transfer_amount(row)
            if amount <= 0:
                continue
            matched_benefit_target_ids.add(row["id"])
            matches.append(
                _make_match(
                    match_type="benefit_income",
                    relation_code=str(relation.get("code") or f"{from_account}_to_{to_account}"),
                    from_account_code=str(from_account),
                    to_account_code=str(to_account),
                    amount=amount,
                    source_row=None,
                    target_row=row,
                    date_delta_days=0,
                    confidence=Decimal("0.90"),
                    note="雇主福利/医保缴费入账，按医保台账单边确认",
                )
            )

    account_transfer_relations = [
        r
        for r in (config.get("account_relationships") or [])
        if r.get("relation_type") == "account_transfer"
    ]
    used_transfer_source_ids = {int(m["source_row_id"]) for m in matches if m.get("source_row_id") is not None}
    used_transfer_target_ids = {int(m["target_row_id"]) for m in matches if m.get("target_row_id") is not None}
    for relation in account_transfer_relations:
        from_account = relation.get("from_account")
        to_account = relation.get("to_account")
        from_keywords = (relation.get("match_keywords") or {}).get("from") or []
        to_keywords = (relation.get("match_keywords") or {}).get("to") or []
        source_candidates = [
            r
            for r in rows
            if r.get("account_code") == from_account
            and r["id"] not in used_transfer_source_ids
            and Decimal(str(r.get("amount") or "0")) < 0
            and (not from_keywords or _has_any(_row_text(r), from_keywords))
        ]
        target_candidates = [
            r
            for r in rows
            if r.get("account_code") == to_account
            and Decimal(str(r.get("amount") or "0")) > 0
            and (not to_keywords or _has_any(_row_text(r), to_keywords))
        ]
        for source_row in source_candidates:
            target_row, delta = _best_amount_date_match(
                source_row,
                target_candidates,
                used_ids=used_transfer_target_ids,
                date_window_days=date_window_days,
                tolerance=tolerance,
            )
            if not target_row:
                continue
            amount = _row_transfer_amount(source_row)
            used_transfer_source_ids.add(source_row["id"])
            used_transfer_target_ids.add(target_row["id"])
            matches.append(
                _make_match(
                    match_type="account_transfer",
                    relation_code=str(relation.get("code") or f"{from_account}_to_{to_account}"),
                    from_account_code=str(from_account),
                    to_account_code=str(to_account),
                    amount=amount,
                    source_row=source_row,
                    target_row=target_row,
                    date_delta_days=delta,
                    confidence=Decimal("0.98"),
                    note="银行账户之间内部转账，金额与日期匹配",
                )
            )

    credit_payment_relations = [
        r
        for r in (config.get("account_relationships") or [])
        if r.get("relation_type") == "credit_card_payment"
    ]
    for relation in credit_payment_relations:
        from_account = relation.get("from_account")
        to_account = relation.get("to_account")
        from_keywords = ((relation.get("match_keywords") or {}).get("from") or []) + ["信用卡", "还款"]
        to_keywords = ((relation.get("match_keywords") or {}).get("to") or []) + ["还款"]
        bank_candidates = [
            r
            for r in rows
            if r.get("account_code") == from_account
            and r["id"] not in used_bank_ids
            and Decimal(str(r.get("amount") or "0")) < 0
            and _has_any(_row_text(r), from_keywords)
        ]
        credit_repayments = [
            r
            for r in rows
            if r.get("account_code") == to_account
            and Decimal(str(r.get("amount") or "0")) > 0
            and _has_any(_row_text(r), to_keywords)
        ]
        for credit_row in credit_repayments:
            repayment_amount = _row_cny_transfer_amount(credit_row)
            bank_row, delta = _best_amount_date_match(
                credit_row,
                bank_candidates,
                used_ids=used_bank_ids,
                date_window_days=date_window_days,
                tolerance=tolerance,
                reference_amount=repayment_amount,
            )
            if not bank_row:
                continue
            used_bank_ids.add(bank_row["id"])
            matches.append(
                _make_match(
                    match_type="credit_card_payment",
                    relation_code=str(relation.get("code") or f"{from_account}_to_{to_account}"),
                    from_account_code=str(from_account),
                    to_account_code=str(to_account),
                    amount=repayment_amount,
                    source_row=bank_row,
                    target_row=credit_row,
                    date_delta_days=delta,
                    confidence=Decimal("0.97"),
                    note="银行扣款与信用卡账单还款记录金额匹配",
                )
            )

    matches.sort(key=lambda m: (m["match_date"] or date.min, m["match_type"], m["source_row_id"] or 0), reverse=True)
    return matches




def list_candidates(cur, config, *, limit=1000):
    """Suggest only; matching never constitutes confirmation or de-duplication."""
    from domain.ledger import bank_source_read, confirmed_transfers
    existing = confirmed_transfers.read(cur, limit=10000)
    linked = {leg['id'] for record in existing if record['evidence_status'] == 'confirmed'
              for leg in (record['from_evidence'], record['to_evidence']) if leg['kind'] == 'cashflow'}
    from domain.ledger.payment_proxy_confirmation import decisions
    for record in decisions(cur):
        if record['evidence_status'] == 'confirmed':
            linked.update((record['from_cashflow_id'],record['to_cashflow_id']))
    raw = bank_source_read.rows(cur, limit=limit)
    rows = []
    for item in raw:
        if item.get('source_evidence_status') != 'verified' or item['cashflow_id'] in linked:
            continue
        source = item.get('source') if isinstance(item.get('source'), dict) else {}
        original = source.get('statement_row') if isinstance(source.get('statement_row'), dict) else {}
        rows.append({**item, 'id':item['cashflow_id'], 'transaction_date':item['event_date'],
                     'raw_json':original.get('raw') or {},
                     'original_amount':original.get('original_amount'),
                     'original_currency':original.get('original_currency')})
    suggestions = _build_transfer_matches(rows, config)
    by_id = {row['id']:row for row in rows}
    for candidate in suggestions:
        source, target = by_id.get(candidate.get('source_row_id')), by_id.get(candidate.get('target_row_id'))
        candidate['status'] = 'review_required'
        candidate['confirmation_supported'] = bool(source and target and source['account_code'] != target['account_code']
            and source['currency'] == target['currency'] and source['amount'] < 0 and target['amount'] > 0
            and -source['amount'] == target['amount'])
        # Old heuristics choose a closest row; they cannot prove uniqueness.
        # Treat every suggestion as a candidate until the user verifies evidence.
        if candidate['match_type'] == 'payment_proxy' and source and target:
            candidate['confirmation_supported'] = (source['account_code'] != target['account_code']
                and source['currency'] == target['currency'] and source['event_date'] == target['event_date']
                and source['amount'] < 0 and source['amount'] == target['amount'])
        candidate['ambiguity_status'] = 'unverified'
    return suggestions
