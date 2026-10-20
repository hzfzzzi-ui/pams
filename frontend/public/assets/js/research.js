import { jget, jpost, apiBase, fmtMoneyCompact } from './api.js?v=20260606-refresh-v3';
import { openBottomSheet, setActiveNav } from './ui.js?v=20260620-page-boundary-v1';

function fmtNum(x, digits=2){
  if(x==null || x==='') return '—';
  const n = Number(x);
  if(Number.isNaN(n)) return String(x);
  return n.toLocaleString('zh-CN',{minimumFractionDigits:digits,maximumFractionDigits:digits});
}

const TREND_TAGS = {
  'RISK_ON':      '<span class="tag data">RISK ON</span>',
  'RISK_NEUTRAL': '<span class="tag warn">NEUTRAL</span>',
  'RISK_OFF':     '<span class="tag risk">RISK OFF</span>',
};

const SENT_TAGS = {
  'HOT':     '<span class="tag risk">HOT</span>',
  'NEUTRAL': '<span class="tag data">NEUTRAL</span>',
  'PANIC':   '<span class="tag warn">PANIC</span>',
};

// ==================== 候选表格 ====================

const STAGE_TAGS = {
  'TRIGGERED':     '<span class="tag risk" title="收盘站上MA20且收阳，满足买入触发条件">触发</span>',
  'PULLBACK_ZONE': '<span class="tag warn" title="处于MA20附近回调区间，等待收阳确认">回调区</span>',
  'TREND_PASS':    '<span class="tag data" title="MA20>MA60多头排列，趋势向上但尚未回调到位">趋势通过</span>',
  'UNIVERSE_PASS': '<span class="tag" title="进入扫描池但未通过趋势门">待筛选</span>',
  'INVALIDATED':   '<span class="tag" style="opacity:.5" title="曾经触发但已失效（跌破止损或过期）">已失效</span>',
};

const FLOW_TAGS = {
  'PASS': '<span class="tag data" title="量价齐升：回调缩量+触发放量，买盘确认">量价确认</span>',
  'WEAK': '<span class="tag warn" title="量价信号不明显，缩量或放量条件只满足一个">信号偏弱</span>',
  'FAIL': '<span class="tag risk" title="出现放量大阴线或量价背离，买盘不足">量价否决</span>',
};

// ==================== Reason Code 中文映射 ====================

const REASON_LABELS = {
  'RC_U001_ELIGIBLE':                  { label: '进入扫描池', cat: 'pass' },
  'RC_U001_FROM_HOLDINGS':             { label: '来自持仓', cat: 'pass' },
  'RC_U001_FROM_WATCHLIST':            { label: '来自观察池', cat: 'pass' },
  'RC_U001_FROM_BOTH':                 { label: '持仓+观察池', cat: 'pass' },
  'RC_U101_EXCLUDE_ST':                { label: 'ST/退市风险排除', cat: 'fail' },
  'RC_U102_EXCLUDE_SUSP':              { label: '长期停牌排除', cat: 'fail' },
  'RC_U103_LIQUIDITY_FAIL':            { label: '流动性不足排除', cat: 'fail' },
  'RC_U104_COOLDOWN_ACTIVE':           { label: '冷却期未过排除', cat: 'fail' },
  'RC_U201_BLACKLIST_ACTIVE':          { label: '黑名单排除', cat: 'fail' },
  'RC_F001_QUALITY_SCORE_HIGH':        { label: '基本面优质', cat: 'pass' },
  'RC_F101_CASHFLOW_DETERIORATE':      { label: '现金流持续恶化', cat: 'fail' },
  'RC_F102_INDUSTRY_DOWNTREND':        { label: '行业下行趋势', cat: 'fail' },
  'RC_F201_EVENT_PRELOSS':             { label: '预亏/业绩预警', cat: 'fail' },
  'RC_F202_EVENT_INVESTIGATION':       { label: '立案调查/重大处罚', cat: 'fail' },
  'RC_F203_EVENT_ABNORMAL_HALT':       { label: '异常停牌', cat: 'warn' },
  'RC_S001_SENT_NEUTRAL':              { label: '情绪中性', cat: 'info' },
  'RC_S101_SENT_HOT':                  { label: '情绪过热 (减半仓)', cat: 'warn' },
  'RC_S102_SENT_PANIC':                { label: '情绪恐慌 (收紧)', cat: 'fail' },
  'RC_T001_TREND_GATE_PASS':           { label: '趋势门通过 (MA20>MA60)', cat: 'pass' },
  'RC_T101_TREND_GATE_FAIL':           { label: '趋势门失败 (MA20<MA60)', cat: 'fail' },
  'RC_T102_MA_TANGLE_REJECT':          { label: '均线缠绕无趋势', cat: 'fail' },
  'RC_T201_PULLBACK_ZONE_HIT':         { label: '进入回调区间', cat: 'pass' },
  'RC_T202_PULLBACK_TRIGGER_OK':       { label: '触发日确认 (C>MA20 & C>O)', cat: 'pass' },
  'RC_T301_ENTRY_LIMIT_SET':           { label: '入场限价已设定', cat: 'info' },
  'RC_T401_INVALID_LOW_BREAK':         { label: '无效化: 跌破回调低点/MA60', cat: 'fail' },
  'RC_T402_VALIDITY_EXPIRED':          { label: '无效化: 有效期过期', cat: 'fail' },
  'RC_FL001_PULLBACK_VOL_SHRINK':      { label: '回调缩量确认', cat: 'pass' },
  'RC_FL002_TRIGGER_VOL_POP':          { label: '触发放量确认', cat: 'pass' },
  'RC_FL101_HARD_FAIL_BIG_RED':        { label: '放量大阴线否决', cat: 'fail' },
  'RC_FL201_FLOW_PASS':                { label: '量价确认 PASS', cat: 'pass' },
  'RC_FL202_FLOW_WEAK':                { label: '量价确认 WEAK', cat: 'warn' },
  'RC_FL203_FLOW_FAIL':                { label: '量价确认 FAIL', cat: 'fail' },
  'RC_V001_BREAK_MA60_VOL_SPIKE':      { label: '硬否决: 跌破MA60+放量', cat: 'fail' },
  'RC_V002_CLOSE_BELOW_PULLBACK_BAND': { label: '硬否决: 低于回调下沿', cat: 'fail' },
  'RC_V003_MAJOR_NEG_EVENT_TPLUS1':    { label: '硬否决: 重大负面事件T+1', cat: 'fail' },
  'RC_V004_LIQUIDITY_CRASH':           { label: '硬否决: 连续跌停/流动性枯竭', cat: 'fail' },
  'RC_V005_PANIC_AND_INDEX_BREAK':     { label: '硬否决: 恐慌+指数破位', cat: 'fail' },
  'RC_V006_BLACKLIST':                 { label: '硬否决: 黑名单标的', cat: 'fail' },
  'RC_R001_RISK_MODE_DEFENSIVE':       { label: '风险模式: 防御', cat: 'info' },
  'RC_R002_RISK_MODE_ATTACK':          { label: '风险模式: 进攻', cat: 'info' },
  'RC_R101_CONCURRENT_RISK_CAP':       { label: '并发风险超限', cat: 'fail' },
  'RC_R102_THEME_EXPOSURE_CAP':        { label: '行业敞口超限', cat: 'fail' },
  'RC_R103_POSITION_VALUE_CAP':        { label: '单仓市值上限触发', cat: 'warn' },
  'RC_R201_MONTHLY_CB_PAUSE':          { label: '月度熔断: 暂停开仓', cat: 'fail' },
  'RC_R202_MONTHLY_CB_REDUCE_ONLY':    { label: '月度熔断: 仅减仓', cat: 'fail' },
  'RC_E001_ORDER_DRAFT_CREATED':       { label: '行动草案已创建', cat: 'info' },
  'RC_E002_MANUAL_CONFIRM_REQUIRED':   { label: '等待股东确认', cat: 'info' },
  'RC_E003_AUTO_EXECUTED':             { label: '系统记录', cat: 'info' },
  'RC_E101_KILL_SWITCH':               { label: '紧急停止触发', cat: 'fail' },
};

function getReasonLabel(code){
  const entry = REASON_LABELS[code];
  if(entry) return entry;
  return { label: code, cat: 'neutral' };
}

// ==================== 扫描漏斗 ====================

function renderFunnel(scan, candidates){
  const el = document.getElementById('funnelChart');
  if(!el) return;
  if(!scan || !scan.total){
    el.innerHTML = '<div class="muted">暂无扫描数据</div>';
    return;
  }

  const rows = candidates.rows||[];
  const stageCount = { UNIVERSE_PASS: 0, TREND_PASS: 0, PULLBACK_ZONE: 0, TRIGGERED: 0 };
  for(const r of rows) stageCount[r.stage] = (stageCount[r.stage]||0) + 1;

  const total = rows.length;
  const trendCum = stageCount.TREND_PASS + stageCount.PULLBACK_ZONE + stageCount.TRIGGERED;
  const pullbackCum = stageCount.PULLBACK_ZONE + stageCount.TRIGGERED;
  const triggered = stageCount.TRIGGERED;
  const eligible = rows.filter(r => r.eligible === true || r.eligible === 'true').length;

  const steps = [
    { label: 'Universe',  count: total,       color: 'var(--info)' },
    { label: '趋势通过',   count: trendCum,    color: 'var(--info)' },
    { label: '回调区间',   count: pullbackCum,  color: 'var(--warn)' },
    { label: '触发信号',   count: triggered,    color: '#f97316' },
    { label: '行动草案',   count: eligible,     color: 'var(--good)' },
  ];

  const max = Math.max(total, 1);

  el.innerHTML = steps.map(s => {
    const pct = Math.round(s.count / max * 100);
    return `
      <div class="funnel-row">
        <div class="funnel-label">${s.label}</div>
        <div class="funnel-bar-wrap">
          <div class="funnel-bar" style="width:${pct}%;background:${s.color}"></div>
        </div>
        <div class="funnel-count" style="color:${s.color}">${s.count}</div>
      </div>
    `;
  }).join('');
}

// ==================== 决策链渲染 ====================

function renderTrail(reasonCodes){
  if(!reasonCodes || !reasonCodes.length) return '<div class="muted">无决策记录</div>';

  const codes = typeof reasonCodes === 'string' ? JSON.parse(reasonCodes) : reasonCodes;

  return `<div class="trail">${codes.map(code => {
    const info = getReasonLabel(code);
    return `
      <div class="trail-step">
        <div class="trail-dot dot-${info.cat}"></div>
        <div class="trail-line"></div>
        <div>
          <span class="trail-text">${info.label}</span>
          <span class="trail-code">${code}</span>
        </div>
      </div>
    `;
  }).join('')}</div>`;
}

function renderCandidateDetail(r){
  const vetoTag = r.hard_veto_hit === true || r.hard_veto_hit === 'true'
    ? '<span class="tag risk" title="触发硬否决规则（跌破MA60放量、流动性枯竭等），一票否决">否决</span>'
    : '<span class="tag data" title="未触发任何硬否决规则">通过</span>';
  const eligibleTag = r.eligible === true || r.eligible === 'true'
    ? '<span class="tag data" title="通过检查，进入人工复核">可复核</span>'
    : '<span class="tag risk" title="未通过风控或量价检查，继续观察">观察</span>';

  return `
    <div class="detail-grid">
      <div class="detail-section">
        <h4>决策链</h4>
        ${renderTrail(r.reason_codes)}
      </div>
      <div class="detail-section">
        <h4>技术指标</h4>
        <div class="detail-item"><span class="dl">收盘</span><span class="dv">${fmtNum(r.close)}</span></div>
        <div class="detail-item"><span class="dl">MA20</span><span class="dv">${fmtNum(r.ma20)}</span></div>
        <div class="detail-item"><span class="dl">MA60</span><span class="dv">${fmtNum(r.ma60)}</span></div>
        <div class="detail-item"><span class="dl">ATR14</span><span class="dv">${fmtNum(r.atr14)}</span></div>
        <div class="detail-item"><span class="dl">20日高</span><span class="dv">${fmtNum(r.high_20d)}</span></div>
        <div class="detail-item"><span class="dl">20日低</span><span class="dv">${fmtNum(r.low_20d)}</span></div>
        <div class="detail-item"><span class="dl">入场价</span><span class="dv">${r.entry_price?'¥'+fmtNum(r.entry_price):'—'}</span></div>
        <div class="detail-item"><span class="dl">止损价</span><span class="dv">${r.stop_price?'¥'+fmtNum(r.stop_price):'—'}</span></div>
        <div class="detail-item"><span class="dl">有效至</span><span class="dv">${r.valid_until||'—'}</span></div>
      </div>
      <div class="detail-section">
        <h4>风控摘要</h4>
        <div class="detail-item"><span class="dl" title="量价确认：回调是否缩量、触发是否放量">量价</span><span class="dv">${FLOW_TAGS[r.flow_result]||'—'}</span></div>
        <div class="detail-item"><span class="dl" title="综合评分 = 量价×50% + 质量×20% + 情绪×30%">综合评分</span><span class="dv">${r.confirm_score?fmtNum(r.confirm_score,1):'—'}</span></div>
        <div class="detail-item"><span class="dl" title="基本面质量 (ROE/毛利率/负债率/现金流)">质量分</span><span class="dv">${r.quality_score!=null?fmtNum(r.quality_score,1):'—'}</span></div>
        <div class="detail-item"><span class="dl" title="市场情绪 (涨跌家数/涨跌停/社交热度)">情绪</span><span class="dv">${r.sent_state?(SENT_TAGS[r.sent_state]||r.sent_state):'—'} ${r.sent_score!=null?fmtNum(r.sent_score,0):''}</span></div>
        <div class="detail-item"><span class="dl" title="硬否决：跌破MA60放量、流动性枯竭、重大负面事件等一票否决">硬否决</span><span class="dv">${vetoTag}</span></div>
        <div class="detail-item"><span class="dl">结论</span><span class="dv">${eligibleTag}</span></div>
        <div class="detail-item"><span class="dl">仓位</span><span class="dv">${r.position_size||'—'} 股</span></div>
        <div class="detail-item"><span class="dl">风险额</span><span class="dv">${r.risk_amount_cny?'¥'+fmtNum(r.risk_amount_cny,0):'—'}</span></div>
      </div>
    </div>
  `;
}

// ==================== 可展开候选表格 ====================

function candidateTheme(row){
  const text = `${row.name || ''} ${row.symbol || ''}`.toLowerCase();
  if(text.includes('黄金')) return '贵金属';
  if(text.includes('银行')) return '金融';
  if(text.includes('光') || text.includes('半导体') || text.includes('北方华创') || text.includes('海光')) return '科技/半导体';
  return row.market || 'A股';
}

function candidateRisk(row){
  if(row.hard_veto_hit === true || row.hard_veto_hit === 'true') return '<span class="tag risk">硬否决</span>';
  if(row.flow_result === 'FAIL') return '<span class="tag risk">量价否决</span>';
  if(row.stage === 'TRIGGERED') return '<span class="tag warn">需观察</span>';
  return '<span class="tag data">普通</span>';
}

function candidateTrigger(row){
  if(row.stage === 'TRIGGERED') return `触发，入场 ${row.entry_price ? '¥' + fmtNum(row.entry_price) : '待定'}`;
  if(row.stage === 'PULLBACK_ZONE') return '回调区，等待确认';
  if(row.stage === 'TREND_PASS') return '趋势通过，等待回调';
  return '纳入观察，等待趋势';
}

function nextReviewDate(row){
  return row.valid_until || row.scan_date || '下次扫描';
}

function renderCandidateMobileList(rows) {
  const el = document.getElementById('candidateList');
  if (!el) return;
  el.innerHTML = rows.slice(0, 12).map((row, index) => `
    <button class="researchListRow" type="button" data-candidate-index="${index}">
      <span>
        <strong>${row.name || row.symbol || '—'}</strong>
        <small>${candidateTheme(row)} · ${candidateTrigger(row)}</small>
      </span>
      <em>
        ${row.eligible === true || row.eligible === 'true' ? '<b class="good">匹配</b>' : '<b class="warn">观察</b>'}
        ${candidateRisk(row)}
      </em>
    </button>
  `).join('') || '<div class="mobileEmpty">暂无观察标的</div>';

  el.querySelectorAll('[data-candidate-index]').forEach((button) => {
    button.addEventListener('click', () => {
      const row = rows[Number(button.dataset.candidateIndex)];
      if (!row) return;
      openBottomSheet(row.name || row.symbol || '观察详情', renderCandidateDetail(row));
    });
  });
}

function renderCandidateTable(data){
  const rows = data.rows||[];
  const tbody = document.getElementById('candidateRows');
  tbody.innerHTML = '';
  renderCandidateMobileList(rows);

  if(rows.length>0){
    const first = rows[0];
    document.getElementById('scanDate').textContent = first.scan_date||'—';
  }
  document.getElementById('candidateCount').textContent = rows.length;

  for(const r of rows){
    const tr = document.createElement('tr');
    tr.className = 'candidate-row';
    const score = Number(r.confirm_score||0);
    const scoreColor = score>=70?'var(--good)':score>=55?'var(--warn)':'var(--bad)';

    tr.innerHTML = `
      <td class="nowrap mono">${r.symbol||'—'}</td>
      <td class="name">${r.name||''}</td>
      <td>${candidateTheme(r)}</td>
      <td>${STAGE_TAGS[r.stage]||r.stage}</td>
      <td>${candidateTrigger(r)}</td>
      <td>${r.eligible === true || r.eligible === 'true' ? '<span class="tag data">匹配</span>' : '<span class="tag warn">不匹配</span>'}</td>
      <td>${candidateRisk(r)}</td>
      <td class="mono">${nextReviewDate(r)}</td>
    `;

    const detailTr = document.createElement('tr');
    detailTr.className = 'candidate-detail';
    detailTr.innerHTML = `<td colspan="8">${renderCandidateDetail(r)}</td>`;

    tr.addEventListener('click', () => {
      if (window.matchMedia('(max-width: 760px)').matches) {
        openBottomSheet(r.name || r.symbol || '观察详情', renderCandidateDetail(r));
        return;
      }
      const isOpen = detailTr.classList.contains('open');
      tbody.querySelectorAll('.candidate-detail.open').forEach(el => el.classList.remove('open'));
      tbody.querySelectorAll('.candidate-row.expanded').forEach(el => el.classList.remove('expanded'));
      if(!isOpen){
        detailTr.classList.add('open');
        tr.classList.add('expanded');
      }
    });

    tbody.appendChild(tr);
    tbody.appendChild(detailTr);
  }
}

// ==================== 行动草案 ====================

const STATUS_TAGS = {
  'pending':   '<span class="tag warn">待确认</span>',
  'confirmed': '<span class="tag data">已确认</span>',
  'executed':  '<span class="tag data">已记录</span>',
  'expired':   '<span class="tag muted">已过期</span>',
  'cancelled': '<span class="tag muted">已取消</span>',
};

const BORDER_COLORS = {
  'pending':   'var(--warn)',
  'confirmed': 'var(--good)',
  'executed':  'var(--good)',
  'expired':   'var(--border)',
  'cancelled': 'var(--border)',
};

function orderButtons(o){
  const status = o.status||'';
  if(status==='pending') return '<span class="miniBadge pending">待复核</span>';
  if(status==='confirmed') return '<span class="miniBadge data">已复核</span>';
  if(status==='cancelled') return '<span class="miniBadge">已取消</span>';
  return '<span class="miniBadge data">只读</span>';
}

function renderOrderCards(data){
  const rows = data.rows||[];
  const cardEl = document.getElementById('pendingOrdersCard');
  const container = document.getElementById('orderCards');

  if(rows.length===0){
    cardEl.style.display = 'none';
    return;
  }

  cardEl.style.display = 'block';
  document.getElementById('orderCount').textContent = `${rows.length} 笔`;
  container.innerHTML = '';

  for(const o of rows){
    const borderColor = BORDER_COLORS[o.status]||'var(--border)';
    const statusTag = STATUS_TAGS[o.status]||o.status;
    const buttons = orderButtons(o);

    const div = document.createElement('div');
    div.className = 'card';
    div.style.marginBottom = 'var(--sp-2)';
    div.style.borderLeft = `3px solid ${borderColor}`;
    div.innerHTML = `
      <div class="row" style="margin-bottom:8px">
        <div>
          <span class="mono" style="font-weight:700">${o.symbol}</span>
          <span class="muted" style="margin-left:8px">${o.name||o.symbol||''}</span>
        </div>
        <div>
          <span class="tag warn">${o.risk_mode||'defensive'}</span>
          ${statusTag}
        </div>
      </div>
      <div class="grid3" style="gap:var(--sp-2)">
        <div><div class="muted">入场</div><div class="mono">¥${fmtNum(o.entry_price)}</div></div>
        <div><div class="muted">止损</div><div class="mono">¥${fmtNum(o.stop_price)}</div></div>
        <div><div class="muted">数量</div><div class="mono">${o.position_size||0} 股</div></div>
      </div>
      <div class="grid3" style="gap:var(--sp-2);margin-top:6px">
        <div><div class="muted">风险额</div><div class="mono">¥${fmtNum(o.risk_amount_cny,0)}</div></div>
        <div><div class="muted">Flow</div><div>${FLOW_TAGS[o.flow_confirm]||'—'}</div></div>
        <div><div class="muted">有效至</div><div class="mono">${o.valid_until||'—'}</div></div>
      </div>
      ${buttons?`<div style="margin-top:10px;display:flex;gap:8px">${buttons}</div>`:''}
    `;
    container.appendChild(div);
  }

}

// ==================== IPS 实况仪表 ====================

const CB_TAGS = {
  'normal':      '<span class="tag data">正常</span>',
  'pause':       '<span class="tag warn">暂停开仓</span>',
  'reduce_only': '<span class="tag risk">仅减仓</span>',
};

function gaugeColor(pct){
  if(pct < 60) return 'var(--good)';
  if(pct < 85) return 'var(--warn)';
  return 'var(--bad)';
}

function gaugeCard(label, actual, limit, unit, invert){
  const num = Number(actual)||0;
  const cap = Number(limit)||1;
  const pct = Math.min(Math.round(Math.abs(num)/Math.abs(cap)*100), 100);
  const color = gaugeColor(invert ? (100 - pct) : pct);
  const display = unit === '¥'
    ? `¥${fmtNum(num, 0)}`
    : unit === '%'
      ? `${fmtNum(num, 2)}%`
      : String(num);
  const capDisplay = unit === '¥'
    ? `¥${fmtNum(cap, 0)}`
    : unit === '%'
      ? `${fmtNum(cap, 2)}%`
      : String(cap);
  return `
    <div class="card">
      <div class="muted">${label}</div>
      <div class="mono" style="font-size:var(--fs-xl);font-weight:700">${display} <span class="muted" style="font-size:var(--fs-sm)">/ ${capDisplay}</span></div>
      <div class="gauge-bar"><div class="gauge-fill" style="width:${pct}%;background:${color}"></div></div>
    </div>
  `;
}

function renderIpsLive(ips, risk, updatedAt, trend, sentiment){
  const el = document.getElementById('ipsGauges');
  if(!el) return;

  const dateEl = document.getElementById('ipsDate');
  if(dateEl && updatedAt) dateEl.textContent = String(updatedAt).slice(0, 10);

  const portfolio = ips.portfolio || {};
  const limits = portfolio.limits || {};
  const budget = portfolio.risk_budget || {};
  const cb = portfolio.monthly_circuit_breakers || {};

  const maxPos = limits.max_positions_total || 12;
  const concCap = (budget.concurrent_risk_pct_cap?.defensive || 0.02) * 100;
  const cbPause = (cb.pause_new_entries_drawdown_pct || 0.04) * 100;
  const bNavConfig = portfolio.b_nav_override_cny || 0;

  const openPos = risk?.open_positions || 0;
  const concRisk = (Number(risk?.concurrent_risk_pct) || 0) * 100;
  const mdd = Math.abs(Number(risk?.monthly_drawdown_pct) || 0) * 100;
  const bNav = Number(risk?.b_nav) || 0;
  const cbState = risk?.circuit_breaker || 'normal';

  const cbTag = CB_TAGS[cbState] || CB_TAGS['normal'];
  const riskMode = mdd >= (cb.reduce_only_drawdown_pct || 0.07) * 100
    ? '<span class="tag risk">仅减仓</span>'
    : mdd >= cbPause
      ? '<span class="tag warn">暂停开仓</span>'
      : '<span class="tag data">defensive</span>';

  // 宏观趋势
  const trendTag = TREND_TAGS[trend?.trend_state] || TREND_TAGS['RISK_NEUTRAL'];
  const spxClose = trend?.spx_close ? fmtNum(trend.spx_close, 0) : '—';
  const spxMa10 = trend?.spx_ma10 ? fmtNum(trend.spx_ma10, 0) : '—';

  // 市场情绪
  const sentState = sentiment?.sent_state || 'NEUTRAL';
  const sentTag = SENT_TAGS[sentState] || SENT_TAGS['NEUTRAL'];
  const sentAdv = sentiment?.advancers;
  const sentDec = sentiment?.decliners;
  const sentBreadth = sentAdv != null && sentDec != null ? `涨${sentAdv}/跌${sentDec}` : '—';

  const item = (label, value, help = '') => `
    <div class="ipsStatusItem">
      <div class="ipsStatusLabel">${label}</div>
      <div class="ipsStatusValue">${value}</div>
      <div class="muted" style="margin-top:4px">${help}</div>
    </div>
  `;
  el.innerHTML = [
    item('模式', riskMode, `单笔 ${fmtNum((budget.per_trade_risk_pct?.defensive||0.005)*100,2)}%`),
    item('趋势', trendTag, `SPX ${spxClose} / MA10 ${spxMa10}`),
    item('持仓数', `${openPos}/${maxPos}`, '当前策略持仓'),
    item('月回撤', `${fmtNum(mdd, 2)}%`, `暂停 ${fmtNum(cbPause,1)}%`),
    item('并发风险', `${fmtNum(concRisk, 2)}%`, `上限 ${fmtNum(concCap,2)}%`),
    item('B_NAV', fmtMoneyCompact(bNav), `配置 ${fmtMoneyCompact(bNavConfig)}`),
  ].join('');
}

// ==================== 持仓 vs IPS ====================

const CAT_COLORS = {
  'A股': '#f85149', '美股': '#58a6ff', '加密': '#bc8cff',
  '贵金属': '#d29922', '理财': '#3fb950', '现金': '#6e7681',
  '公积金': '#8b949e', '借款': '#f85149',
};

function renderHoldingsAlign(ips, summary, risk){
  const el = document.getElementById('holdingsAlign');
  if(!el) return;

  const cats = summary.by_category || [];
  const total = summary.total || {};
  const totalVal = Number(total.value) || 1;

  if(cats.length === 0){
    el.innerHTML = '<div class="muted">暂无持仓数据</div>';
    return;
  }

  const targetRange = (name) => {
    if(name === '现金') return [20, 30];
    if(name === 'A股') return [10, 35];
    if(name === '美股') return [15, 45];
    if(name === '贵金属') return [5, 15];
    if(name === 'ESOP' || name === '内部股份分红') return [0, 20];
    return [0, 25];
  };
  const rows = cats.map(c => {
    const val = Number(c.total_value_base) || 0;
    const pct = val / totalVal * 100;
    const [low, high] = targetRange(c.major_category);
    const drift = pct < low ? pct - low : pct > high ? pct - high : 0;
    const status = drift === 0 ? '<span class="tag data">正常</span>' : '<span class="tag warn">偏离</span>';
    return `
      <tr>
        <td>${c.major_category}</td>
        <td class="num mono">${fmtMoneyCompact(val)} · ${fmtNum(pct, 1)}%</td>
        <td class="num mono">${fmtNum(low, 0)}%-${fmtNum(high, 0)}%</td>
        <td class="num mono" style="color:${drift === 0 ? 'var(--good)' : 'var(--warn)'}">${drift === 0 ? '0.0%' : `${drift > 0 ? '+' : ''}${fmtNum(drift, 1)}%`}</td>
        <td>${status}</td>
      </tr>
    `;
  });

  const totalPnl = Number(total.pnl) || 0;
  const pnlColor = totalPnl >= 0 ? 'var(--good)' : 'var(--bad)';
  const totalCount = cats.reduce((s, c) => s + (c.holdings_count || 0), 0);

  rows.push(`
    <tr>
      <td><strong>总计</strong></td>
      <td class="num mono"><strong>${fmtMoneyCompact(totalVal)}</strong> · ${totalCount}仓</td>
      <td class="num mono">—</td>
      <td class="num mono" style="color:${pnlColor}">${totalPnl >= 0 ? '+' : ''}${fmtMoneyCompact(Math.abs(totalPnl))}</td>
      <td><span class="tag data">汇总</span></td>
    </tr>
  `);

  el.innerHTML = `
    <div class="tableWrap">
      <table class="alignTable">
        <thead>
          <tr>
            <th>资产</th>
            <th class="num">当前</th>
            <th class="num">目标区间</th>
            <th class="num">偏离</th>
            <th>状态</th>
          </tr>
        </thead>
        <tbody>${rows.join('')}</tbody>
      </table>
    </div>
  `;
}

// ==================== 策略参数 (可折叠) ====================

function renderIpsParams(ips){
  const el = document.getElementById('paramsBody');
  if(!el) return;

  const sat = ips.satellite_cn || {};
  const weights = sat.confirm_score?.weights || {};
  const sentScoring = sat.sentiment?.scoring?.sent_score_map || {};
  const sentRules = sat.sentiment?.rules || {};
  const te = sat.technical_engine || {};
  const entry = te.pullback_setup?.entry || {};
  const stop = te.stop || {};

  el.innerHTML = `
    <div class="grid3" style="gap:var(--sp-4)">
      <div>
        <div style="font-weight:700;margin-bottom:8px">Confirm Score 权重</div>
        <div class="detail-item"><span class="dl">量价 (flow)</span><span class="dv mono">${((weights.flow||0.5)*100).toFixed(0)}%</span></div>
        <div class="detail-item"><span class="dl">基本面</span><span class="dv mono">${((weights.fundamental||0.2)*100).toFixed(0)}%</span></div>
        <div class="detail-item"><span class="dl">情绪</span><span class="dv mono">${((weights.sent||0.3)*100).toFixed(0)}%</span></div>
      </div>
      <div>
        <div style="font-weight:700;margin-bottom:8px">情绪规则</div>
        <div class="detail-item"><span class="dl">${SENT_TAGS['HOT']||'HOT'}</span><span class="dv mono">${sentScoring.HOT||62}分 / ×${sentRules.HOT?.position_cap_multiplier||0.5}</span></div>
        <div class="detail-item"><span class="dl">${SENT_TAGS['NEUTRAL']||'NEUTRAL'}</span><span class="dv mono">${sentScoring.NEUTRAL||70}分 / 标准</span></div>
        <div class="detail-item"><span class="dl">${SENT_TAGS['PANIC']||'PANIC'}</span><span class="dv mono">${sentScoring.PANIC||50}分 / 需PASS</span></div>
      </div>
      <div>
        <div style="font-weight:700;margin-bottom:8px">入场 / 止损</div>
        <div class="detail-item"><span class="dl">入场偏移</span><span class="dv mono">${((entry.price_offset_pct||(-0.002))*100).toFixed(1)}%</span></div>
        <div class="detail-item"><span class="dl">止损A</span><span class="dv mono">ENTRY - ${stop.rule_a?.mult||2.0}×ATR</span></div>
        <div class="detail-item"><span class="dl">止损B</span><span class="dv mono">LOW20 - ${stop.rule_b?.mult||0.5}×ATR</span></div>
        <div class="detail-item"><span class="dl">取值</span><span class="dv mono">min(A, B)</span></div>
      </div>
    </div>
  `;

  // Toggle interaction
  document.getElementById('paramsToggle')?.addEventListener('click', () => {
    const open = el.style.display !== 'none';
    el.style.display = open ? 'none' : 'block';
    const h3 = document.querySelector('#paramsToggle h3');
    if(h3) h3.textContent = open ? '策略参数 ▸' : '策略参数 ▾';
  });
}

// ==================== 策略流程说明 ====================

function renderFlowGuide(){
  const el = document.getElementById('flowBody');
  if(!el) return;

  el.innerHTML = `
    <div class="flow-guide">
      <div class="flow-steps">
        <div class="flow-step">
          <div class="flow-num">1</div>
          <div>
            <strong>股票池</strong>
            <div class="muted">排除 ST、停牌、低流动性、黑名单和冷却期。</div>
          </div>
        </div>

        <div class="flow-step">
          <div class="flow-num">2</div>
          <div>
            <strong>趋势门</strong>
            <div class="muted">MA20 > MA60，趋势不清则跳过。</div>
          </div>
        </div>

        <div class="flow-step">
          <div class="flow-num">3</div>
          <div>
            <strong>回调区</strong>
            <div class="muted">等待回到 MA20 附近，不追高。</div>
          </div>
        </div>

        <div class="flow-step">
          <div class="flow-num">4</div>
          <div>
            <strong>触发</strong>
            <div class="muted">收盘重新站上 MA20 且收阳。</div>
          </div>
        </div>

        <div class="flow-step">
          <div class="flow-num">5</div>
          <div>
            <strong>质量 / 事件</strong>
            <div class="muted">基本面和重大负面事件不过线则否决。</div>
          </div>
        </div>

        <div class="flow-step">
          <div class="flow-num">6</div>
          <div>
            <strong>硬否决</strong>
            <div class="muted">破位、流动性、事件、黑名单触发则放弃。</div>
          </div>
        </div>

        <div class="flow-step">
          <div class="flow-num">7</div>
          <div>
            <strong>量价</strong>
            <div class="muted">回调缩量、触发放量优先。</div>
          </div>
        </div>

        <div class="flow-step">
          <div class="flow-num">8</div>
          <div>
            <strong>仓位 / 草案</strong>
            <div class="muted">超过阈值才生成草案，需人工复核。</div>
          </div>
        </div>
      </div>
    </div>
  `;
}

// ==================== 操作控制 ====================

async function handleEodScan(){
  const btn = document.getElementById('btnEodScan');
  const resultEl = document.getElementById('scanActionResult');
  if(!btn) return;

  const origText = btn.textContent;
  btn.disabled = true;
  btn.textContent = '扫描中...';
  resultEl.style.display = 'block';
  resultEl.innerHTML = '<div class="muted">正在运行 EOD 扫描，请稍候...</div>';

  try {
    const res = await jpost('/api/strategy/scan/eod', {});
    resultEl.innerHTML = `
      <div style="padding:8px;border-radius:var(--radius-md);background:var(--bg-surface);border:1px solid var(--line)">
        <div style="font-weight:700;margin-bottom:4px">扫描完成 · ${res.scan_date||'--'}</div>
        <div class="muted">
          Universe ${res.universe_pass||0} →
          趋势 ${res.trend_pass||0} →
          回调 ${res.pullback_zone||0} →
          触发 ${res.triggered||0} →
          草案 ${res.orders_created||0}
        </div>
      </div>
    `;
    const [newCandidates, newOrders, newScanLatest] = await Promise.all([
      jget('/api/strategy/candidates').catch(()=>({rows:[]})),
      jget('/api/strategy/orders').catch(()=>({rows:[]})),
      jget('/api/strategy/scan/latest').catch(()=>({scan_date:null,total:0})),
    ]);
    renderCandidateTable(newCandidates);
    renderOrderCards(newOrders);
    renderFunnel(newScanLatest, newCandidates);
  } catch(e) {
    resultEl.innerHTML = `<div style="color:var(--bad)">扫描失败: ${String(e)}</div>`;
  } finally {
    btn.disabled = false;
    btn.textContent = origText;
  }
}

async function handleRiskRefresh(){
  const btn = document.getElementById('btnRiskRefresh');
  const resultEl = document.getElementById('scanActionResult');
  if(!btn) return;

  const origText = btn.textContent;
  btn.disabled = true;
  btn.textContent = '刷新中...';
  resultEl.style.display = 'block';
  resultEl.innerHTML = '<div class="muted">正在刷新风控快照...</div>';

  try {
    await jpost('/api/strategy/risk-snapshot/refresh', {});
    const [riskSnapshot, trendState, sentiment, ipsWrap] = await Promise.all([
      jget('/api/strategy/risk-snapshot').catch(()=>({as_of:null})),
      jget('/api/strategy/state').catch(()=>({trend_state:'RISK_NEUTRAL'})),
      jget('/api/strategy/sentiment').catch(()=>({sent_state:'NEUTRAL'})),
      jget('/policy/ips').catch(()=>({value:{}})),
    ]);
    const ips = ipsWrap?.value || {};
    renderIpsLive(ips, riskSnapshot, ipsWrap?.updated_at, trendState, sentiment);
    resultEl.innerHTML = '<div style="color:var(--good)">风控快照已刷新</div>';
  } catch(e) {
    resultEl.innerHTML = `<div style="color:var(--bad)">刷新失败: ${String(e)}</div>`;
  } finally {
    btn.disabled = false;
    btn.textContent = origText;
  }
}

async function handleIntradayRefresh(){
  const btn = document.getElementById('btnIntradayRefresh');
  const resultEl = document.getElementById('scanActionResult');
  if(!btn) return;

  const origText = btn.textContent;
  btn.disabled = true;
  btn.textContent = '刷新中...';
  resultEl.style.display = 'block';
  resultEl.innerHTML = '<div class="muted">正在运行盘中刷新...</div>';

  try {
    const res = await jpost('/api/strategy/intraday/refresh', {});

    if(res.skipped_non_trading){
      resultEl.innerHTML = '<div class="muted">非交易时段，已跳过</div>';
    } else {
      const changes = res.changes||[];
      const changeLines = changes.map(c =>
        `<span class="mono">${c.symbol}</span> ${c.old_stage} → <strong>${c.new_stage}</strong>`
      ).join('<br>');

      resultEl.innerHTML = `
        <div style="padding:8px;border-radius:var(--radius-md);background:var(--bg-surface);border:1px solid var(--line)">
          <div style="font-weight:700;margin-bottom:4px">盘中刷新完成</div>
          <div class="muted">
            监控 ${res.refreshed||0} 只 |
            升级 ${res.upgraded||0} |
            无效 ${res.invalidated||0} |
            草案 ${res.orders_created||0}
          </div>
          ${changeLines ? `<div style="margin-top:6px;font-size:var(--fs-sm)">${changeLines}</div>` : ''}
        </div>
      `;

      // 如果有变更，刷新候选表和行动草案
      if(changes.length > 0){
        const [newCandidates, newOrders] = await Promise.all([
          jget('/api/strategy/candidates').catch(()=>({rows:[]})),
          jget('/api/strategy/orders').catch(()=>({rows:[]})),
        ]);
        renderCandidateTable(newCandidates);
        renderOrderCards(newOrders);
      }
    }
  } catch(e) {
    resultEl.innerHTML = `<div style="color:var(--bad)">盘中刷新失败: ${String(e)}</div>`;
  } finally {
    btn.disabled = false;
    btn.textContent = origText;
  }
}

// ==================== 扫描历史 ====================

function renderScanHistory(data){
  const el = document.getElementById('scanHistBody');
  if(!el) return;

  const rows = data.rows || [];
  if(rows.length === 0){
    el.innerHTML = '<div class="muted">暂无扫描记录</div>';
  } else {
    el.innerHTML = `
      <table class="tableWide">
        <thead>
          <tr>
            <th style="width:120px">日期</th>
            <th class="num" style="width:80px">总数</th>
            <th class="num" style="width:80px">趋势</th>
            <th class="num" style="width:80px">回调</th>
            <th class="num" style="width:80px">触发</th>
          </tr>
        </thead>
        <tbody>
          ${rows.map(r => `
            <tr class="candidate-row" data-scan-date="${r.scan_date}" style="cursor:pointer">
              <td class="mono">${r.scan_date||'—'}</td>
              <td class="mono num">${r.total||0}</td>
              <td class="mono num">${r.trend_pass||0}</td>
              <td class="mono num">${r.pullback_zone||0}</td>
              <td class="mono num">${r.triggered||0}</td>
            </tr>
          `).join('')}
        </tbody>
      </table>
    `;
    el.querySelectorAll('tr[data-scan-date]').forEach(tr => {
      tr.addEventListener('click', async () => {
        const scanDate = tr.dataset.scanDate;
        try {
          const hist = await jget(`/api/strategy/candidates?scan_date=${scanDate}`);
          renderCandidateTable(hist);
          renderFunnel({ scan_date: scanDate, total: hist.rows?.length||0 }, hist);
        } catch(e) {
          console.error('加载扫描历史失败:', e);
        }
      });
    });
  }

  document.getElementById('scanHistToggle')?.addEventListener('click', () => {
    const open = el.style.display !== 'none';
    el.style.display = open ? 'none' : 'block';
    const h3 = document.querySelector('#scanHistToggle h3');
    if(h3) h3.textContent = open ? '扫描历史 ▸' : '扫描历史 ▾';
  });
}

// ==================== 事件风险 ====================

const SEV_TAGS = {
  'CRITICAL': '<span class="tag risk">CRITICAL</span>',
  'HIGH':     '<span class="tag warn">HIGH</span>',
  'INFO':     '<span class="tag data">INFO</span>',
};

function renderEventRisks(data){
  const rows = data.rows || [];
  const card = document.getElementById('eventRiskCard');
  const el = document.getElementById('eventRiskList');
  if(!card || !el) return;

  if(rows.length === 0){
    card.style.display = 'none';
    return;
  }

  card.style.display = 'block';
  document.getElementById('eventRiskCount').textContent = `${rows.length} 条活跃事件`;

  el.innerHTML = rows.map(r => {
    const sevTag = SEV_TAGS[r.severity] || SEV_TAGS['INFO'];
    return `
      <div style="display:flex;gap:8px;align-items:center;padding:6px 0;border-bottom:1px solid var(--line)">
        ${sevTag}
        <span class="mono" style="font-weight:600">${r.symbol||''}</span>
        <span class="muted">${r.event_type||r.title||''}</span>
        <span class="mono muted" style="margin-left:auto;font-size:var(--fs-sm)">${r.event_date||''}</span>
      </div>
    `;
  }).join('');
}

// ==================== 黑名单管理 ====================

function renderBlacklist(data){
  const el = document.getElementById('blacklistBody');
  if(!el) return;

  const rows = data.rows || [];
  if(rows.length === 0){
    el.innerHTML = '<div class="muted">暂无活跃黑名单</div>';
    return;
  }

  el.innerHTML = rows.map(r => `
    <div style="display:flex;gap:8px;align-items:center;padding:6px 0;border-bottom:1px solid var(--line)">
      <span class="mono" style="font-weight:600">${r.symbol||''}</span>
      <span class="tag">${r.market||'CN_A'}</span>
      <span class="muted" style="flex:1">${r.reason||''}</span>
      <span class="mono muted" style="font-size:var(--fs-sm)">至 ${r.expires_at||'永久'}</span>
      <button class="pill" style="background:var(--bad);color:#fff;border:none;padding:2px 10px;cursor:pointer;font-size:var(--fs-sm)" data-bl-id="${r.id}">移除</button>
    </div>
  `).join('');

  el.querySelectorAll('button[data-bl-id]').forEach(btn => {
    btn.addEventListener('click', async () => {
      try {
        const res = await fetch(apiBase()+`/api/strategy/blacklist/${btn.dataset.blId}`, { method: 'DELETE' });
        if(!res.ok) throw new Error(`${res.status}`);
        const updated = await jget('/api/strategy/blacklist');
        renderBlacklist(updated);
      } catch(e) {
        alert('移除失败: ' + String(e));
      }
    });
  });
}

// ==================== 冷却期 ====================

function renderCooldowns(data){
  const rows = data.rows || [];
  const card = document.getElementById('cooldownCard');
  const el = document.getElementById('cooldownList');
  if(!card || !el) return;

  if(rows.length === 0){
    card.style.display = 'none';
    return;
  }

  card.style.display = 'block';
  document.getElementById('cooldownCount').textContent = `${rows.length} 个活跃`;

  el.innerHTML = rows.map(r => {
    const isBlacklist = r.stop_count >= 2 || r.blacklist_until;
    const typeTag = isBlacklist
      ? '<span class="tag risk">黑名单</span>'
      : '<span class="tag warn">冷却</span>';
    const until = r.blacklist_until || r.cooldown_until;
    const daysLeft = until
      ? Math.max(0, Math.ceil((new Date(until) - new Date()) / 86400000))
      : '—';
    return `
      <div style="display:flex;gap:8px;align-items:center;padding:6px 0;border-bottom:1px solid var(--line)">
        <span class="mono" style="font-weight:600">${r.symbol||''}</span>
        ${typeTag}
        <span class="muted">止损 ${r.stop_date||'—'}</span>
        <span class="muted">至 ${until||'—'} (${daysLeft}天)</span>
      </div>
    `;
  }).join('');
}

function renderDecisionLog(candidates, events){
  const el = document.getElementById('decisionLogRows');
  const list = document.getElementById('decisionLogList');
  if(!el) return;
  const candidateRows = (candidates.rows || []).slice(0, 8).map(row => ({
    date: row.scan_date || '—',
    symbol: row.symbol || '—',
    conclusion: row.eligible === true || row.eligible === 'true' ? '纳入观察' : '暂不进入',
    reason: candidateTrigger(row),
    action: row.stage === 'TRIGGERED' ? '等待你确认是否纳入交易计划' : '下次扫描复盘',
  }));
  const eventRows = (events.rows || []).slice(0, 4).map(row => ({
    date: row.event_date || row.created_at || '—',
    symbol: row.symbol || '—',
    conclusion: row.severity || '事件',
    reason: row.event_type || row.title || '事件风险',
    action: '观察事件解除或继续跟踪',
  }));
  const rows = [...eventRows, ...candidateRows].slice(0, 10);
  if (list) {
    list.innerHTML = rows.map((row, index) => `
      <button class="researchListRow" type="button" data-decision-index="${index}">
        <span>
          <strong>${row.symbol}</strong>
          <small>${row.date} · ${row.reason}</small>
        </span>
        <em><b>${row.conclusion}</b></em>
      </button>
    `).join('') || '<div class="mobileEmpty">暂无决策日志</div>';
    list.querySelectorAll('[data-decision-index]').forEach((button) => {
      button.addEventListener('click', () => {
        const row = rows[Number(button.dataset.decisionIndex)];
        if (!row) return;
        openBottomSheet(row.symbol || '决策日志', `
          <div class="sheetDetailGrid">
            <div><span class="muted">日期</span><strong class="mono">${row.date}</strong></div>
            <div><span class="muted">结论</span><strong>${row.conclusion}</strong></div>
          </div>
          <div class="sheetBlock"><div class="muted">理由</div><div>${row.reason}</div></div>
          <div class="sheetBlock"><div class="muted">后续动作</div><div>${row.action}</div></div>
        `);
      });
    });
  }
  el.innerHTML = rows.map(row => `
    <tr>
      <td class="mono">${row.date}</td>
      <td class="mono">${row.symbol}</td>
      <td>${row.conclusion}</td>
      <td>${row.reason}</td>
      <td>${row.action}</td>
    </tr>
  `).join('') || '<tr><td colspan="5" class="muted" style="text-align:center;padding:20px">暂无决策日志</td></tr>';
}

// ==================== 主流程 ====================

async function main(){
  setActiveNav();

  const [scanLatest, candidates, riskSnapshot, orders, trendState, sentiment, ipsWrap, holdingSummary,
         blacklist, cooldowns, events, scanHistory] = await Promise.all([
    jget('/api/strategy/scan/latest').catch(()=>({scan_date:null,total:0})),
    jget('/api/strategy/candidates').catch(()=>({rows:[]})),
    jget('/api/strategy/risk-snapshot').catch(()=>({as_of:null})),
    jget('/api/strategy/orders').catch(()=>({rows:[]})),
    jget('/api/strategy/state').catch(()=>({trend_state:'RISK_NEUTRAL'})),
    jget('/api/strategy/sentiment').catch(()=>({sent_state:'NEUTRAL'})),
    jget('/policy/ips').catch(()=>({value:{}})),
    jget('/api/holdings/summary').catch(()=>({total:{value:0,cost:0,pnl:0},by_category:[]})),
    jget('/api/strategy/blacklist').catch(()=>({rows:[]})),
    jget('/api/strategy/cooldowns').catch(()=>({rows:[]})),
    jget('/api/strategy/events?limit=10').catch(()=>({rows:[]})),
    jget('/api/strategy/scan/history?limit=10').catch(()=>({rows:[]})),
  ]);

  // 策略说明
  renderFlowGuide();

  // IPS 实况
  const ips = ipsWrap?.value || {};
  renderIpsLive(ips, riskSnapshot, ipsWrap?.updated_at, trendState, sentiment);
  renderHoldingsAlign(ips, holdingSummary, riskSnapshot);
  renderIpsParams(ips);

  // 扫描历史
  renderScanHistory(scanHistory);

  // 策略扫描
  renderFunnel(scanLatest, candidates);
  renderCandidateTable(candidates);
  renderOrderCards(orders);
  renderDecisionLog(candidates, events);

  // 风控面板
  renderEventRisks(events);
  renderBlacklist(blacklist);
  renderCooldowns(cooldowns);

  // 按钮绑定
  document.getElementById('btnEodScan')?.addEventListener('click', handleEodScan);
  document.getElementById('btnIntradayRefresh')?.addEventListener('click', handleIntradayRefresh);
  document.getElementById('btnRiskRefresh')?.addEventListener('click', handleRiskRefresh);

  // 黑名单折叠 (独立绑定，因为 renderBlacklist 可能被重复调用)
  document.getElementById('blacklistToggle')?.addEventListener('click', () => {
    const bl = document.getElementById('blacklistBody');
    if(!bl) return;
    const open = bl.style.display !== 'none';
    bl.style.display = open ? 'none' : 'block';
    const h3 = document.querySelector('#blacklistToggle h3');
    if(h3) h3.textContent = open ? '黑名单管理 ▸' : '黑名单管理 ▾';
  });
}

main().catch(err=>{
  document.getElementById('err').style.display='block';
  document.getElementById('err').textContent = '加载失败：'+String(err);
});
