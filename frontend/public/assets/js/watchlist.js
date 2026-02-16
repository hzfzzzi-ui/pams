import { jget, jpost } from './api.js';
import { setActiveNav } from './ui.js';

function fmtNum(x, digits=2){
  if(x==null || x==='') return '—';
  const n = Number(x);
  if(Number.isNaN(n)) return String(x);
  return n.toLocaleString('zh-CN',{minimumFractionDigits:digits,maximumFractionDigits:digits});
}

// ==================== Tag 常量 ====================

const STAGE_TAGS = {
  'TRIGGERED':     '<span class="tag risk">触发</span>',
  'PULLBACK_ZONE': '<span class="tag warn">回调区</span>',
};

const SENT_TAGS = {
  'HOT':     '<span class="tag risk">HOT</span>',
  'NEUTRAL': '<span class="tag data">NEUTRAL</span>',
  'PANIC':   '<span class="tag warn">PANIC</span>',
};

const TREND_TAGS = {
  'RISK_ON':      '<span class="tag data">RISK ON</span>',
  'RISK_NEUTRAL': '<span class="tag warn">NEUTRAL</span>',
  'RISK_OFF':     '<span class="tag risk">RISK OFF</span>',
};

const SEV_TAGS = {
  'CRITICAL': '<span class="tag risk">严重</span>',
  'HIGH':     '<span class="tag warn">高</span>',
  'INFO':     '<span class="tag data">信息</span>',
};

const WL_STATUS_TAGS = {
  'watch':     '<span class="tag data">观察</span>',
  'candidate': '<span class="tag warn">候选</span>',
  'hold':      '<span class="tag" style="background:var(--good);color:#000">持有</span>',
  'avoid':     '<span class="tag risk">回避</span>',
};

const EXIT_REASON_TAGS = {
  'RC_WL_EXIT_TREND_REVERSAL': '<span class="tag risk">趋势反转</span>',
  'RC_WL_EXIT_FUNDAMENTAL':    '<span class="tag warn">基本面恶化</span>',
  'RC_WL_EXIT_EVENT_RISK':     '<span class="tag risk">事件风险</span>',
};

// ==================== 扫描控制 ====================

async function handleWlScan(){
  const btn = document.getElementById('btnWlScan');
  const resultEl = document.getElementById('scanResult');
  if(!btn) return;

  btn.disabled = true;
  btn.textContent = '扫描中...';
  resultEl.style.display = 'block';
  resultEl.innerHTML = '<div class="muted">正在扫描板块，可能需要 1-2 分钟...</div>';

  try {
    const data = await jpost('/api/strategy/watchlist/scan');
    resultEl.innerHTML = `
      <div style="padding:8px;border-radius:var(--radius-md);background:var(--bg-surface);border:1px solid var(--line)">
        <span class="tag data">完成</span>
        扫描 ${data.sectors_scanned||0} 个板块，检查 ${data.symbols_checked||0} 只，
        新入池 <strong style="color:var(--good)">${data.new_entries||0}</strong> 只
        ${(data.details||[]).length > 0
          ? '：' + data.details.map(d => `${d.name}(${d.symbol})`).join('、')
          : ''}
      </div>
    `;
    // 刷新观察池列表
    const wl = await jget('/watchlist?limit=200');
    renderWatchlist(wl);
  } catch(e) {
    resultEl.innerHTML = `<div style="color:var(--bad)">扫描失败: ${e.message||e}</div>`;
  } finally {
    btn.disabled = false;
    btn.textContent = '扫描入池';
  }
}

async function handleExitCheck(){
  const btn = document.getElementById('btnExitCheck');
  const resultEl = document.getElementById('scanResult');
  if(!btn) return;

  btn.disabled = true;
  btn.textContent = '检查中...';
  resultEl.style.display = 'block';
  resultEl.innerHTML = '<div class="muted">正在检查退出条件...</div>';

  try {
    const data = await jpost('/api/strategy/watchlist/exit-check');
    const exitInfo = (data.exit_details||[]).map(d => {
      const tags = (d.reason_codes||[]).map(c => EXIT_REASON_TAGS[c]||c).join(' ');
      return `${d.name}(${d.symbol}) ${tags}`;
    }).join('、');

    resultEl.innerHTML = `
      <div style="padding:8px;border-radius:var(--radius-md);background:var(--bg-surface);border:1px solid var(--line)">
        <span class="tag data">完成</span>
        检查 ${data.checked||0} 只，退出 <strong style="color:${data.exits > 0 ? 'var(--bad)' : 'var(--good)'}">${data.exits||0}</strong> 只
        ${exitInfo ? '：' + exitInfo : ''}
      </div>
    `;
    // 刷新观察池列表 + 退出历史
    const [wl, exitHist] = await Promise.all([
      jget('/watchlist?limit=200'),
      jget('/api/strategy/watchlist/exit-history?limit=20').catch(()=>({rows:[]})),
    ]);
    renderWatchlist(wl);
    renderExitHistory(exitHist);
  } catch(e) {
    resultEl.innerHTML = `<div style="color:var(--bad)">检查失败: ${e.message||e}</div>`;
  } finally {
    btn.disabled = false;
    btn.textContent = '退出检查';
  }
}

// ==================== 退出历史 ====================

function renderExitHistory(data){
  const el = document.getElementById('exitHistory');
  if(!el) return;

  const rows = (data.rows||[]).slice(0, 20);

  if(rows.length === 0){
    el.innerHTML = '<div class="muted">暂无退出记录</div>';
    return;
  }

  el.innerHTML = rows.map(r => {
    const codes = Array.isArray(r.reason_codes)
      ? r.reason_codes
      : (typeof r.reason_codes === 'string' ? JSON.parse(r.reason_codes||'[]') : []);
    const tags = codes.map(c => EXIT_REASON_TAGS[c] || `<span class="tag">${c}</span>`).join(' ');
    return `
      <div class="event-row">
        ${tags}
        <span class="mono" style="min-width:70px">${r.symbol||''}</span>
        <span>${r.name||''}</span>
        <span class="muted" style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${r.reason||''}</span>
        <span class="muted" style="min-width:80px;text-align:right">${r.scan_date||''}</span>
      </div>
    `;
  }).join('');
}

// ==================== 市场快报 ====================

function renderMarketBrief(sentiment, trend){
  const el = document.getElementById('marketBrief');
  if(!el) return;

  const sentState = sentiment?.sent_state || 'NEUTRAL';
  const sentTag = SENT_TAGS[sentState] || SENT_TAGS['NEUTRAL'];
  const adv = sentiment?.advancers;
  const dec = sentiment?.decliners;
  const breadth = adv != null && dec != null ? `涨${adv} / 跌${dec}` : '—';
  const limits = sentiment?.limit_up != null
    ? `涨停${sentiment.limit_up} / 跌停${sentiment.limit_down||0}`
    : '—';
  const bigRed = sentiment?.big_red_7d != null ? `${sentiment.big_red_7d}次` : '—';
  const sentDate = sentiment?.as_of || '—';

  const trendState = trend?.trend_state || 'RISK_NEUTRAL';
  const trendTag = TREND_TAGS[trendState] || TREND_TAGS['RISK_NEUTRAL'];
  const spxClose = trend?.spx_close ? fmtNum(trend.spx_close, 0) : '—';
  const spxMa10 = trend?.spx_ma10 ? fmtNum(trend.spx_ma10, 0) : '—';
  const trendDate = trend?.as_of || '—';

  el.innerHTML = `
    <div class="card">
      <div class="sectionTitle" style="margin-bottom:8px">
        <h3>市场情绪</h3>
        <div class="muted">${sentDate}</div>
      </div>
      <div class="row"><div class="muted">状态</div><div>${sentTag}</div></div>
      <div class="row"><div class="muted">涨跌家数</div><div class="mono" style="font-size:0.85em">${breadth}</div></div>
      <div class="row"><div class="muted">涨跌停</div><div class="mono" style="font-size:0.85em">${limits}</div></div>
      <div class="row"><div class="muted">7日大阴线</div><div class="mono" style="font-size:0.85em">${bigRed}</div></div>
    </div>
    <div class="card">
      <div class="sectionTitle" style="margin-bottom:8px">
        <h3>宏观趋势</h3>
        <div class="muted">${trendDate}</div>
      </div>
      <div class="row"><div class="muted">趋势状态</div><div>${trendTag}</div></div>
      <div class="row"><div class="muted">SPX 收盘</div><div class="mono" style="font-size:0.85em">${spxClose}</div></div>
      <div class="row"><div class="muted">SPX MA10</div><div class="mono" style="font-size:0.85em">${spxMa10}</div></div>
    </div>
  `;
}

// ==================== 策略候选 ====================

function renderCandidates(data, heldSymbols){
  const tbody = document.getElementById('wlCandidateRows');
  const card = document.getElementById('wlCandidates');
  if(!tbody || !card) return;

  const hot = (data.rows||[]).filter(r =>
    (r.stage === 'TRIGGERED' || r.stage === 'PULLBACK_ZONE')
    && !heldSymbols.has(r.symbol)
  ).slice(0, 20);

  if(hot.length === 0){
    tbody.innerHTML = '<tr><td colspan="7" class="muted">暂无活跃候选</td></tr>';
    return;
  }

  tbody.innerHTML = hot.map(r => {
    const score = Number(r.confirm_score||0);
    const scoreColor = score >= 70 ? 'var(--good)' : score >= 55 ? 'var(--warn)' : 'var(--bad)';
    return `
      <tr>
        <td class="mono nowrap">${r.symbol||'—'}</td>
        <td>${r.name||''}</td>
        <td>${STAGE_TAGS[r.stage]||r.stage}</td>
        <td class="mono num">${fmtNum(r.close)}</td>
        <td class="mono num">${r.entry_price ? fmtNum(r.entry_price) : '—'}</td>
        <td class="mono num">${r.stop_price ? fmtNum(r.stop_price) : '—'}</td>
        <td class="mono num" style="color:${scoreColor};font-weight:700">${r.confirm_score ? fmtNum(r.confirm_score, 1) : '—'}</td>
      </tr>
    `;
  }).join('');
}

// ==================== 事件监控 ====================

function renderEvents(data){
  const el = document.getElementById('eventList');
  if(!el) return;

  const rows = (data.rows||[]).slice(0, 20);

  if(rows.length === 0){
    el.innerHTML = '<div class="muted">暂无风险事件</div>';
    return;
  }

  el.innerHTML = rows.map(e => {
    const sev = (e.severity||'INFO').toUpperCase();
    const sevTag = SEV_TAGS[sev] || SEV_TAGS['INFO'];
    return `
      <div class="event-row">
        ${sevTag}
        <span class="mono" style="min-width:70px">${e.symbol||''}</span>
        <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${e.title||e.event_type||''}</span>
        <span class="muted" style="min-width:80px;text-align:right">${e.event_date||''}</span>
      </div>
    `;
  }).join('');
}

// ==================== 入池逻辑说明 ====================

function renderEntryLogic(){
  const el = document.getElementById('entryLogicBody');
  if(!el) return;

  el.innerHTML = `
    <div class="flow-guide">
      <p style="margin-bottom:12px;color:var(--text-secondary)">
        观察池是 PAMS 策略系统的"雷达"——定期从全市场扫描，把值得跟踪的股票放进来，
        等待技术面信号触发后再进入策略候选和订单流程。
      </p>

      <div style="font-weight:700;margin-bottom:8px">入池来源</div>
      <div class="flow-steps">
        <div class="flow-step">
          <div class="flow-num">1</div>
          <div>
            <strong>板块轮动扫描</strong>
            <div class="muted">每周从主力板块（有色、科技、银行、消费、新能源等）中挑选趋势向上、近期有回调的标的。优先选龙头和行业前三。</div>
          </div>
        </div>
        <div class="flow-step">
          <div class="flow-num">2</div>
          <div>
            <strong>基本面初筛</strong>
            <div class="muted">市值 > 50亿、ROE > 8%、毛利率稳定、现金流为正、无重大风险事件（预亏/立案/停牌）。不碰 ST 和流动性差的品种。</div>
          </div>
        </div>
        <div class="flow-step">
          <div class="flow-num">3</div>
          <div>
            <strong>技术面确认</strong>
            <div class="muted">MA20 > MA60（多头排列）、近 40 天最大涨幅 > 12%（趋势有力度）、当前价位在 MA20 附近或回调区间内（不追高）。</div>
          </div>
        </div>
        <div class="flow-step">
          <div class="flow-num">4</div>
          <div>
            <strong>排除已持仓</strong>
            <div class="muted">已经买入的标的不重复进入观察池。策略候选表也会自动过滤掉当前持仓。</div>
          </div>
        </div>
      </div>

      <div style="font-weight:700;margin:16px 0 8px">状态含义</div>
      <div style="display:flex;flex-wrap:wrap;gap:12px;font-size:var(--fs-base)">
        <div>${WL_STATUS_TAGS['watch']} <span class="muted">持续跟踪，等待入场信号</span></div>
        <div>${WL_STATUS_TAGS['candidate']} <span class="muted">已触发技术信号，待确认下单</span></div>
        <div>${WL_STATUS_TAGS['hold']} <span class="muted">已买入持有（联动持仓页）</span></div>
        <div>${WL_STATUS_TAGS['avoid']} <span class="muted">基本面恶化或黑名单，暂不考虑</span></div>
      </div>

      <div style="margin-top:16px;padding:10px;border-radius:var(--radius-md);background:var(--bg-surface);border:1px solid var(--line)">
        <div style="font-weight:700;margin-bottom:6px">观察池 → 策略候选 → 订单</div>
        <div class="muted" style="line-height:1.7">
          观察池中的标的每天收盘后由策略扫描器自动评估 ·
          当技术信号触发（收盘站上 MA20 + 收阳）时进入<strong>策略候选</strong> ·
          通过量价确认 + 风控校验后自动生成<strong>订单草稿</strong> ·
          最终由你手动确认后才会执行买入
        </div>
      </div>
    </div>
  `;

  document.getElementById('entryLogicToggle')?.addEventListener('click', () => {
    const open = el.style.display !== 'none';
    el.style.display = open ? 'none' : 'block';
    const h3 = document.querySelector('#entryLogicToggle h3');
    if(h3) h3.textContent = open ? '入池逻辑说明 ▸' : '入池逻辑说明 ▾';
  });
}

// ==================== 观察池列表 ====================

function renderWatchlist(wl){
  const el = document.getElementById('rows');
  if(!el) return;

  const rows = wl.rows || [];
  const countEl = document.getElementById('wlCount');
  if(countEl) countEl.textContent = rows.length;

  if(rows.length === 0){
    el.innerHTML = '<tr><td colspan="5" class="muted">观察池为空（将由每周筛选器自动填充）</td></tr>';
    return;
  }

  const marketLabels = { 'CN': 'A股', 'US': '美股' };

  el.innerHTML = rows.map(r => {
    const statusTag = WL_STATUS_TAGS[r.status] || `<span class="tag">${r.status}</span>`;
    const market = marketLabels[r.market] || r.market;
    return `
      <tr>
        <td>${market}</td>
        <td class="mono nowrap">${r.symbol}</td>
        <td>${r.name||''}</td>
        <td>${statusTag}</td>
        <td class="muted" style="font-size:var(--fs-sm);line-height:1.5">${r.thesis||''}</td>
      </tr>
    `;
  }).join('');
}

// ==================== 主流程 ====================

async function main(){
  setActiveNav();

  const [wl, sentiment, trendState, candidates, events, holdings, exitHist] = await Promise.all([
    jget('/watchlist?limit=200'),
    jget('/api/strategy/sentiment').catch(()=>({sent_state:'NEUTRAL'})),
    jget('/api/strategy/state').catch(()=>({trend_state:'RISK_NEUTRAL'})),
    jget('/api/strategy/candidates').catch(()=>({rows:[]})),
    jget('/api/strategy/events?limit=20').catch(()=>({rows:[]})),
    jget('/holdings/latest').catch(()=>({rows:[]})),
    jget('/api/strategy/watchlist/exit-history?limit=20').catch(()=>({rows:[]})),
  ]);

  // 构建当前持仓 symbol 集合，排除已持仓标的
  const heldSymbols = new Set(
    (holdings.rows||[]).map(r => r.symbol).filter(Boolean)
  );

  renderEntryLogic();
  renderMarketBrief(sentiment, trendState);
  renderCandidates(candidates, heldSymbols);
  renderEvents(events);
  renderWatchlist(wl);
  renderExitHistory(exitHist);

  // 绑定扫描控制按钮
  document.getElementById('btnWlScan')?.addEventListener('click', handleWlScan);
  document.getElementById('btnExitCheck')?.addEventListener('click', handleExitCheck);
}

main().catch(err=>{
  document.getElementById('err').style.display='block';
  document.getElementById('err').textContent = '加载失败：'+String(err);
});
