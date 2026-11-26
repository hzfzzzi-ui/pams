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
      el.innerHTML = `
        <div class="card" style="margin-bottom:12px">
          <div class="sectionTitle">
            <h3>${esc(data.month || '本月')} 净资产归因与负债提醒</h3>
            <span class="miniBadge data">${esc(data.as_of || '--')}</span>
          </div>
          <div style="display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin-top:8px">
            <div style="background:rgba(15,23,42,0.58);border:1px solid rgba(148,163,184,0.14);border-radius:10px;padding:9px">
              <div style="font-size:11px;color:rgba(148,163,184,0.88)">本月收支结余</div>
              <strong style="font-size:15px;color:${savings >= 0 ? '#4ade80' : '#f87171'}">${esc(fmtMoney(savings, true))}</strong>
              <div style="font-size:10px;color:rgba(148,163,184,0.75)">入 ${esc(fmtMoney(attr.monthly_income_cny))} / 出 ${esc(fmtMoney(attr.monthly_expense_cny))}</div>
            </div>
            <div style="background:rgba(15,23,42,0.58);border:1px solid rgba(148,163,184,0.14);border-radius:10px;padding:9px">
              <div style="font-size:11px;color:rgba(148,163,184,0.88)">投资账户净值</div>
              <strong style="font-size:15px;color:#f8fafc">${esc(fmtMoney(attr.investment_net_value_cny))}</strong>
              <div style="font-size:10px;color:rgba(148,163,184,0.75)">同源估值投影</div>
            </div>
            <div style="background:rgba(15,23,42,0.58);border:1px solid rgba(148,163,184,0.14);border-radius:10px;padding:9px">
              <div style="font-size:11px;color:rgba(148,163,184,0.88)">总负债 / 月息(估)</div>
              <strong style="font-size:15px;color:#fbbf24">${esc(fmtMoney(debts.total_debt_cny))}</strong>
              <div style="font-size:10px;color:rgba(148,163,184,0.75)">约 ${esc(fmtMoney(debts.total_monthly_interest_cny))}/月</div>
            </div>
          </div>
          ${items.length ? `
            <div style="display:flex;flex-direction:column;gap:6px;margin-top:10px">
              ${items.map((row) => `
                <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;padding:8px 10px;border-radius:10px;background:rgba(15,23,42,0.5);border-left:3px solid ${row.apr_pct > 0 ? '#fbbf24' : '#38bdf8'}">
                  <div style="min-width:0">
                    <strong style="display:block;font-size:13px;color:#f8fafc">${esc(row.name)}</strong>
                    <small style="display:block;font-size:11px;color:rgba(148,163,184,0.85)">${esc(row.note)}</small>
                  </div>
                  <div style="text-align:right;white-space:nowrap">
                    <b style="display:block;font-size:13px;color:#f8fafc">${esc(fmtMoney(row.balance_cny))}</b>
                    <em style="display:block;font-style:normal;font-size:11px;color:${row.days_until_due != null && row.days_until_due <= 5 ? '#f87171' : 'rgba(148,163,184,0.88)'}">
                      ${row.days_until_due != null ? `${row.days_until_due}天后还款 (${esc(row.due_date.slice(5))})` : `年化 ${row.apr_pct}%`}
                    </em>
                  </div>
                </div>
              `).join('')}
            </div>
          ` : ''}
        </div>
      `;
      el.hidden = false;
    })
    .catch(() => {});
}
