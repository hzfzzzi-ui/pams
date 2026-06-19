-- Backfill confirmed fund trade fees provided by the user on 2026-06-07.
-- Keep net_cash unchanged. For buys, split cash outflow into gross + fee.
-- For sells, keep net proceeds and add the fee back to gross proceeds.

WITH rules AS (
    SELECT *
    FROM (
        VALUES
            ('501016', '国泰中证申万证券行业指数(LOF)A', 'buy', 0.001199::numeric, '国泰中证申万证券行业指数(LOF)A 买入手续费 0.1199%'),
            ('016440', '华夏中证红利质量ETF发起式联接A', 'buy', 0.001199::numeric, '华夏中证红利质量ETF发起式联接A 买入手续费 0.1199%'),
            ('025197', '广发恒生港股通科技主题ETF发起式联接A', 'buy', 0.0005::numeric, '广发恒生港股通科技主题ETF发起式联接A 买入手续费 0.05%'),
            (NULL, '国投瑞银白银期货(LOF)A', 'sell', 0.0005::numeric, '国投瑞银白银期货(LOF)A 卖出手续费 0.05%'),
            (NULL, '华安黄金ETF联接A', 'sell', 0.001::numeric, '华安黄金ETF联接A 卖出手续费 0.1%'),
            ('001412', '德邦鑫星价值灵活配置混合A', 'buy', 0.0015::numeric, '德邦鑫星价值灵活配置混合A 买入手续费按每 1000 元 1.5 元折算'),
            ('016186', '广发中证电力公用事业ETF联接C', 'buy', 0::numeric, '广发中证电力公用事业ETF联接C 买入手续费确认 0'),
            ('011613', '华夏科创50ETF联接C', 'buy', 0::numeric, '华夏科创50ETF联接C 买入手续费确认 0')
    ) AS r(symbol, name, trade_type, fee_rate, note)
),
matched AS (
    SELECT
        f.trade_id,
        f.trade_type,
        r.fee_rate,
        r.note,
        round(abs(COALESCE(f.net_cash, f.gross_amount, 0)) * r.fee_rate, 2) AS fee_amount_new,
        abs(COALESCE(f.net_cash, f.gross_amount, 0)) AS cash_amount
    FROM pams_investment_trade_fact f
    JOIN rules r
      ON f.trade_type = r.trade_type
     AND (
        (r.symbol IS NOT NULL AND f.symbol = r.symbol)
        OR f.name = r.name
     )
    WHERE f.source_table = 'pams_investment_transactions'
      AND COALESCE(f.fee_amount, 0) = 0
      AND COALESCE(f.tax_amount, 0) = 0
)
UPDATE pams_investment_trade_fact f
SET
    fee_amount = m.fee_amount_new,
    tax_amount = 0,
    gross_amount = CASE
        WHEN m.trade_type IN ('buy', 'fund_subscription') THEN m.cash_amount - m.fee_amount_new
        WHEN m.trade_type IN ('sell', 'fund_redemption') THEN m.cash_amount + m.fee_amount_new
        ELSE f.gross_amount
    END,
    notes = CASE
        WHEN COALESCE(f.notes, '') LIKE '%manual_fee_backfill_20260607_fund_rules%' THEN f.notes
        WHEN COALESCE(f.notes, '') = '' THEN 'manual_fee_backfill_20260607_fund_rules: ' || m.note
        ELSE f.notes || E'\nmanual_fee_backfill_20260607_fund_rules: ' || m.note
    END,
    updated_at = now()
FROM matched m
WHERE f.trade_id = m.trade_id
  AND (
    m.fee_amount_new <> 0
    OR COALESCE(f.notes, '') NOT LIKE '%manual_fee_backfill_20260607_fund_rules%'
  );
