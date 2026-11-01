from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend" / "api"))

from domain.ledger.transfer_candidates import _build_transfer_matches  # noqa: E402
from domain.ledger.bank_statement_rules import _row_transfer_amount  # noqa: E402


def _row(
    row_id: int,
    account_code: str,
    amount: str,
    *,
    original_amount: str | None = None,
    original_currency: str | None = None,
    summary: str = "",
    description: str = "",
    day: date = date(2026, 5, 22),
) -> dict:
    return {
        "id": row_id,
        "account_code": account_code,
        "transaction_date": day,
        "amount": Decimal(amount),
        "original_amount": Decimal(original_amount) if original_amount is not None else None,
        "original_currency": original_currency,
        "currency": "CNY",
        "summary": summary,
        "description": description,
        "category": "未分类",
        "subcategory": None,
        "raw_json": {},
    }


def test_row_transfer_amount_uses_original_amount_for_neutral_platform_rows():
    row = _row(1, "wechat", "0", original_amount="4888.15")

    assert _row_transfer_amount(row) == Decimal("4888.15")


def test_lqt_regular_payment_is_not_investment_funding():
    row = _row(
        1,
        "wechat",
        "-21",
        original_amount="21",
        summary="商户消费",
        description="iCloud 由云上贵州运营 零钱通 支付成功",
    )

    assert _build_transfer_matches([row], {}) == []


def test_lqt_licaitong_neutral_row_is_investment_funding():
    row = _row(
        1,
        "wechat",
        "0",
        original_amount="10",
        summary="购买理财通",
        description="理财通 建信纳斯达克100指数(QDII)A人民币(539001) 零钱通 支付成功",
    )

    matches = _build_transfer_matches([row], {})

    assert len(matches) == 1
    assert matches[0]["relation_code"] == "wechat_lqt_to_tencent_licaitong"
    assert matches[0]["amount"] == Decimal("10")


def test_bank_card_licaitong_pair_is_matched_by_amount_and_date():
    bank = _row(
        1,
        "ccb_7332",
        "-10000",
        summary="消费",
        description="消费 财付通-微信支付-腾安基金销售（深圳）有限公司",
    )
    wechat = _row(
        2,
        "wechat",
        "0",
        original_amount="10000",
        summary="购买理财通",
        description="理财通 国泰中证申万证券行业指数(LOF)A(501016) 建设银行储蓄卡(7332) 支付成功",
    )

    matches = _build_transfer_matches([bank, wechat], {})

    assert len(matches) == 1
    assert matches[0]["relation_code"] == "ccb_to_tencent_licaitong"
    assert matches[0]["source_row_id"] == 1
    assert matches[0]["target_row_id"] == 2


def test_guangfa_usd_card_payment_matches_bank_by_original_cny_amount():
    bank = _row(
        1,
        "ccb_7332",
        "-1478.01",
        summary="跨行代收",
        description="跨行代收 信用卡还款 广发银行（信用卡还款）",
        day=date(2026, 1, 18),
    )
    credit = _row(
        2,
        "gf_9711",
        "211.60",
        original_amount="1478.01",
        original_currency="CNY",
        summary="信用卡还款",
        description="(还款)银联跨行购汇还_微信银行",
        day=date(2026, 1, 18),
    )

    matches = _build_transfer_matches(
        [bank, credit],
        {
            "account_relationships": [
                {
                    "code": "ccb_to_gf_credit_9711",
                    "relation_type": "credit_card_payment",
                    "from_account": "ccb_7332",
                    "to_account": "gf_9711",
                    "match_keywords": {
                        "from": ["广发银行", "信用卡还款"],
                        "to": ["信用卡还款"],
                    },
                }
            ]
        },
    )

    assert len(matches) == 1
    assert matches[0]["match_type"] == "credit_card_payment"
    assert matches[0]["relation_code"] == "ccb_to_gf_credit_9711"
    assert matches[0]["amount"] == Decimal("1478.01")


def test_santander_ibkr_source_only_is_pending_account_transfer():
    row = _row(
        1,
        "santander",
        "-90000",
        summary="TRANSFERENCIA A INTERACTIVE BROKERS LLC",
        description="TRANSFERENCIA A INTERACTIVE BROKERS LLC",
    )
    row["currency"] = "MXN"
    row["original_currency"] = "MXN"

    matches = _build_transfer_matches(
        [row],
        {
            "account_relationships": [
                {
                    "code": "santander_to_ibkr",
                    "relation_type": "investment_funding",
                    "match_mode": "source_only",
                    "from_account": "santander",
                    "to_account": "ibkr",
                    "match_keywords": {"from": ["INTERACTIVE BROKERS"]},
                }
            ]
        },
    )

    assert len(matches) == 1
    assert matches[0]["match_type"] == "account_transfer"
    assert matches[0]["relation_code"] == "santander_to_ibkr"
    assert matches[0]["from_account_code"] == "santander"
    assert matches[0]["to_account_code"] == "ibkr"
    assert matches[0]["currency"] == "MXN"
    assert matches[0]["amount"] == Decimal("90000")


def test_ccb_to_tonghuashun_source_only_is_pending_account_transfer():
    row = _row(
        1,
        "ccb_7332",
        "-50000",
        summary="跨行转出",
        description="跨行转出 跨行转出 6230220350071687/沈钰棋",
    )

    matches = _build_transfer_matches(
        [row],
        {
            "account_relationships": [
                {
                    "code": "ccb_to_tonghuashun",
                    "relation_type": "investment_funding",
                    "match_mode": "source_only",
                    "from_account": "ccb_7332",
                    "to_account": "tonghuashun",
                    "match_keywords": {"from": ["6230220350071687", "沈钰棋"]},
                }
            ]
        },
    )

    assert len(matches) == 1
    assert matches[0]["match_type"] == "account_transfer"
    assert matches[0]["relation_code"] == "ccb_to_tonghuashun"
    assert matches[0]["from_account_code"] == "ccb_7332"
    assert matches[0]["to_account_code"] == "tonghuashun"
    assert matches[0]["amount"] == Decimal("50000")


def test_icbc_to_ccb_account_transfer_matches_by_amount_and_date():
    source = _row(
        1,
        "icbc_6689_cny",
        "-15193.76",
        summary="跨行汇款",
        description="跨行汇款 黄展峰 6217003370010267332 手机银行",
        day=date(2026, 1, 18),
    )
    target = _row(
        2,
        "ccb_7332",
        "15193.76",
        summary="汇兑",
        description="汇兑 电子汇入 6212263602103766689/黄展峰",
        day=date(2026, 1, 18),
    )

    matches = _build_transfer_matches(
        [source, target],
        {
            "account_relationships": [
                {
                    "code": "icbc_to_ccb_7332",
                    "relation_type": "account_transfer",
                    "from_account": "icbc_6689_cny",
                    "to_account": "ccb_7332",
                    "match_keywords": {
                        "from": ["跨行汇款"],
                        "to": ["电子汇入"],
                    },
                }
            ]
        },
    )

    assert len(matches) == 1
    assert matches[0]["match_type"] == "account_transfer"
    assert matches[0]["relation_code"] == "icbc_to_ccb_7332"
    assert matches[0]["amount"] == Decimal("15193.76")


def test_hpf_withdrawal_matches_ccb_receipt_as_account_transfer():
    source = _row(
        1,
        "hpf",
        "-46655.00",
        summary="租房提取",
        description="公积金租房提取 深圳市住房公积金管理中心",
        day=date(2026, 4, 22),
    )
    target = _row(
        2,
        "ccb_7332",
        "46655.00",
        summary="代理付款",
        description="代理付款 提取托付 44201501100052523456/深圳市住房公积金管理中心",
        day=date(2026, 4, 22),
    )

    matches = _build_transfer_matches(
        [source, target],
        {
            "account_relationships": [
                {
                    "code": "hpf_to_ccb_7332",
                    "relation_type": "account_transfer",
                    "from_account": "hpf",
                    "to_account": "ccb_7332",
                    "match_keywords": {
                        "from": ["租房提取", "公积金租房提取"],
                        "to": ["深圳市住房公积金管理中心", "提取托付", "代理付款"],
                    },
                },
                {
                    "code": "hpf_to_wechat",
                    "relation_type": "account_transfer",
                    "from_account": "hpf",
                    "to_account": "wechat",
                    "match_keywords": {"from": ["租房提取", "公积金租房提取"]},
                },
            ]
        },
    )

    assert len(matches) == 1
    assert matches[0]["match_type"] == "account_transfer"
    assert matches[0]["relation_code"] == "hpf_to_ccb_7332"
    assert matches[0]["amount"] == Decimal("46655.00")


def test_alipay_withdrawal_matches_ccb_receipt_as_account_transfer():
    source = _row(
        1,
        "alipay",
        "-16103.00",
        summary="账户存取",
        description="账户存取 中国建设银行 提现-实时提现 余额 交易成功",
        day=date(2026, 2, 17),
    )
    target = _row(
        2,
        "ccb_7332",
        "16103.00",
        summary="支付机构提现",
        description="支付机构提现 黄展峰支付宝余额提现 20885426636848110156/黄展峰",
        day=date(2026, 2, 17),
    )

    matches = _build_transfer_matches(
        [source, target],
        {
            "account_relationships": [
                {
                    "code": "alipay_to_ccb_7332",
                    "relation_type": "account_transfer",
                    "from_account": "alipay",
                    "to_account": "ccb_7332",
                    "match_keywords": {
                        "from": ["提现", "实时提现", "账户存取"],
                        "to": ["支付宝余额提现", "支付机构提现"],
                    },
                }
            ]
        },
    )

    assert len(matches) == 1
    assert matches[0]["match_type"] == "account_transfer"
    assert matches[0]["relation_code"] == "alipay_to_ccb_7332"
    assert matches[0]["amount"] == Decimal("16103.00")


def test_alipay_card_payment_proxy_matches_actual_bank_debit():
    source = _row(
        1,
        "ccb_7332",
        "-35.00",
        summary="消费",
        description="消费 支付宝-支付宝外部商户-银河互联网电视有限公司",
        day=date(2026, 5, 1),
    )
    target = _row(
        2,
        "alipay",
        "-35.00",
        summary="文化休闲",
        description="文化休闲 银河互联网电视 小米影视会员连续包月 建设银行储蓄卡(7332) 交易成功",
        day=date(2026, 5, 1),
    )

    matches = _build_transfer_matches(
        [source, target],
        {
            "account_relationships": [
                {
                    "code": "ccb_alipay_payment_proxy",
                    "relation_type": "payment_proxy",
                    "from_account": "ccb_7332",
                    "to_account": "alipay",
                    "match_keywords": {
                        "from": ["支付宝"],
                        "to": ["建设银行储蓄卡(7332)"],
                    },
                }
            ]
        },
    )

    assert len(matches) == 1
    assert matches[0]["match_type"] == "payment_proxy"
    assert matches[0]["relation_code"] == "ccb_alipay_payment_proxy"
    assert matches[0]["amount"] == Decimal("35.00")


def test_medical_insurance_benefit_income_matches_single_sided_ledger_row():
    row = _row(
        1,
        "medical_insurance",
        "470.00",
        summary="医保个人账户入账",
        description="本月进入个人账户金额；缴费结构：个人470.00，单位基本医保1410.00，单位生育保险117.50",
        day=date(2026, 5, 31),
    )

    matches = _build_transfer_matches(
        [row],
        {
            "account_relationships": [
                {
                    "code": "huawei_to_medical_insurance",
                    "relation_type": "benefit_income",
                    "from_account": "huawei_employer",
                    "to_account": "medical_insurance",
                    "match_keywords": {"to": ["医疗保险", "生育保险", "医保"]},
                }
            ]
        },
    )

    assert len(matches) == 1
    assert matches[0]["match_type"] == "benefit_income"
    assert matches[0]["relation_code"] == "huawei_to_medical_insurance"
    assert matches[0]["from_account_code"] == "huawei_employer"
    assert matches[0]["to_account_code"] == "medical_insurance"
    assert matches[0]["source_row_id"] is None
    assert matches[0]["target_row_id"] == 1
    assert matches[0]["amount"] == Decimal("470.00")
