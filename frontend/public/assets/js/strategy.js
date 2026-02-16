import { jget, jpost, apiBase } from './api.js';
import { setActiveNav } from './ui.js';

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
  'TRIGGERED':     '<span class="tag risk">触发</span>',
  'PULLBACK_ZONE': '<span class="tag warn">回调区</span>',
  'TREND_PASS':    '<span class="tag data">趋势</span>',
  'UNIVERSE_PASS': '<span class="tag">Universe</span>',
};

const FLOW_TAGS = {
  'PASS': '<span class="tag data">PASS</span>',
  'WEAK': '<span class="tag warn">WEAK</span>',
  'FAIL': '<span class="tag risk">FAIL</span>',
};

// ==================== Reason Code 中文映射 ====================

const REASON_LABELS = {
  'RC_U001_ELIGIBLE':                  { label: 'Universe 通过', cat: 'pass' },
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
  'RC_E001_ORDER_DRAFT_CREATED':       { label: '订单草稿已创建', cat: 'info' },
  'RC_E002_MANUAL_CONFIRM_REQUIRED':   { label: '等待股东确认', cat: 'info' },
  'RC_E003_AUTO_EXECUTED':             { label: '自动执行', cat: 'info' },
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
    { label: '生成订单',   count: eligible,     color: 'var(--good)' },
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
    ? '<span class="tag risk">VETO</span>'
    : '<span class="tag data">PASS</span>';
  const eligibleTag = r.eligible === true || r.eligible === 'true'
    ? '<span class="tag data">ELIGIBLE</span>'
    : '<span class="tag risk">BLOCKED</span>';

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
        <div class="detail-item"><span class="dl">Flow</span><span class="dv">${FLOW_TAGS[r.flow_result]||'—'}</span></div>
        <div class="detail-item"><span class="dl">综合评分</span><span class="dv">${r.confirm_score?fmtNum(r.confirm_score,1):'—'}</span></div>
        <div class="detail-item"><span class="dl">质量分</span><span class="dv">${r.quality_score!=null?fmtNum(r.quality_score,1):'—'}</span></div>
        <div class="detail-item"><span class="dl">情绪</span><span class="dv">${r.sent_state?(SENT_TAGS[r.sent_state]||r.sent_state):'—'} ${r.sent_score!=null?fmtNum(r.sent_score,0):''}</span></div>
        <div class="detail-item"><span class="dl">Hard Veto</span><span class="dv">${vetoTag}</span></div>
        <div class="detail-item"><span class="dl">结论</span><span class="dv">${eligibleTag}</span></div>
        <div class="detail-item"><span class="dl">仓位</span><span class="dv">${r.position_size||'—'} 股</span></div>
        <div class="detail-item"><span class="dl">风险额</span><span class="dv">${r.risk_amount_cny?'¥'+fmtNum(r.risk_amount_cny,0):'—'}</span></div>
      </div>
    </div>
  `;
}

// ==================== 可展开候选表格 ====================

function renderCandidateTable(data){
  const rows = data.rows||[];
  const tbody = document.getElementById('candidateRows');
  tbody.innerHTML = '';

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
      <td>${STAGE_TAGS[r.stage]||r.stage}</td>
      <td class="mono num">${fmtNum(r.close)}</td>
      <td class="mono num">${fmtNum(r.ma20)}</td>
      <td class="mono num">${fmtNum(r.ma60)}</td>
      <td class="mono num">${fmtNum(r.atr14)}</td>
      <td class="mono num">${r.entry_price?fmtNum(r.entry_price):'—'}</td>
      <td class="mono num">${r.stop_price?fmtNum(r.stop_price):'—'}</td>
      <td>${FLOW_TAGS[r.flow_result]||'—'}</td>
      <td class="mono num" style="color:${scoreColor};font-weight:700">${r.confirm_score?fmtNum(r.confirm_score,1):'—'}</td>
    `;

    const detailTr = document.createElement('tr');
    detailTr.className = 'candidate-detail';
    detailTr.innerHTML = `<td colspan="11">${renderCandidateDetail(r)}</td>`;

    tr.addEventListener('click', () => {
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

// ==================== 订单管理 ====================

const STATUS_TAGS = {
  'pending':   '<span class="tag warn">待确认</span>',
  'confirmed': '<span class="tag data">已确认</span>',
  'executed':  '<span class="tag" style="background:var(--good);color:#000">已执行</span>',
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
  if(status==='pending'){
    return `
      <button class="pill" style="background:var(--good);color:#000;cursor:pointer;border:none;padding:4px 16px;border-radius:4px" data-order-id="${o.id}" data-action="confirm">确认</button>
      <button class="pill" style="background:var(--bad);color:#fff;cursor:pointer;border:none;padding:4px 16px;border-radius:4px" data-order-id="${o.id}" data-action="cancel">取消</button>
    `;
  }
  if(status==='confirmed'){
    return `
      <button class="pill" style="background:var(--good);color:#000;cursor:pointer;border:none;padding:4px 16px;border-radius:4px" data-order-id="${o.id}" data-action="execute">标记已执行</button>
      <button class="pill" style="background:var(--bad);color:#fff;cursor:pointer;border:none;padding:4px 16px;border-radius:4px" data-order-id="${o.id}" data-action="cancel">取消</button>
    `;
  }
  return '';
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
          <span class="muted" style="margin-left:8px">${o.name||''}</span>
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

  container.querySelectorAll('button[data-order-id]').forEach(btn => {
    btn.addEventListener('click', () => handleOrderAction(btn.dataset.orderId, btn.dataset.action));
  });
}

async function handleOrderAction(orderId, action){
  try {
    if(action==='execute'){
      await jpost(`/api/strategy/orders/${orderId}/execute`, {});
    } else {
      await jpost(`/api/strategy/orders/${orderId}/action`, { action });
    }
    const orders = await jget('/api/strategy/orders');
    renderOrderCards(orders);
  } catch(e) {
    alert('操作失败: ' + String(e));
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

  el.innerHTML = [
    gaugeCard('持仓数', openPos, maxPos, '', false),
    gaugeCard('并发风险', concRisk, concCap, '%', false),
    gaugeCard('月度回撤', mdd, cbPause, '%', false),
    `<div class="card">
      <div class="muted">B_NAV</div>
      <div class="mono" style="font-size:var(--fs-xl);font-weight:700">¥${fmtNum(bNav, 0)}</div>
      <div class="muted" style="margin-top:4px;font-size:var(--fs-sm)">配置: ¥${fmtNum(bNavConfig, 0)}</div>
    </div>`,
    `<div class="card">
      <div class="muted">熔断状态</div>
      <div style="margin-top:6px">${cbTag}</div>
      <div class="muted" style="margin-top:6px;font-size:var(--fs-sm)">暂停 ${fmtNum((cb.pause_new_entries_drawdown_pct||0.04)*100,1)}% / 减仓 ${fmtNum((cb.reduce_only_drawdown_pct||0.07)*100,1)}%</div>
    </div>`,
    `<div class="card">
      <div class="muted">风险模式</div>
      <div style="margin-top:6px">${riskMode}</div>
      <div class="muted" style="margin-top:6px;font-size:var(--fs-sm)">单笔风险 ${fmtNum((budget.per_trade_risk_pct?.defensive||0.005)*100,2)}%</div>
    </div>`,
    `<div class="card">
      <div class="muted">宏观趋势</div>
      <div style="margin-top:6px">${trendTag}</div>
      <div class="muted" style="margin-top:6px;font-size:var(--fs-sm)">SPX ${spxClose} / MA10 ${spxMa10}</div>
    </div>`,
    `<div class="card">
      <div class="muted">市场情绪</div>
      <div style="margin-top:6px">${sentTag}</div>
      <div class="muted" style="margin-top:6px;font-size:var(--fs-sm)">${sentBreadth}</div>
    </div>`,
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

  const rows = cats.map(c => {
    const val = Number(c.total_value_base) || 0;
    const pct = val / totalVal * 100;
    const color = CAT_COLORS[c.major_category] || 'var(--info)';
    return `
      <div class="align-row">
        <div style="font-weight:600">${c.major_category}</div>
        <div class="mono muted">${c.holdings_count}仓</div>
        <div class="mono">¥${fmtNum(val, 0)}</div>
        <div class="mono" style="color:${color}">${fmtNum(pct, 1)}%</div>
        <div class="align-bar-wrap"><div class="align-bar" style="width:${Math.min(pct, 100)}%;background:${color}"></div></div>
      </div>
    `;
  });

  const totalPnl = Number(total.pnl) || 0;
  const pnlColor = totalPnl >= 0 ? 'var(--good)' : 'var(--bad)';
  const totalCount = cats.reduce((s, c) => s + (c.holdings_count || 0), 0);

  rows.push(`
    <div class="align-row" style="font-weight:700;border-top:1px solid var(--line-bright);padding-top:8px">
      <div>总计</div>
      <div class="mono muted">${totalCount}仓</div>
      <div class="mono">¥${fmtNum(totalVal, 0)}</div>
      <div class="mono" style="color:${pnlColor}">${totalPnl >= 0 ? '+' : ''}¥${fmtNum(totalPnl, 0)}</div>
      <div></div>
    </div>
  `);

  el.innerHTML = rows.join('');
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
      <p style="margin-bottom:12px;color:var(--text-secondary)">
        PAMS 每天收盘后自动扫描 A 股市场，经过 <strong>8 道筛选</strong> 找出值得买入的股票，
        并自动计算买多少股、在什么价格买、亏多少该止损。最终由你手动确认后才会执行。
      </p>

      <div class="flow-steps">
        <div class="flow-step">
          <div class="flow-num">1</div>
          <div>
            <strong>选股票池</strong>
            <div class="muted">从全部 A 股中去掉 ST、停牌、成交量太小的股票，排除黑名单和冷却期内的标的。剩下的就是"可投资池"。</div>
          </div>
        </div>

        <div class="flow-step">
          <div class="flow-num">2</div>
          <div>
            <strong>看大趋势</strong>
            <div class="muted">只买"正在涨"的股票——20 日均线在 60 日均线上方，且近 40 天最大涨幅超过 12%。均线缠在一起说明没方向，也跳过。</div>
          </div>
        </div>

        <div class="flow-step">
          <div class="flow-num">3</div>
          <div>
            <strong>等回调</strong>
            <div class="muted">趋势向上的股票不追高买入，而是等它回调到 20 日均线附近（±1 倍 ATR 波幅），在便宜的位置"接"。</div>
          </div>
        </div>

        <div class="flow-step">
          <div class="flow-num">4</div>
          <div>
            <strong>等信号</strong>
            <div class="muted">回调到位后，等某一天收盘价站上 20 日均线、且当天收阳（收盘 > 开盘）——这就是"触发日"，说明跌够了要反弹。</div>
          </div>
        </div>

        <div class="flow-step">
          <div class="flow-num">5</div>
          <div>
            <strong>查基本面</strong>
            <div class="muted">检查财务健康度（ROE、毛利率、负债率、现金流），有无预亏公告或立案调查。基本面太差的一票否决。</div>
          </div>
        </div>

        <div class="flow-step">
          <div class="flow-num">6</div>
          <div>
            <strong>硬否决</strong>
            <div class="muted">6 条铁律，触发任何一条直接放弃：跌破 60 日线放量、收盘低于回调下沿、重大负面事件、连续跌停、恐慌+指数破位、黑名单。</div>
          </div>
        </div>

        <div class="flow-step">
          <div class="flow-num">7</div>
          <div>
            <strong>量价确认</strong>
            <div class="muted">检查成交量是否配合——回调时缩量（卖的人少）、触发时放量（买的人多）。量价都好评 PASS，不太好评 WEAK，放量大阴线直接 FAIL。</div>
          </div>
        </div>

        <div class="flow-step">
          <div class="flow-num">8</div>
          <div>
            <strong>算仓位 → 生成订单</strong>
            <div class="muted">根据你的总资产和风险预算，自动算出"买多少股"和"亏到多少该卖"。
              综合评分（量价 50% + 基本面 20% + 情绪 30%）超过 55 分才生成订单草稿，等你确认后才执行。</div>
          </div>
        </div>
      </div>

      <div style="margin-top:16px;padding:10px;border-radius:var(--radius-md);background:var(--bg-surface);border:1px solid var(--line)">
        <div style="font-weight:700;margin-bottom:6px">风控保护</div>
        <div class="muted" style="line-height:1.7">
          单笔最多亏总资产的 0.5%（防御模式）· 所有持仓同时最多亏 2% ·
          同行业不超 25% · 月度亏 4% 暂停开仓、亏 7% 只许卖 ·
          市场过热自动减半仓 · 市场恐慌收紧条件 · 止损后冷却 5 天
        </div>
      </div>
    </div>
  `;

  document.getElementById('flowToggle')?.addEventListener('click', () => {
    const open = el.style.display !== 'none';
    el.style.display = open ? 'none' : 'block';
    const h3 = document.querySelector('#flowToggle h3');
    if(h3) h3.textContent = open ? '策略流程说明 ▸' : '策略流程说明 ▾';
  });
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
  resultEl.innerHTML = '<div class="muted">正在执行 EOD 扫描，请稍候...</div>';

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
          订单 ${res.orders_created||0}
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
  resultEl.innerHTML = '<div class="muted">正在执行盘中刷新...</div>';

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
            订单 ${res.orders_created||0}
          </div>
          ${changeLines ? `<div style="margin-top:6px;font-size:var(--fs-sm)">${changeLines}</div>` : ''}
        </div>
      `;

      // 如果有变更，刷新候选表和订单
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
