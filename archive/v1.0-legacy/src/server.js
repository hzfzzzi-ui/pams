const path = require('path');
const express = require('express');
const dayjs = require('dayjs');
const { ensureDb, dbPath } = require('./db');

const app = express();
app.use(express.json({ limit: '2mb' }));

// Allow the static dashboard (served from :8080) to call this API (served from :18888)
app.use((req, res, next) => {
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET,POST,OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type');
  if (req.method === 'OPTIONS') return res.sendStatus(204);
  next();
});

const db = ensureDb();
const port = process.env.PAMS_PORT || 18888;

// Serve dashboard
app.use('/', express.static(path.join(__dirname, '..', 'web')));

app.get('/api/health', (req, res) => {
  res.json({ ok: true, dbPath });
});

async function getFxRate(asOfDate, from, to) {
  if (from === to) return 1;
  const pair = `${from}/${to}`;
  const row = db.prepare('SELECT rate FROM fx_rates WHERE asOfDate = ? AND pair = ?').get(asOfDate, pair);
  if (row && Number.isFinite(Number(row.rate))) return Number(row.rate);

  // Auto-fetch from a public endpoint (no key). Frankfurter returns the previous business day when markets closed.
  const url = `https://api.frankfurter.app/${asOfDate}?from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}`;
  const r = await fetch(url);
  if (!r.ok) throw new Error(`FX fetch failed ${r.status}: ${url}`);
  const j = await r.json();
  const rate = Number(j?.rates?.[to]);
  if (!Number.isFinite(rate)) throw new Error(`Bad FX payload for ${pair} on ${asOfDate}`);

  db.prepare(
    `INSERT INTO fx_rates (asOfDate, pair, rate, source)
     VALUES (?, ?, ?, ?)
     ON CONFLICT(asOfDate, pair) DO UPDATE SET rate=excluded.rate, source=excluded.source`
  ).run(asOfDate, pair, rate, 'frankfurter');

  return rate;
}

function classifyBucket(p) {
  if (p.accountKind === 'liability' || p.symbolType === 'liability') return 'liability';
  if (p.accountKind === 'receivable') return 'receivable';
  const t = p.symbolType;
  if (t === 'cash') return 'cash';
  if (t === 'fixed_income') return 'fixed_income';
  if (t === 'gold') return 'gold_commodities';
  if (t === 'crypto') return 'crypto';
  if (t === 'esop') return 'esop';
  if (t === 'stock' || t === 'etf' || t === 'fund') return 'equity';
  return 'other';
}

app.get('/api/overview', async (req, res) => {
  const asOfDate = req.query.asOfDate || dayjs().format('YYYY-MM-DD');
  const baseCurrency = (req.query.baseCurrency || 'CNY').toUpperCase();

  const positionsRaw = db.prepare(
    `SELECT p.*, a.kind as accountKind, a.name as accountName, a.currency as accountCurrency
     FROM positions p JOIN accounts a ON a.id = p.accountId
     WHERE p.asOfDate = ?`
  ).all(asOfDate);

  // Convert everything into baseCurrency (CNY) using fx_rates; auto-fetch if missing.
  const fxUsed = {};
  const positions = [];
  for (const p of positionsRaw) {
    const from = (p.accountCurrency || baseCurrency).toUpperCase();
    const rate = await getFxRate(asOfDate, from, baseCurrency);
    fxUsed[`${from}/${baseCurrency}`] = rate;
    const mv = Number(p.marketValue ?? 0);
    positions.push({
      ...p,
      marketValueBase: mv * rate,
      baseCurrency,
      fxRateToBase: rate,
    });
  }

  const buckets = {
    cash: 0,
    fixed_income: 0,
    equity: 0,
    gold_commodities: 0,
    crypto: 0,
    esop: 0,
    receivable: 0,
    liability: 0,
    other: 0,
  };

  for (const p of positions) {
    const v = Number(p.marketValueBase ?? 0);
    const b = classifyBucket(p);
    buckets[b] += v;
  }

  const totalAssets = Object.entries(buckets)
    .filter(([k]) => k !== 'liability')
    .reduce((acc, [, v]) => acc + (v > 0 ? v : 0), 0);
  const totalLiabilities = buckets.liability; // liabilities are negative in our sheet
  const netWorth = totalAssets + totalLiabilities;

  res.json({ asOfDate, baseCurrency, fxUsed, buckets, totalAssets, totalLiabilities, netWorth, positions });
});

// Manual refresh endpoint (optional): prefetch common FX pairs into DB.
app.get('/api/nav/series', async (req, res) => {
  const baseCurrency = (req.query.baseCurrency || 'CNY').toUpperCase();
  const limit = Number(req.query.limit || 90);

  const dates = db.prepare(
    `SELECT asOfDate
     FROM positions
     GROUP BY asOfDate
     ORDER BY asOfDate ASC
     LIMIT ?`
  ).all(limit).map(x=>x.asOfDate);

  const rows = [];
  for (const asOfDate of dates) {
    const pos = db.prepare(
      `SELECT p.marketValue, a.currency as accountCurrency, a.kind as accountKind
       FROM positions p JOIN accounts a ON a.id = p.accountId
       WHERE p.asOfDate = ?`
    ).all(asOfDate);

    let totalAssets = 0;
    let totalLiabilities = 0;

    for (const p of pos) {
      const from = (p.accountCurrency || baseCurrency).toUpperCase();
      const rate = await getFxRate(asOfDate, from, baseCurrency);
      const mv = Number(p.marketValue ?? 0) * rate;
      if (p.accountKind === 'liability') totalLiabilities += mv; // negative
      else totalAssets += mv;
    }

    const netWorth = totalAssets + totalLiabilities;
    rows.push({ asOfDate, totalAssets, totalLiabilities, netWorth });
  }

  res.json({ ok: true, baseCurrency, rows });
});

app.get('/api/nav/drawdown', async (req, res) => {
  const baseCurrency = (req.query.baseCurrency || 'CNY').toUpperCase();
  const limit = Number(req.query.limit || 365);
  const dates = db.prepare(
    `SELECT asOfDate FROM positions GROUP BY asOfDate ORDER BY asOfDate ASC LIMIT ?`
  ).all(limit).map(x=>x.asOfDate);

  const rows = [];
  for (const asOfDate of dates) {
    const pos = db.prepare(
      `SELECT p.marketValue, a.currency as accountCurrency, a.kind as accountKind
       FROM positions p JOIN accounts a ON a.id = p.accountId
       WHERE p.asOfDate = ?`
    ).all(asOfDate);

    let totalAssets = 0;
    let totalLiabilities = 0;
    for (const p of pos) {
      const from = (p.accountCurrency || baseCurrency).toUpperCase();
      const rate = await getFxRate(asOfDate, from, baseCurrency);
      const mv = Number(p.marketValue ?? 0) * rate;
      if (p.accountKind === 'liability') totalLiabilities += mv;
      else totalAssets += mv;
    }
    rows.push({ asOfDate, netWorth: totalAssets + totalLiabilities });
  }

  let peak = -Infinity;
  const out = rows.map(r => {
    const v = Number(r.netWorth ?? 0);
    if (v > peak) peak = v;
    const dd = peak > 0 ? (v / peak - 1) : 0;
    return {
      as_of: r.asOfDate,
      total_market_value: v,
      drawdown: dd,
      completeness: rows.length < 2 ? 'partial' : 'ok'
    };
  });

  const maxDrawdown = out.reduce((m, r) => Math.min(m, Number(r.drawdown || 0)), 0);
  res.json({ ok: true, max_drawdown: maxDrawdown, rows: out });
});

app.get('/api/policy/ips', (req, res) => {
  const row = db.prepare('SELECT json FROM rules WHERE active = 1 ORDER BY id DESC LIMIT 1').get();
  const value = row?.json ? JSON.parse(row.json) : { alert_mode: 'A2', screenshot_ingest: 'B1', trade_execution: 'shareholder_confirm_required' };
  res.json({ ok: true, value });
});

app.get('/api/risk/latest', (req, res) => {
  // Placeholder v1.
  res.json({ ok: true, events: [] });
});

app.get('/api/watchlist', (req, res) => {
  // Placeholder v1.
  res.json({ ok: true, rows: [] });
});

app.get('/api/cashflow/series', (req, res) => {
  const currency = (req.query.currency || 'CNY').toUpperCase();
  const limit = Number(req.query.limit || 90);
  // pams schema has cashflows table. We'll aggregate if data exists.
  const rows = db.prepare(
    `SELECT date,
            SUM(CASE WHEN direction='in' THEN amount ELSE 0 END) as income,
            SUM(CASE WHEN direction='out' THEN amount ELSE 0 END) as expense
     FROM cashflows
     WHERE currency = ?
     GROUP BY date
     ORDER BY date ASC
     LIMIT ?`
  ).all(currency, limit).map(r=>({
    date: r.date,
    income: Number(r.income||0),
    expense: Number(r.expense||0),
    net: Number(r.income||0) - Number(r.expense||0)
  }));

  res.json({ ok: true, currency, rows });
});

function mapTypeFromNotes(note) {
  const big = String(note?.big || note?.raw?.['大类'] || '');
  if (big === '现金') return 'cash';
  if (big === '理财') return 'wealth';
  if (big === '贵金属') return 'gold';
  if (big === 'A股') return 'stock_cn';
  if (big === '美股') return 'stock_us';
  if (big === '加密') return 'crypto';
  if (big === '分红') return 'esop';
  if (big === '公积金') return 'cash';
  if (big === '借款') return 'cash';
  return 'fund';
}

app.get('/api/holdings/latest', (req, res) => {
  const asOfDate = req.query.asOfDate || dayjs().format('YYYY-MM-DD');
  const rows = db.prepare(
    `SELECT p.*, a.kind as accountKind, a.name as accountName, a.currency as accountCurrency
     FROM positions p JOIN accounts a ON a.id = p.accountId
     WHERE p.asOfDate = ?`
  ).all(asOfDate);

  const out = [];
  for (const p of rows) {
    if (p.accountKind === 'liability') continue;
    // exclude pure cash from 'holdings' table? keep cash so user can filter.
    let note = {};
    try { note = p.notes ? JSON.parse(p.notes) : {}; } catch(e) {}
    const type = mapTypeFromNotes(note);
    out.push({
      venue: note?.account || p.accountName || '',
      account: note?.account || p.accountName || '',
      type,
      symbol: p.symbol,
      name: p.symbol,
      currency: p.accountCurrency,
      market_value: Number(p.marketValue || 0),
      holding_pnl: p.pnlUnrealized == null ? null : Number(p.pnlUnrealized),
      daily_pnl: 0,
    });
  }

  res.json({ ok: true, as_of: asOfDate, rows: out });
});

app.get('/api/summary/latest', async (req, res) => {
  const asOfDate = req.query.asOfDate || dayjs().format('YYYY-MM-DD');
  const baseCurrency = (req.query.baseCurrency || 'CNY').toUpperCase();
  const positionsRaw = db.prepare(
    `SELECT p.*, a.kind as accountKind, a.name as accountName, a.currency as accountCurrency
     FROM positions p JOIN accounts a ON a.id = p.accountId
     WHERE p.asOfDate = ?`
  ).all(asOfDate);

  // Convert everything into baseCurrency using fx_rates; auto-fetch if missing.
  const buckets = { cash:0,fixed_income:0,equity:0,gold_commodities:0,crypto:0,esop:0,receivable:0,liability:0,other:0 };

  for (const p of positionsRaw) {
    const from = (p.accountCurrency || baseCurrency).toUpperCase();
    const rate = await getFxRate(asOfDate, from, baseCurrency);
    const mvBase = Number(p.marketValue ?? 0) * rate;
    const b = classifyBucket(p);
    buckets[b] += mvBase;
  }

  const totalAssets = Object.entries(buckets)
    .filter(([k]) => k !== 'liability')
    .reduce((acc, [, v]) => acc + (v > 0 ? v : 0), 0);
  const totalLiabilities = buckets.liability;
  const netWorth = totalAssets + totalLiabilities;

  // Convert buckets into dashboard-friendly by_type rows.
  const b = buckets;
  const by_type = [
    { type: 'cash', market_value: Number(b.cash||0), holding_pnl: 0, daily_pnl: 0 },
    { type: 'wealth', market_value: Number(b.fixed_income||0), holding_pnl: 0, daily_pnl: 0 },
    { type: 'fund', market_value: Number(b.equity||0), holding_pnl: 0, daily_pnl: 0 },
    { type: 'gold', market_value: Number(b.gold_commodities||0), holding_pnl: 0, daily_pnl: 0 },
    { type: 'crypto', market_value: Number(b.crypto||0), holding_pnl: 0, daily_pnl: 0 },
  ].filter(x=>x.market_value>0);

  res.json({
    ok: true,
    as_of: asOfDate,
    totals: { market_value: Number(netWorth||0), holding_pnl: 0, daily_pnl: 0 },
    by_type
  });
});

app.post('/api/fx/refresh', async (req, res) => {
  const asOfDate = (req.body?.asOfDate || dayjs().format('YYYY-MM-DD'));
  const baseCurrency = (req.body?.baseCurrency || 'CNY').toUpperCase();
  const pairs = req.body?.pairs || ['USD', 'MXN', 'JPY', 'EUR', 'HKD'];
  const out = {};
  for (const from of pairs) {
    out[`${from}/${baseCurrency}`] = await getFxRate(asOfDate, from, baseCurrency);
  }
  res.json({ ok: true, asOfDate, baseCurrency, rates: out });
});

// Minimal ingestion endpoint (for manual import)
app.post('/api/ingest/positions', (req, res) => {
  const { asOfDate, accountId, rows } = req.body || {};
  if (!asOfDate || !accountId || !Array.isArray(rows)) {
    return res.status(400).json({ ok: false, error: 'asOfDate, accountId, rows required' });
  }

  const insert = db.prepare(
    `INSERT INTO positions (asOfDate, accountId, symbol, symbolType, quantity, costBasis, costCurrency, marketPrice, marketValue, pnlUnrealized, notes)
     VALUES (@asOfDate, @accountId, @symbol, @symbolType, @quantity, @costBasis, @costCurrency, @marketPrice, @marketValue, @pnlUnrealized, @notes)`
  );

  const tx = db.transaction(() => {
    db.prepare('DELETE FROM positions WHERE asOfDate = ? AND accountId = ?').run(asOfDate, accountId);
    for (const r of rows) {
      insert.run({
        asOfDate,
        accountId,
        symbol: r.symbol,
        symbolType: r.symbolType,
        quantity: r.quantity,
        costBasis: r.costBasis ?? null,
        costCurrency: r.costCurrency ?? null,
        marketPrice: r.marketPrice ?? null,
        marketValue: r.marketValue ?? null,
        pnlUnrealized: r.pnlUnrealized ?? null,
        notes: r.notes ?? null,
      });
    }
  });
  tx();

  res.json({ ok: true, inserted: rows.length });
});

// Seed accounts + rules (idempotent)
app.post('/api/admin/seed', (req, res) => {
  const accounts = [
    { id: 'binance', kind: 'binance', name: 'Binance', currency: 'USD' },
    { id: 'ibkr', kind: 'ibkr', name: 'IBKR', currency: 'USD' },
    { id: 'a_cn_broker', kind: 'a_cn_broker', name: 'A股券商(代持)', currency: 'CNY' },
    { id: 'ccb', kind: 'ccb', name: '建行', currency: 'CNY' },
    { id: 'licaitong', kind: 'licaitong', name: '腾讯理财通', currency: 'CNY' },
    { id: 'santander', kind: 'cash', name: 'Santander', currency: 'MXN' },
    { id: 'huawei_esop', kind: 'huawei_esop', name: '华为员工持股(成本)', currency: 'CNY' },
    { id: 'receivable', kind: 'receivable', name: '应收', currency: 'CNY' },
    { id: 'liability', kind: 'liability', name: '负债', currency: 'CNY' },
  ];

  const ins = db.prepare(
    `INSERT INTO accounts (id, kind, name, currency, notes)
     VALUES (@id, @kind, @name, @currency, @notes)
     ON CONFLICT(id) DO UPDATE SET kind=excluded.kind, name=excluded.name, currency=excluded.currency`
  );

  const rule = {
    name: 'default',
    json: JSON.stringify({
      language: 'zh',
      noLeverage: true,
      allocation: { us: 0.45, cn: 0.50, crypto: 0.05 },
      maxPosition: { us: 0.30, cn: 0.50, crypto: 0.30 },
      stopRule: { lossPct: 0.30, action: 'reduce_to', targetQtyPct: 0.70, basis: 'cost' },
      closePriceOnly: true,
      dailyChecklistTime: 'UTC-6 09:00'
    })
  };

  const tx = db.transaction(() => {
    for (const a of accounts) ins.run({ ...a, notes: a.notes ?? null });
    // keep only one active default rule
    db.prepare('UPDATE rules SET active = 0 WHERE name = ?').run('default');
    db.prepare('INSERT INTO rules (name, json, active) VALUES (@name, @json, 1)').run(rule);
  });
  tx();

  res.json({ ok: true, accounts: accounts.length });
});

app.listen(port, () => {
  console.log(`[pams] listening on http://127.0.0.1:${port}`);
});
