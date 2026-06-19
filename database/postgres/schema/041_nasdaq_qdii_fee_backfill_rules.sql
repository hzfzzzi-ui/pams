-- Backfill confirmed Nasdaq QDII fund trade fees provided by the user on 2026-06-08.
-- Keep net_cash unchanged. For buys, split cash outflow into gross + fee.
-- For sells, keep net proceeds and add the fee back to gross proceeds.

WITH base AS (
    SELECT
        f.trade_id,
        f.trade_type,
        f.quantity,
        abs(COALESCE(f.net_cash, f.gross_amount, 0)) AS cash_amount,
        CASE
            WHEN f.trade_id = 163 THEN 0.56::numeric
            WHEN f.trade_id = 164 THEN 0.78::numeric
            WHEN f.trade_type = 'buy'
             AND f.name IN (
                '建信纳斯达克100指数(QDII)A人民币',
                '广发纳斯达克100ETF联接人民币(QDII)A'
             )
                THEN round(abs(COALESCE(f.net_cash, f.gross_amount, 0)) * 0.001, 2)
            ELSE 0::numeric
        END AS fee_amount_new,
        CASE
            WHEN f.name = '建信纳斯达克100指数(QDII)C人民币'
                THEN '建信纳斯达克100指数(QDII)C人民币买入卖出手续费确认 0'
            WHEN f.name = '华泰柏瑞纳斯达克100ETF发起式联接(QDII)I'
                THEN '华泰柏瑞纳斯达克100ETF发起式联接买入卖出手续费确认 0'
            WHEN f.trade_type = 'buy'
             AND f.name = '建信纳斯达克100指数(QDII)A人民币'
                THEN '建信纳斯达克100指数(QDII)A人民币买入手续费按每 10 元 0.01 元折算'
            WHEN f.trade_id = 163
                THEN '建信纳斯达克100指数(QDII)A人民币卖出 146.89 元，手续费 0.56 元'
            WHEN f.trade_type = 'buy'
             AND f.name = '广发纳斯达克100ETF联接人民币(QDII)A'
                THEN '广发纳斯达克100ETF联接人民币(QDII)A买入手续费按每 10 元 0.01 元折算'
            WHEN f.trade_id = 164
                THEN '广发纳斯达克100ETF联接人民币(QDII)A卖出 156.50 元，手续费 0.78 元'
            ELSE '纳指 QDII 基金手续费手工确认'
        END AS note
    FROM pams_investment_trade_fact f
    WHERE f.source_table = 'pams_investment_transactions'
      AND f.trade_date BETWEEN DATE '2026-01-21' AND DATE '2026-03-04'
      AND COALESCE(f.fee_amount, 0) = 0
      AND COALESCE(f.tax_amount, 0) = 0
      AND (
        (
            f.name = '建信纳斯达克100指数(QDII)C人民币'
            AND f.trade_type IN ('buy', 'sell')
        )
        OR (
            f.name = '华泰柏瑞纳斯达克100ETF发起式联接(QDII)I'
            AND f.trade_type IN ('buy', 'sell')
        )
        OR (
            f.name = '建信纳斯达克100指数(QDII)A人民币'
            AND f.trade_type = 'buy'
        )
        OR (
            f.name = '广发纳斯达克100ETF联接人民币(QDII)A'
            AND f.trade_type = 'buy'
        )
        OR (
            f.trade_id = 163
            AND f.name = '建信纳斯达克100指数(QDII)A人民币'
            AND f.trade_type = 'sell'
            AND abs(COALESCE(f.net_cash, 0)) = 146.33
        )
        OR (
            f.trade_id = 164
            AND f.name = '广发纳斯达克100ETF联接人民币(QDII)A'
            AND f.trade_type = 'sell'
            AND abs(COALESCE(f.net_cash, 0)) = 155.72
        )
      )
),
matched AS (
    SELECT
        trade_id,
        trade_type,
        quantity,
        fee_amount_new,
        CASE
            WHEN trade_type IN ('buy', 'fund_subscription') THEN cash_amount - fee_amount_new
            WHEN trade_type IN ('sell', 'fund_redemption') THEN cash_amount + fee_amount_new
            ELSE cash_amount
        END AS gross_amount_new,
        note
    FROM base
)
UPDATE pams_investment_trade_fact f
SET
    fee_amount = m.fee_amount_new,
    tax_amount = 0,
    gross_amount = m.gross_amount_new,
    price = CASE
        WHEN f.quantity IS NOT NULL AND f.quantity <> 0 THEN m.gross_amount_new / abs(f.quantity)
        ELSE f.price
    END,
    notes = CASE
        WHEN COALESCE(f.notes, '') LIKE '%manual_fee_backfill_20260608_nasdaq_qdii_rules%' THEN f.notes
        WHEN COALESCE(f.notes, '') = '' THEN 'manual_fee_backfill_20260608_nasdaq_qdii_rules: ' || m.note
        ELSE f.notes || E'\nmanual_fee_backfill_20260608_nasdaq_qdii_rules: ' || m.note
    END,
    updated_at = now()
FROM matched m
WHERE f.trade_id = m.trade_id
  AND (
    m.fee_amount_new <> 0
    OR COALESCE(f.notes, '') NOT LIKE '%manual_fee_backfill_20260608_nasdaq_qdii_rules%'
  );
