"""Regression coverage for statement formats using synthetic records only."""

from datetime import date
from decimal import Decimal
from pathlib import Path
import sys
import zipfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend' / 'api'))
from collectors.bank_statement_parser import (  # noqa: E402
    _parse_guangfa_credit_transaction_line,
    _parse_icbc_debit_row,
    _parse_santander_account_row,
    parse_alipay_csv_zip,
    parse_manual_statement_csv,
)


def test_alipay_zip_preserves_cashflow_signs(tmp_path):
    text = (
        '支付宝账户：TEST-ACCOUNT\n'
        '起始时间：[2025-01-01 00:00:00]    终止时间：[2025-01-31 23:59:59]\n'
        '交易时间,交易分类,交易对方,对方账号,商品说明,收/支,金额,收/付款方式,交易状态,交易订单号,商家订单号,备注,\n'
        '2025-01-10 08:00:00,消费,示例商户,/,示例商品,支出,12.50,余额,交易成功,T1,M1,,\n'
        '2025-01-11 08:00:00,账户存取,示例银行,/,提现,不计收支,30.00,余额,交易成功,T2,M2,,\n'
        '2025-01-12 08:00:00,转账,示例对方,/,收款,收入,50.00,余额,交易成功,T3,M3,,\n'
    )
    path = tmp_path / 'synthetic.zip'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('statement.csv', text.encode('gb18030'))
    result = parse_alipay_csv_zip(path, {'profile_code': 'synthetic', 'account_code': 'test', 'currency': 'CNY'})
    assert result.account_tail == 'TEST-ACCOUNT'
    assert [row['amount'] for row in result.rows] == [Decimal('-12.50'), Decimal('-30.00'), Decimal('50.00')]


@pytest.mark.parametrize(('side', 'amount'), [('withdrawal', '-125.00'), ('deposit', '125.00')])
def test_santander_csv_keeps_signed_amount(side, amount):
    withdrawal, deposit = ('$ -125.00', '-') if side == 'withdrawal' else ('-', '$ 125.00')
    row = _parse_santander_account_row(
        ['10/Ene/2025', '08:00:00', 'TEST', 'SYNTHETIC', withdrawal, deposit, '$ 500.00', '1'],
        row_idx=1, path=Path('synthetic.csv'),
        profile={'profile_code': 'test', 'account_code': 'test', 'currency': 'MXN'},
    )
    assert row is not None
    assert row['transaction_date'] == date(2025, 1, 10)
    assert row['amount'] == Decimal(amount)
    assert row['currency'] == 'MXN'
    assert row['balance'] == Decimal('500.00')


def test_icbc_pdf_row_handles_watermark_and_currency_filter():
    fields = ['2025-01-10\n08:00:00', '00000001', '活期', '00000', '人民币', '钞',
              '理4财', 'E\n3 602', '6\nD\n-25.00', '100.00', '示例商户', '0000', '快捷支付']
    profile = {'profile_code': 'test', 'account_code': 'test', 'currency_filter': 'CNY'}
    row = _parse_icbc_debit_row(fields, page_idx=0, row_idx=1, path=Path('synthetic.pdf'), profile=profile)
    assert row is not None
    assert row['transaction_date'] == date(2025, 1, 10)
    assert row['amount'] == Decimal('-25.00')
    assert row['balance'] == Decimal('100.00')
    assert row['summary'] == '理财'
    assert _parse_icbc_debit_row(fields, page_idx=0, row_idx=1, path=Path('synthetic.pdf'),
                                 profile={**profile, 'currency_filter': 'USD'}) is None


def test_icbc_pdf_row_preserves_foreign_currency():
    fields = ['2025-01-10\n08:00:00', '00000001', '活期', '00000', '1\n美3元', '汇',
              '示例', '0000', '+15.00', 'E\n2\n2\nA15.00', 'SYNTHETIC', '0000', '柜面']
    profile = {'profile_code': 'test', 'account_code': 'test', 'currency_filter': 'USD'}
    row = _parse_icbc_debit_row(fields, page_idx=0, row_idx=1, path=Path('synthetic.pdf'), profile=profile)
    assert row is not None
    assert row['amount'] == Decimal('15.00')
    assert row['balance'] == Decimal('15.00')
    assert row['currency'] == 'USD'


def test_guangfa_credit_row_preserves_both_currencies():
    row = _parse_guangfa_credit_transaction_line(
        '2025/01/10 2025/01/11 (还款)银联跨行购汇还_示例银行 -70.00 人民币 -10.00 美元'
    )
    assert row is not None
    assert row['transaction_date'] == date(2025, 1, 10)
    assert row['original_amount'] == Decimal('-70.00')
    assert row['original_currency'] == 'CNY'
    assert row['settlement_amount'] == Decimal('-10.00')
    assert row['settlement_currency'] == 'USD'


def test_guangfa_credit_compact_foreign_currency():
    row = _parse_guangfa_credit_transaction_line(
        '2025/01/10 2025/01/11 (消费)SYNTHETIC MERCHANT 70.00墨西哥比索 4.00 美元'
    )
    assert row is not None
    assert row['original_amount'] == Decimal('70.00')
    assert row['original_currency'] == 'MXN'
    assert row['settlement_amount'] == Decimal('4.00')
    assert row['settlement_currency'] == 'USD'


def test_manual_csv_filters_rows_and_retains_latest_balance(tmp_path):
    path = tmp_path / 'synthetic.csv'
    path.write_text('transaction_date,summary,amount,balance,currency,description,counterparty\n'
                    '2025-01-01,示例支出,-25.00,,CNY,示例,示例对方\n'
                    '2025-01-15,示例收入,50.00,125.00,CNY,示例,示例对方\n', encoding='utf-8')
    profile = {'profile_code': 'test', 'account_code': 'test', 'currency': 'CNY'}
    full = parse_manual_statement_csv(path, profile)
    filtered = parse_manual_statement_csv(path, profile, since=date(2025, 1, 10))
    assert [row['amount'] for row in full.rows] == [Decimal('-25.00'), Decimal('50.00')]
    assert [row['transaction_date'] for row in filtered.rows] == [date(2025, 1, 15)]
    assert filtered.statement_balance == Decimal('125.00')
