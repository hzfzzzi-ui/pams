function esc(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function fmtMoney(val, signed = false) {
  if (val == null || !Number.isFinite(Number(val))) return '--';
  const num = Number(val);
  const abs = Math.abs(num);
  const sign = signed ? (num >= 0 ? '+' : '-') : (num < 0 ? '-' : '');
  if (abs >= 10000) return `${sign}¥${(abs / 10000).toFixed(2)}万`;
  return `${sign}¥${Math.round(abs).toLocaleString('zh-CN')}`;
}

export function mountNetworthDebtsPanel(root, get) {
  const el = root.querySelector?.('#accountNetworthDebtsPanel');
  if (!el || typeof get !== 'function') return;

  get('/api/accounts/networth-and-debts')
    .then((data) => {
      if (!data) return;
      const attr = data.attribution || {};
      const debts = data.debts || {};
      const items = debts.items || [];
      const savings = Number(attr.monthly_savings_cny || 0);
      const totalDebt = Math.max(0, Number(debts.total_debt_cny || 0));
      const palette = ['#fbbf24', '#60a5fa', '#a78bfa', '#34d399'];

      const barHtml = totalDebt > 0 && items.length ? `
        <div style="display:flex;height:6px;border-radius:3px;overflow:hidden;background:rgba(255,255,255,0.06);margin:14px 0 6px;gap:2px" aria-hidden="true">
          ${items.map((row, i) => {
            const pct = Math.max(2, (Number(row.balance_cny || 0) / totalDebt) * 100);
            return `<span style="width:${pct.toFixed(1)}%;background:${palette[i % palette.length]}"></span>`;
          }).join('')}
        </div>
      ` : '';

      el.innerHTML = `
        <section class="assetCard" style="background:transparent;border:0;border-top:1px solid rgba(255,255,255,0.08);border-radius:0;padding:16px 0 8px;margin:0">
          <div style="display:flex;align-items:baseline;justify-content:space-between;margin-bottom:12px">
            <h2 style="margin:0;font-size:15px;font-weight:700;color:#f8fafc">${esc(data.month || '本月')} 收支与负债结构</h2>
            <span style="font-size:11px;color:rgba(148,163,184,0.75)">${esc(data.as_of || '--')}</span>
          </div>
          <div style="display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px;padding-bottom:14px;border-bottom:1px solid rgba(255,255,255,0.07)">
            <div>
              <div style="font-size:11px;color:rgba(148,163,184,0.8)">总负债</div>
              <strong style="display:block;margin-top:2px;font-size:22px;font-weight:700;letter-spacing:-0.02em;color:#f8fafc;font-variant-numeric:tabular-nums">${esc(fmtMoney(totalDebt))}</strong>
              <div style="margin-top:2px;font-size:11px;color:rgba(148,163,184,0.75)">估算月息 ${esc(fmtMoney(debts.total_monthly_interest_cny))}</div>
            </div>
            <div>
              <div style="font-size:11px;color:rgba(148,163,184,0.8)">本月收支结余</div>
              <strong style="display:block;margin-top:2px;font-size:22px;font-weight:700;letter-spacing:-0.02em;color:${savings >= 0 ? '#4ade80' : '#f87171'};font-variant-numeric:tabular-nums">${esc(fmtMoney(savings, true))}</strong>
              <div style="margin-top:2px;font-size:11px;color:rgba(148,163,184,0.75)">入 ${esc(fmtMoney(attr.monthly_income_cny))} · 出 ${esc(fmtMoney(attr.monthly_expense_cny))}</div>
            </div>
          </div>
          ${barHtml}
          ${items.length ? `
            <div>
              ${items.map((row, i) => {
                const share = totalDebt > 0 ? ((Number(row.balance_cny || 0) / totalDebt) * 100).toFixed(1) : '0.0';
                const color = palette[i % palette.length];
                return `
                  <div style="display:flex;align-items:center;justify-content:space-between;gap:10px;padding:11px 0;border-bottom:1px solid rgba(255,255,255,0.06)">
                    <div style="display:flex;align-items:flex-start;gap:8px;min-width:0">
                      <span style="width:7px;height:7px;border-radius:50%;background:${color};margin-top:5px;flex-shrink:0"></span>
                      <div style="min-width:0">
                        <strong style="display:block;font-size:13px;font-weight:600;color:#f8fafc">${esc(row.name)}</strong>
                        <small style="display:block;margin-top:2px;font-size:11px;color:rgba(148,163,184,0.75)">${esc(row.note)}</small>
                      </div>
                    </div>
                    <div style="text-align:right;white-space:nowrap">
                      <b style="display:block;font-size:14px;font-weight:700;color:#f8fafc;font-variant-numeric:tabular-nums">${esc(fmtMoney(row.balance_cny))}</b>
                      <em style="display:block;margin-top:1px;font-style:normal;font-size:11px;color:${row.days_until_due != null && row.days_until_due <= 5 ? '#f87171' : 'rgba(148,163,184,0.78)'}">
                        ${row.days_until_due != null ? `${row.days_until_due}天后还款` : `${share}% · ${row.apr_pct}%`}
                      </em>
                    </div>
                  </div>
                `;
              }).join('')}
            </div>
          ` : ''}
        </section>
      `;
      el.hidden = false;
    })
    .catch(() => {});
}
