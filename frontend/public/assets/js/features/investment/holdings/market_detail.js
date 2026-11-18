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
    <div class="hmBar"><strong>市值与大盘</strong><div><button data-days="30">近1月</button><button data-days="90" aria-pressed="true">近3月</button></div></div>
    <div class="hmLegend"><span>持仓市值 · 万元</span><span>沪深300 · 点</span></div>
    <div class="hmPlot" aria-label="持仓市值与沪深300历史走势"></div>
    <div class="hmReadout" aria-live="polite">正在读取走势…</div>
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
  return `<svg viewBox="0 0 365 175" role="img" aria-label="左轴个人持仓市值，右轴沪深300点位">${shapes.join('')}</svg>`;
}

export function bindMarketDetail(sheet, holding) {
  if (!sheet) return;
  const accounts = holding.__accountRows || [holding];
  const end = accounts.map(row=>String(row.as_of || row.snapshot_as_of || '').slice(0,10)).filter(day=>/^\d{4}-\d\d-\d\d$/.test(day)).sort().pop();
  const readout = sheet.querySelector('.hmReadout'), plot = sheet.querySelector('.hmPlot'), list = sheet.querySelector('.hmTrades');
  if (!end || accounts.some(row=>!row.asset_id || !(row.account_code || row.account))) {
    readout.textContent='缺少持仓历史标识'; list.textContent='暂无可核对成交'; return;
  }
  const params = new URLSearchParams({start:before(end,89),end});
  const indexParams = params.toString();
  for (const row of accounts) {params.append('asset',row.asset_id);params.append('account',row.account_code || row.account);}
  let period=90, selected=end, history=null, index=null, historyError=false, indexError=false;
  const tradeLabel=t=>(t.side==='buy'?'买入':'卖出')+(t.quantity==null?'':' '+Number(t.quantity).toLocaleString('zh-CN',{maximumFractionDigits:6}))+
    (t.price==null?'':' · 成交价 '+Number(t.price).toLocaleString('zh-CN',{maximumFractionDigits:4})+' '+t.currency);
  function render() {
    if (!sheet.isConnected) return;
    const first=before(end,period-1);
    const points=new Map(), days=new Set();
    function put(rows,key){for(const row of rows||[]){if(row.date<first)continue;days.add(row.date);const p=points.get(row.date)||{};
      p[key]=row[key]==null?null:Number(row[key]);points.set(row.date,p);}}
    put(history?.rows,'value');put(index?.rows,'close');
    const trades=(history?.trades||[]).filter(t=>t.date>=first);
    const sides=new Map();
    for(const trade of trades){days.add(trade.date);if(!sides.has(trade.date))sides.set(trade.date,new Set());sides.get(trade.date).add(trade.side);}
    const dates=[...days].sort(),rows=dates.map(d=>points.get(d)||{});
    list.replaceChildren();
    for(const t of [...trades].reverse()){
      const button=list.ownerDocument.createElement('button');button.textContent=t.date+'　'+tradeLabel(t);
      button.onclick=()=>{selected=t.date;render()};list.append(button);
    }
    if(!trades.length)list.textContent=historyError?'成交记录读取失败':history?'本区间暂无已核对成交':'正在读取…';
    const buttons=sheet.querySelectorAll('[data-days]');
    for(const button of buttons){button.setAttribute('aria-pressed',String(+button.dataset.days===period));button.onclick=()=>{period=+button.dataset.days;render()};}
    if(!dates.length){readout.textContent=historyError?'持仓历史读取失败':'暂无区间数据';return;}
    const active=dates.includes(selected)?dates.indexOf(selected):dates.length-1;
    const day=dates[active], current=rows[active];plot.innerHTML=chartMarkup(rows,dates,active,sides);
    const tradeInfo=trades.filter(t=>t.date===day).map(tradeLabel).join(' / ');
    const indexText=current.close!=null?number(current.close)+' 点':indexError?'读取失败':index?'暂无点位':'读取中';
    readout.textContent=day+'　持仓 '+(historyError?'读取失败':amount(current.value))+'　沪深300 '+indexText+(tradeInfo?'　'+tradeInfo:'');
    function pick(event){const bounds=plot.getBoundingClientRect();const px=(event.clientX-bounds.left)/bounds.width*365;
      const i=Math.max(0,Math.min(dates.length-1,Math.round((px-41)/269*(dates.length-1))));
      if(selected!==dates[i]){selected=dates[i];render();}}
    plot.onpointerdown=e=>{plot.setPointerCapture(e.pointerId);pick(e)};
    plot.onpointermove=e=>{if(e.buttons)pick(e)};
    plot.onkeydown=e=>{if(!['ArrowLeft','ArrowRight'].includes(e.key))return;e.preventDefault();selected=dates[Math.max(0,Math.min(dates.length-1,active+(e.key==='ArrowLeft'?-1:1)))];render();};plot.tabIndex=0;
  }
  jget('/api/holdings/market-history?'+params).then(data=>{history=data;render()}).catch(()=>{historyError=true;render()});
  jget('/api/prices/holding-benchmark?'+indexParams,{ttlMs:60000}).then(data=>{index=data;render()}).catch(()=>{indexError=true;render()});
}
