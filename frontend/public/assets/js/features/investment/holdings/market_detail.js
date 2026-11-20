import { jget } from '../../../api.js';
import { esc } from './list_legacy_model.js';

const number = (value, digits = 2) => Number(value).toLocaleString('zh-CN', {
  minimumFractionDigits: digits, maximumFractionDigits: digits,
});
const amount = value => value == null ? '—' : '¥' + number(value);
const before = (end, days) => new Date(Date.parse(end) - days * 86400000).toISOString().slice(0, 10);

export function marketDetailHtml({subtitle, value, totalPnl, pnlClass, breakdown, fields}) {
  return `<section class="holdingMarketDetail">
    <div class="hmSubtitle">${esc(subtitle)}</div>
    <div class="hmHero"><div><small>个人持仓市值</small><strong>${esc(value)}</strong></div>
      <div><small>累计盈亏</small><b class="${pnlClass}">${esc(totalPnl)}</b></div></div>
    <div class="hmBar"><strong>走势</strong><div><button data-period="ytd" aria-pressed="true">年初至今</button><button data-period="month">近1月</button><button data-period="quarter">近3月</button></div></div>
    <div class="hmPanel"><strong>持仓市值走势</strong><div class="hmLegend"><span>持仓市值 · 万元</span></div><div class="hmPlot" aria-label="持仓市值历史走势"></div><div class="hmReadout" aria-live="polite">正在读取走势…</div></div>
    <div class="hmPanel"><strong>我的持仓收益率</strong><div class="hmReturnPlot"></div><div class="hmReturnReadout" aria-live="polite">正在读取走势…</div></div>
    <div class="hmPanel"><strong>标的与大盘对比</strong><div class="hmLegend hmComparisonLegend" hidden><span>标的</span><span class="hmBenchmarkName"></span></div><div class="hmComparisonPlot"></div><div class="hmComparisonReadout" aria-live="polite">正在读取走势…</div></div>
    <div class="hmBreakdown">${breakdown.map(item=>`<div><small>${esc(item.label)}</small><b class="${item.tone}">${esc(item.amount)}</b></div>`).join('')}</div>
    <details><summary>账户成交记录</summary><div class="hmTrades"></div></details>
    <details><summary>持仓与盈亏明细</summary>${fields}</details>
  </section>`;
}

function chartMarkup(rows, dates, active, trades) {
  const left = 41, right = 310, top = 17, bottom = 143;
  const x = i => left + i * (right-left) / Math.max(dates.length-1, 1);
  function scale(key) {
    const values = rows.map(row => row[key]).filter(n => n != null);
    if (!values.length) return null;
    const low = Math.min(...values), high = Math.max(...values);
    const pad = Math.max((high-low)*0.14, Math.abs(high)*0.002, 0.01);
    const min = low-pad, max = high+pad;
    return {min, max, y: n => bottom-(n-min)*(bottom-top)/(max-min)};
  }
  const mv = scale('value'), ix = scale('close');
  const shapes = [];
  for (const fraction of [0,0.5,1]) {
    const y = bottom-fraction*(bottom-top);
    shapes.push(`<line x1="${left}" x2="${right}" y1="${y}" y2="${y}" class="hmGrid"/>`);
    if (mv) shapes.push(`<text x="36" y="${y+4}" text-anchor="end">${number((mv.min+(mv.max-mv.min)*fraction)/10000,2)}</text>`);
    if (ix) shapes.push(`<text x="315" y="${y+4}">${number(ix.min+(ix.max-ix.min)*fraction,0)}</text>`);
  }
  for (const [key,s,cls] of [['close',ix,'hmIndex'],['value',mv,'hmValue']]) {
    if (!s) continue;
    let path = '', gap = true;
    rows.forEach((row,i) => {
      if (row[key] == null) {
        // Index closes have no weekend observation. Keep the readout empty there.
        if (key === 'value') gap = true;
        return;
      }
      path += (gap ? 'M' : 'L') + x(i)+' '+s.y(row[key])+' ';
      gap = false;
    });
    shapes.push(`<path d="${path}" class="${cls}" fill="none"/>`);
  }
  shapes.push(`<line x1="${x(active)}" x2="${x(active)}" y1="${top}" y2="${bottom}" class="hmCursor"/>`);
  for (const [date, sides] of trades) {
    const i = dates.indexOf(date);
    if (i<0 || rows[i].value == null || !mv) continue;
    const xx=x(i), yy=mv.y(rows[i].value), cy=yy<32?yy+14:yy-14;
    const label=sides.size>1?'双':sides.has('buy')?'买':'卖';
    shapes.push(`<g><line x1="${xx}" x2="${xx}" y1="${yy}" y2="${cy}" class="hmCursor"/>
      <circle cx="${xx}" cy="${cy}" r="9" fill="${label==='买'?'#c8e2ff':'#e2cbed'}"/>
      <text x="${xx}" y="${cy+4}" text-anchor="middle" class="hmTag">${label}</text></g>`);
  }
  for (const i of [0,Math.floor(dates.length/2),dates.length-1]) {
    shapes.push(`<text x="${x(i)}" y="166" text-anchor="${i===0?'start':i===dates.length-1?'end':'middle'}">${esc(dates[i].slice(5))}</text>`);
  }
  return `<svg viewBox="0 0 365 175" role="img" aria-label="个人持仓市值">${shapes.join('')}</svg>`;
}


function comparisonMarkup(rows, dates) {
  const values = rows.flatMap(row => [row.value, row.close]);
  const low = Math.min(1, ...values), high = Math.max(1, ...values);
  const pad = Math.max((high - low) * 0.12, 0.005);
  const min = low - pad, max = high + pad;
  const startTime = Date.parse(dates[0]), endTime = Date.parse(dates[dates.length - 1]);
  const x = i => 41 + (Date.parse(dates[i]) - startTime) * 269 / Math.max(endTime - startTime, 1);
  const y = value => 143 - (value - min) * 126 / (max - min);
  const shapes = [];
  for (const fraction of [0, 0.5, 1]) {
    const value = min + (max - min) * fraction, yy = y(value);
    shapes.push(`<line x1="41" x2="310" y1="${yy}" y2="${yy}" class="hmGrid"/><text x="36" y="${yy+4}" text-anchor="end">${number(value,2)}</text>`);
  }
  for (const [key, cls] of [['value', 'hmValue'], ['close', 'hmIndex']]) {
    const path = rows.map((row, i) => (i ? 'L' : 'M') + x(i) + ' ' + y(row[key])).join(' ');
    shapes.push(`<path d="${path}" class="${cls}" fill="none"/>`);
  }
  for (const i of [0, Math.floor(dates.length / 2), dates.length - 1]) {
    shapes.push(`<text x="${x(i)}" y="166" text-anchor="${i === 0 ? 'start' : i === dates.length - 1 ? 'end' : 'middle'}">${esc(dates[i].slice(5))}</text>`);
  }
  return `<svg viewBox="0 0 365 175" role="img" aria-label="标的与大盘归一化走势，起点为1">${shapes.join('')}</svg>`;
}


function returnMarkup(rows) {
  const values = rows.filter(row => row.return_rate != null).map(row => Number(row.return_rate) * 100);
  if (!values.length) return '';
  const minValue = Math.min(0, ...values), maxValue = Math.max(0, ...values);
  const pad = Math.max((maxValue - minValue) * 0.12, 0.5);
  const low = minValue - pad, high = maxValue + pad;
  const start = Date.parse(rows[0].date), end = Date.parse(rows[rows.length - 1].date);
  const x = row => 41 + (Date.parse(row.date) - start) * 269 / Math.max(end - start, 1);
  const y = value => 143 - (value - low) * 126 / (high - low);
  const shapes = [];
  for (const fraction of [0, 0.5, 1]) {
    const value = low + (high - low) * fraction, yy = y(value);
    shapes.push(`<line x1="41" x2="310" y1="${yy}" y2="${yy}" class="hmGrid"/><text x="36" y="${yy + 4}" text-anchor="end">${number(value, 1)}%</text>`);
  }
  let path = '', gap = true;
  for (const row of rows) {
    if (row.return_rate == null) { gap = true; continue; }
    path += (gap ? 'M' : 'L') + x(row) + ' ' + y(Number(row.return_rate) * 100) + ' ';
    gap = false;
  }
  shapes.push(`<path d="${path}" class="hmValue" fill="none"/>`);
  for (const i of [0, Math.floor(rows.length / 2), rows.length - 1]) {
    shapes.push(`<text x="${x(rows[i])}" y="166" text-anchor="${i === 0 ? 'start' : i === rows.length - 1 ? 'end' : 'middle'}">${esc(rows[i].date.slice(5))}</text>`);
  }
  return `<svg viewBox="0 0 365 175" role="img" aria-label="我的持仓区间收益率">${shapes.join('')}</svg>`;
}

export function bindMarketDetail(sheet, holding) {
  if (!sheet) return;
  const accounts = holding.__accountRows || [holding];
  const end = accounts.flatMap(row => [row.as_of, row.snapshot_as_of])
    .map(value => String(value || '').slice(0, 10))
    .filter(day => /^\d{4}-\d\d-\d\d$/.test(day)).sort().pop();
  const readout = sheet.querySelector('.hmReadout'), plot = sheet.querySelector('.hmPlot'), list = sheet.querySelector('.hmTrades');
  const identity = row => row.asset_id || (row.symbol && row.currency ? `symbol:${row.currency}:${row.symbol}` : '');
  if (!end || accounts.some(row=>!identity(row) || !(row.account_code || row.account))) {
    readout.textContent='缺少持仓历史标识'; list.textContent='暂无可核对成交';
    sheet.querySelector('.hmReturnReadout').textContent = '缺少持仓历史标识';
    sheet.querySelector('.hmComparisonReadout').textContent = '缺少持仓历史标识';
    return;
  }
  const starts = {ytd: end.slice(0, 4) + '-01-01', month: before(end, 29), quarter: before(end, 89)};
  const windows = new Map();
  let period = 'ytd', selected = end, records = null;
  function requestWindow(start) {
    if (windows.has(start)) return windows.get(start);
    const data = {};
    windows.set(start, data);
    const dates = new URLSearchParams({start, end});
    const params = new URLSearchParams(dates);
    params.set('all_trades', 'true');
    for (const row of accounts) {
      params.append('asset', identity(row));
      params.append('account', row.account_code || row.account);
    }
    jget('/api/holdings/market-history?' + params).then(history => {
      data.history = history;
      records = history.trades.slice().reverse();
    }).catch(() => { data.historyError = true; }).finally(() => { renderTrades(); render(); });
    jget('/api/holdings/position-return?' + params).then(result => { data.returns = result; })
      .catch(() => { data.returnError = true; }).finally(render);
    jget('/api/holdings/price-comparison?' + params, {ttlMs:60000}).then(index => {
      data.index = index;
    }).catch(() => { data.indexError = true; }).finally(render);
    return data;
  }
  // Each period has its own common comparison baseline.
  function activeWindow() {
    const first = starts[period];
    return requestWindow(first);
  }
  const tradeLabel=t=>(t.side==='buy'?'买入':'卖出')+(t.quantity==null?'':' '+Number(t.quantity).toLocaleString('zh-CN',{maximumFractionDigits:6}))+
    (t.price==null?'':' · 成交价 '+Number(t.price).toLocaleString('zh-CN',{maximumFractionDigits:4})+' '+t.currency);
  let expanded = false;
  function renderTrades() {
    if (!sheet.isConnected) return;
    const {history, historyError} = activeWindow();
    list.replaceChildren();
    if (!records?.length) {
      list.textContent = historyError ? '成交记录读取失败' : history ? '暂无已核对成交' : '正在读取…';
      return;
    }
    const first = starts[period];
    for (const t of expanded ? records : records.slice(0, 10)) {
      const inChart = first <= t.date && t.date <= end;
      const row = list.ownerDocument.createElement(inChart ? 'button' : 'div');
      row.className = 'hmTradeRow';
      row.textContent = t.date + '　' + tradeLabel(t);
      if (inChart) row.onclick = () => { selected = t.date; render(); };
      list.append(row);
    }
    if (records.length > 10) {
      const toggle = list.ownerDocument.createElement('button');
      toggle.textContent = expanded ? '收起' : '展开更多（共 ' + records.length + ' 笔）';
      toggle.setAttribute('aria-expanded', String(expanded));
      toggle.onclick = () => { expanded = !expanded; renderTrades(); };
      list.append(toggle);
    }
  }
  function render() {
    if (!sheet.isConnected) return;
    const first = starts[period];
    const {history, index, returns, returnError, historyError, indexError} = activeWindow();
    const returnPlot = sheet.querySelector('.hmReturnPlot');
    const returnReadout = sheet.querySelector('.hmReturnReadout');
    if (returns?.status === 'available') {
      returnPlot.innerHTML = returnMarkup(returns.rows);
      const last = returns.rows[returns.rows.length - 1];
      returnReadout.textContent = last.return_rate == null ? '当日收益率待核对'
        : last.date + '　' + number(Number(last.return_rate) * 100) + '%'
          + (last.return_scope === 'partial_positions' ? '　部分持仓' : '');
    } else {
      returnPlot.replaceChildren();
      returnReadout.textContent = returnError ? '收益率读取失败' : returns ? '暂无可核对收益率' : '正在读取走势…';
    }
    const points=new Map(), days=new Set();
    function put(rows,key){for(const row of rows||[]){if(row.date<first)continue;days.add(row.date);const p=points.get(row.date)||{};
      p[key]=row[key]==null?null:Number(row[key]);points.set(row.date,p);}}
    put(history?.rows,'value');
    const trades=(history?.trades||[]).filter(t=>t.date>=first && t.date<=end);
    const sides=new Map();
    for(const trade of trades){days.add(trade.date);if(!sides.has(trade.date))sides.set(trade.date,new Set());sides.get(trade.date).add(trade.side);}
    const dates=[...days].sort(),rows=dates.map(d=>points.get(d)||{});
    if (!dates.length) {
      plot.replaceChildren();
      readout.textContent = historyError ? '持仓历史读取失败' : history ? '暂无区间数据' : '正在读取走势…';
      return;
    }
    const active=dates.includes(selected)?dates.indexOf(selected):dates.length-1;
    const day=dates[active], current=rows[active];plot.innerHTML=chartMarkup(rows,dates,active,sides);
    const tradeInfo=trades.filter(t=>t.date===day).map(tradeLabel).join(' / ');
    const comparisonPlot = sheet.querySelector('.hmComparisonPlot');
    const comparisonReadout = sheet.querySelector('.hmComparisonReadout');
    sheet.querySelector('.hmComparisonLegend').hidden = index?.status !== 'ok';
    if (index?.status === 'ok') {
      sheet.querySelector('.hmBenchmarkName').textContent = index.benchmark;
      const series = index.rows.map(row => ({value:Number(row.asset_index), close:Number(row.benchmark_index)}));
      const compareDates = index.rows.map(row => row.date);
      comparisonPlot.innerHTML = comparisonMarkup(series, compareDates);
      const last = series[series.length - 1];
      comparisonReadout.textContent = '起点 ' + index.base_date + ' = 1　标的 ' + number(last.value) + '　' + index.benchmark + ' ' + number(last.close);
    } else {
      comparisonPlot.replaceChildren();
      comparisonReadout.textContent = indexError ? '对比数据读取失败' : index ? '暂无可用的市场对比数据' : '正在读取走势…';
    }
    readout.textContent=day+'　持仓 '+(historyError?'读取失败':amount(current.value))+(tradeInfo?'　'+tradeInfo:'');
    function pick(event){const bounds=plot.getBoundingClientRect();const px=(event.clientX-bounds.left)/bounds.width*365;
      const i=Math.max(0,Math.min(dates.length-1,Math.round((px-41)/269*(dates.length-1))));
      if(selected!==dates[i]){selected=dates[i];render();}}
    plot.onpointerdown=e=>{plot.setPointerCapture(e.pointerId);pick(e)};
    plot.onpointermove=e=>{if(e.buttons)pick(e)};
    plot.onkeydown=e=>{if(!['ArrowLeft','ArrowRight'].includes(e.key))return;e.preventDefault();selected=dates[Math.max(0,Math.min(dates.length-1,active+(e.key==='ArrowLeft'?-1:1)))];render();};plot.tabIndex=0;
  }
  const buttons = sheet.querySelectorAll('[data-period]');
  for (const button of buttons) {
    button.onclick = () => {
      period = button.dataset.period;
      for (const item of buttons) item.setAttribute('aria-pressed', String(item === button));
      renderTrades();
      render();
    };
  }
  renderTrades();
  render();
}
