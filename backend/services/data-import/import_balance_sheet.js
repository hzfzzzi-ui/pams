const fs = require('fs');
const path = require('path');
const Database = require('better-sqlite3');

function slug(s) {
  return String(s || '')
    .trim()
    .toLowerCase()
    .replace(/\s+/g, '_')
    .replace(/[^a-z0-9_\-\u4e00-\u9fff]/g, '')
    .slice(0, 80);
}

function parseCsv(text) {
  // Minimal CSV parser supporting quotes.
  const lines = text.replace(/^\uFEFF/, '').split(/\r?\n/).filter(l => l.length);
  const rows = [];
  let headers = null;
  for (const line of lines) {
    const out = [];
    let cur = '';
    let inQ = false;
    for (let i = 0; i < line.length; i++) {
      const ch = line[i];
      if (inQ) {
        if (ch === '"') {
          if (line[i + 1] === '"') { cur += '"'; i++; }
          else inQ = false;
        } else cur += ch;
      } else {
        if (ch === ',') { out.push(cur); cur = ''; }
        else if (ch === '"') inQ = true;
        else cur += ch;
      }
    }
    out.push(cur);
    if (!headers) headers = out;
    else {
      const obj = {};
      headers.forEach((h, idx) => { obj[h] = out[idx] ?? ''; });
      rows.push(obj);
    }
  }
  return rows;
}

function toNum(s) {
  if (s === null || s === undefined) return null;
  const t = String(s).replace(/,/g, '').trim();
  if (!t) return null;
  const n = Number(t);
  return Number.isFinite(n) ? n : null;
}

function kindFromAccountName(acct, side) {
  // side: 资产|负债
  if (String(side||'') === '负债') return 'liability';
  const a = String(acct || '');
  if (a.includes('腾讯理财通')) return 'licaitong';
  if (a === '建行') return 'ccb';
  if (a === '微信') return 'cash';
  if (a === 'Santander') return 'santander';
  if (a === 'IBKR') return 'ibkr';
  if (a === '同花顺') return 'a_cn_broker';
  if (a === '币安') return 'binance';
  if (a === '华为账户') return 'huawei_esop';
  if (a.includes('公积金')) return 'cash';
  if (a === '借款') return 'receivable';
  return 'cash';
}

function symbolTypeFromRow(r) {
  const big = String(r['大类'] || '');
  const small = String(r['小类'] || '');
  const side = String(r['资产/负债'] || '');
  const acct = String(r['账户'] || '');

  if (side === '负债') return 'liability';
  if (acct === '华为账户' || big === '分红') return 'esop';
  if (big === '现金' || small.includes('现金/活期')) return 'cash';
  if (big === '借款') return 'cash';
  if (big === '加密') return 'crypto';
  if (big === '贵金属' || small.includes('黄金')) return 'gold';
  if (big === '理财' || small.includes('固收')) return 'fixed_income';
  if (small.includes('股票')) return 'stock';
  if (small.includes('基金') || big.includes('美股') || big.includes('A股')) return 'fund';
  return 'fund';
}

function main() {
  const csvPath = process.argv[2] || path.join(__dirname, '..', 'import', 'balance_sheet_export.csv');
  const asOfDate = process.argv[3] || '2026-02-13';
  const doWrite = process.argv.includes('--write');

  const text = fs.readFileSync(csvPath, 'utf8');
  const rows = parseCsv(text);

  // Summary
  const byCurrency = new Map();
  const bySide = new Map();
  for (const r of rows) {
    const side = r['资产/负债'];
    const ccy = r['币种'];
    const mv = toNum(r['当前市值']) ?? 0;
    const key = `${ccy}|${side}`;
    byCurrency.set(key, (byCurrency.get(key) || 0) + mv);
    bySide.set(side, (bySide.get(side) || 0) + mv);
  }

  console.log(`Rows: ${rows.length}, asOfDate=${asOfDate}`);
  console.log('Totals by currency+side:');
  for (const [k, v] of [...byCurrency.entries()].sort()) console.log(`  ${k}: ${v.toFixed(2)}`);

  if (!doWrite) {
    console.log('\n(DRY RUN) add --write to upsert into sqlite.');
    return;
  }

  const dbPath = path.join(__dirname, '..', 'data', 'pams.sqlite3');
  const db = new Database(dbPath);
  db.pragma('journal_mode = WAL');

  // Ensure schema
  const schemaSql = fs.readFileSync(path.join(__dirname, '..', 'schema.sql'), 'utf8');
  // schema.sql starts with PRAGMA for sqlite; safe to exec.
  db.exec(schemaSql);

  const upsertAccount = db.prepare(`
    INSERT INTO accounts (id, kind, name, currency, notes)
    VALUES (@id, @kind, @name, @currency, @notes)
    ON CONFLICT(id) DO UPDATE SET
      kind=excluded.kind,
      name=excluded.name,
      currency=excluded.currency,
      notes=excluded.notes
  `);

  const insertPos = db.prepare(`
    INSERT INTO positions (asOfDate, accountId, symbol, symbolType, quantity, costBasis, costCurrency, marketPrice, marketValue, pnlUnrealized, notes)
    VALUES (@asOfDate, @accountId, @symbol, @symbolType, @quantity, @costBasis, @costCurrency, @marketPrice, @marketValue, @pnlUnrealized, @notes)
  `);

  const deleteExisting = db.prepare(`DELETE FROM positions WHERE asOfDate=@asOfDate AND accountId=@accountId AND symbol=@symbol`);

  const tx = db.transaction(() => {
    // Replace snapshot for the day (idempotent): wipe existing positions for this asOfDate.
    db.prepare('DELETE FROM positions WHERE asOfDate = ?').run(asOfDate);
    for (const r of rows) {
      const acctName = r['账户'];
      const ccy = r['币种'];
      const side = r['资产/负债'];
      const symbol = r['标的名称'];

      // IMPORTANT: liabilities must not share the same accountId as their underlying bank assets.
      // Use the liability instrument name as its own account.
      let acctId;
      let acctDisplayName;
      if (String(side) === '负债') {
        acctDisplayName = symbol;
        acctId = `liability_${slug(symbol)}_${ccy}`;
      } else {
        acctDisplayName = acctName;
        acctId = `${slug(acctName)}_${ccy}`;
      }

      const kind = kindFromAccountName(acctDisplayName, side);
      upsertAccount.run({ id: acctId, kind, name: acctDisplayName, currency: ccy, notes: null });

      const mv = toNum(r['当前市值']);
      const cost = toNum(r['成本']);
      const pnl = (mv != null && cost != null) ? (mv - cost) : null;
      const notesObj = {
        side: r['资产/负债'],
        account: acctName,
        big: r['大类'],
        small: r['小类'],
        raw: r,
      };

      deleteExisting.run({ asOfDate, accountId: acctId, symbol });
      insertPos.run({
        asOfDate,
        accountId: acctId,
        symbol,
        symbolType: symbolTypeFromRow(r),
        quantity: 1,
        costBasis: cost,
        costCurrency: ccy,
        marketPrice: null,
        marketValue: mv,
        pnlUnrealized: pnl,
        notes: JSON.stringify(notesObj),
      });
    }
  });

  tx();
  console.log(`Wrote ${rows.length} position rows into ${dbPath}`);
}

main();
