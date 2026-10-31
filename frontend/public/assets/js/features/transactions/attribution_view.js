import { escapeHtml } from '../../utils.js?v=20261031-gray-v1';

export function createAttributionView({state, scope, legacy}) {
const $ = scope.byId;
const {autoConfidenceLabel, autoEvidenceRows, autoLevelTone, autoSuggestedTagButtons, formatCnyEffect, formatCnyEffectCompact, formatDecisionRate, formatPctPoint, horizonMetric, pctTone, reviewAttributionLabel, sideLabel, tradeReviewActionPatterns, tradeReviewAssetBreakdownTone, tradeReviewAssetBreakdownValue, tradeReviewAssetFullLabel, tradeReviewAssetMeta, tradeReviewAssetName, tradeReviewAssetPrimaryPnl, tradeReviewAssetStatusText, tradeReviewAssetTags, tradeReviewAssetTileClass, tradeReviewAssetTileStyle, tradeReviewAssetTileText, tradeReviewAssetTiles, tradeReviewAssetTone, tradeReviewContributionLabel, tradeReviewDirectionFilterButton, tradeReviewDirectionMatchesFilter, tradeReviewDirectionSort, tradeReviewDirectionTradeRow, tradeReviewDirectionalTrades, tradeReviewHeatTileStyle, tradeReviewHeatTone, tradeReviewNumber, tradeReviewPatternReasonFold, tradeReviewPatternTiles, tradeReviewTradeMoveLabel, tradeReviewTradeReason, tradeReviewTradeTags, tradeReviewTradeTone, tradeReviewTreemapLayout, updateTradeReviewCompletion, workbenchTone} = legacy;

function TradeReviewAssetAttributionBridge(asset) {
  const rows = asset?.attribution_breakdown || [];
  return `
    <div class="tradeReviewAttributionBridge">
      ${rows.map((item) => `
        <div class="tradeReviewAttributionBridgeItem ${escapeHtml(item.status || '')}">
          <span>${escapeHtml(item.label || '')}</span>
          <strong class="${tradeReviewAssetBreakdownTone(item)}">${escapeHtml(tradeReviewAssetBreakdownValue(item))}</strong>
          <small>${escapeHtml(item.note || '')}</small>
        </div>
      `).join('')}
    </div>
  `;
}

function TradeReviewAssetRuleInsights(asset) {
  const insights = asset?.rule_insights || [];
  return `
    <div class="tradeReviewRuleInsightList">
      ${insights.length ? insights.map((item) => `
        <div class="tradeReviewRuleInsight ${workbenchTone(item.level)}">
          <strong>${escapeHtml(item.title || '--')}</strong>
          <p>${escapeHtml(item.detail || '')}</p>
        </div>
      `).join('') : '<div class="emptyState">暂无规则建议。</div>'}
    </div>
  `;
}

function TradeReviewAssetTradeCard(trade) {
  const tone = tradeReviewTradeTone(trade);
  const tags = tradeReviewTradeTags(trade);
  return `
    <button type="button" class="tradeReviewAssetTrade ${tone}" data-trade-review="${escapeHtml(String(trade.trade_id || ''))}">
      <div class="tradeReviewAssetTradeTop">
        <span>${escapeHtml(trade.trade_date || '--')} · ${escapeHtml(sideLabel(trade.side))}</span>
        <b class="${tone}">${escapeHtml(tradeReviewContributionLabel(trade))}</b>
      </div>
      <div class="tradeReviewAssetTradeMain">
        <strong>${escapeHtml(trade.label || trade.name || trade.symbol || '--')}</strong>
        <em class="${pctTone(trade.estimated_decision_effect_cny)}">${escapeHtml(formatCnyEffect(trade.estimated_decision_effect_cny))}</em>
      </div>
      <p>${escapeHtml(tradeReviewTradeReason(trade))}</p>
      <div class="tradeReviewAssetTradeMeta">
        <span>${escapeHtml(tradeReviewTradeMoveLabel(trade))}</span>
        ${tags}
      </div>
    </button>
  `;
}

function TradeReviewAssetAttributionDetail(asset, selected) {
  const trades = (asset.trades || []).slice(0, 8);
  const positiveCount = asset.positive_count ?? trades.filter((trade) => tradeReviewTradeTone(trade) === 'gain').length;
  const negativeCount = asset.negative_count ?? trades.filter((trade) => tradeReviewTradeTone(trade) === 'loss').length;
  const actualPnl = tradeReviewAssetPrimaryPnl(asset);
  return `
    <section class="tradeReviewAssetDetail tradeReviewAssetAttributionDetail" data-asset-heat-detail="${escapeHtml(asset.key)}"${selected ? '' : ' hidden'}>
      <div class="tradeReviewAssetDetailHead">
        <div>
          <span>${escapeHtml(tradeReviewAssetMeta(asset))}</span>
          <strong>${escapeHtml(tradeReviewAssetName(asset))}</strong>
          <small>${escapeHtml(tradeReviewAssetStatusText(asset))}</small>
        </div>
        <em class="${pctTone(actualPnl)}">${escapeHtml(formatCnyEffect(actualPnl))}</em>
      </div>
      <div class="tradeReviewAssetDetailStats">
        <span>${escapeHtml(asset.trade_count || 0)} 笔</span>
        <span>正 ${escapeHtml(positiveCount)}</span>
        <span>负 ${escapeHtml(negativeCount)}</span>
        <span>动作 ${escapeHtml(formatCnyEffect(asset.estimated_decision_effect_cny))}</span>
      </div>
      ${TradeReviewAssetAttributionBridge(asset)}
      ${TradeReviewAssetRuleInsights(asset)}
      ${tradeReviewAssetTags(asset) ? `<div class="tradeReviewAssetTagStrip">${tradeReviewAssetTags(asset)}</div>` : ''}
      <div class="tradeReviewAssetTradeList attribution">
        ${trades.length ? trades.map(TradeReviewAssetTradeCard).join('') : '<div class="emptyState">暂无可打开的交易。</div>'}
      </div>
    </section>
  `;
}

function TradeReviewDirectionalTradePanel(payload) {
  const trades = tradeReviewDirectionalTrades(payload).sort(tradeReviewDirectionSort);
  const limit = 10;
  const counts = {
    all: trades.length,
    loss: trades.filter((trade) => tradeReviewDirectionMatchesFilter(trade, 'loss')).length,
    gain: trades.filter((trade) => tradeReviewDirectionMatchesFilter(trade, 'gain')).length,
    buy: trades.filter((trade) => tradeReviewDirectionMatchesFilter(trade, 'buy')).length,
    sell: trades.filter((trade) => tradeReviewDirectionMatchesFilter(trade, 'sell')).length,
  };
  const visibleCount = Math.min(limit, counts.all);
  return `
    <section class="tradeReviewDirectionalPanel" data-direction-panel data-direction-limit="${escapeHtml(limit)}">
      <div class="tradeReviewDirectionalIntro">
        <div>
          <strong>交易动作明细</strong>
          <span data-direction-count>显示 ${escapeHtml(visibleCount)} / ${escapeHtml(counts.all)} 笔</span>
        </div>
        <button type="button" class="tradeReviewDirectionFilterToggle" data-direction-filter-toggle aria-expanded="false">
          筛选
        </button>
      </div>
      <div class="tradeReviewDirectionFilters" data-direction-filter-panel hidden>
        ${tradeReviewDirectionFilterButton('全部', 'all', counts.all, true)}
        ${tradeReviewDirectionFilterButton('负向', 'loss', counts.loss)}
        ${tradeReviewDirectionFilterButton('正向', 'gain', counts.gain)}
        ${tradeReviewDirectionFilterButton('买入', 'buy', counts.buy)}
        ${tradeReviewDirectionFilterButton('卖出', 'sell', counts.sell)}
      </div>
      <div class="tradeReviewDirectionList" data-direction-list>
        ${trades.length ? trades.map((trade, index) => tradeReviewDirectionTradeRow(trade, index >= limit)).join('') : '<div class="emptyState">暂无对应交易。</div>'}
      </div>
    </section>
  `;
}

function TradeReviewAssetHeatmap(payload, workbench) {
  const assets = tradeReviewAssetTiles(payload);
  const layout = tradeReviewTreemapLayout(assets);
  const gainCount = payload?.asset_heatmap?.positive_action_count
    ?? assets.reduce((sum, asset) => sum + tradeReviewNumber(asset.positive_count, 0), 0);
  const lossCount = payload?.asset_heatmap?.negative_action_count
    ?? assets.reduce((sum, asset) => sum + tradeReviewNumber(asset.negative_count, 0), 0);
  if (!assets.length) {
    return '<section class="tradeReviewAssetHeatmapPanel"><div class="emptyState">暂无可生成热力图的交易。</div></section>';
  }
  return `
    <section class="tradeReviewAssetHeatmapPanel" data-asset-heatmap-panel>
      <div class="tradeReviewAssetHeatmapHead">
        <div>
          <span>标的归因热力图</span>
          <strong>${escapeHtml(assets.length)} 个标的 · 真实盈亏优先</strong>
        </div>
        <div class="tradeReviewAssetLegend">
          <span class="loss">负向动作 ${escapeHtml(lossCount)}</span>
          <span class="gain">正向动作 ${escapeHtml(gainCount)}</span>
        </div>
      </div>
      <div class="tradeReviewAssetHeatmapCanvas" data-asset-heatmap>
        ${layout.map((item) => {
          const { asset, rect } = item;
          const text = tradeReviewAssetTileText(asset, rect);
          const primaryPnl = tradeReviewAssetPrimaryPnl(asset);
          const effectText = formatCnyEffectCompact(primaryPnl);
          const actionText = formatCnyEffectCompact(asset.estimated_decision_effect_cny);
          const fullLabel = `${tradeReviewAssetFullLabel(asset)} 真实 ${effectText} / 动作 ${actionText}`;
          return `
            <button type="button"
              class="tradeReviewAssetTile ${tradeReviewAssetTone(asset)} ${tradeReviewAssetTileClass(rect)}"
              style="${tradeReviewAssetTileStyle(rect)}"
              data-asset-heat-select="${escapeHtml(asset.key)}"
              title="${escapeHtml(fullLabel)}"
              aria-pressed="false">
              <span>${escapeHtml(text.meta)}</span>
              <strong>${escapeHtml(text.name)}</strong>
              <em>${escapeHtml(effectText)}</em>
              <small>${escapeHtml(asset.trade_count || 0)} 笔 · 动作 ${escapeHtml(actionText)}</small>
            </button>
          `;
        }).join('')}
      </div>
      <div class="tradeReviewAssetFloatLayer" data-asset-heat-float hidden>
        <button type="button" class="tradeReviewAssetFloatScrim" data-asset-heat-close aria-label="关闭标的详情"></button>
        <div class="tradeReviewAssetFloatCard" role="dialog" aria-modal="true" aria-label="标的交易归因详情">
          <button type="button" class="tradeReviewAssetFloatClose" data-asset-heat-close aria-label="关闭">关闭</button>
          ${assets.map((asset) => TradeReviewAssetAttributionDetail(asset, false)).join('')}
        </div>
      </div>
      <details class="tradeReviewAnalysisFold tradeReviewCompactFold">
        <summary>
          <strong>高影响动作</strong>
          <span>逐笔正负贡献证据</span>
        </summary>
        ${TradeReviewDirectionalTradePanel(payload)}
      </details>
      <p class="tradeReviewAssetHint">热力图面积按真实盈亏优先；动作贡献用于解释买卖时点，基准/行业超额后续继续拆分。</p>
    </section>
  `;
}

function TradeReviewPatternHeatmap(patterns, opts = {}) {
  const tiles = tradeReviewPatternTiles(patterns, {
    limit: opts.heatLimit || 14,
    rowsPerGroup: opts.rowsPerGroup || 4,
  });
  const maxWeight = Math.max(...tiles.map((tile) => tile.weight), 0);
  const lossCount = tiles.filter((tile) => tile.effect < -0.01).length;
  const flatCount = tiles.filter((tile) => Math.abs(tile.effect) <= 0.01).length;
  const gainCount = tiles.filter((tile) => tile.effect > 0.01).length;
  const topLoss = tiles.filter((tile) => tile.effect < -0.01).sort((a, b) => a.effect - b.effect)[0];
  const topGain = tiles.filter((tile) => tile.effect > 0.01).sort((a, b) => b.effect - a.effect)[0];
  if (!tiles.length) {
    return '<div class="emptyState">暂无足够样本生成模式热力图。</div>';
  }
  return `
    <div class="tradeReviewHeatmap">
      <div class="tradeReviewHeatmapHead">
        <div>
          <strong>${escapeHtml(opts.title || '模式热力图')}</strong>
          <span>${escapeHtml(opts.subtitle || '面积≈交易规模，颜色=估算决策影响；标签为多选口径，不做加总。')}</span>
        </div>
        <div class="tradeReviewHeatmapLegend">
          <span class="loss">拖累 ${escapeHtml(lossCount)}</span>
          <span class="flat">中性 ${escapeHtml(flatCount)}</span>
          <span class="gain">贡献 ${escapeHtml(gainCount)}</span>
        </div>
      </div>
      <div class="tradeReviewHeatmapCanvas">
        <div class="tradeReviewHeatmapTiles">
          ${tiles.map((tile) => {
            const ratio = maxWeight > 0 ? tile.weight / maxWeight : 0;
            const sizeClass = ratio >= 0.62 ? 'is-large' : ratio >= 0.28 ? 'is-medium' : 'is-small';
            const primaryTrade = tile.examples[0];
            return `
              <article class="tradeReviewHeatTile ${tradeReviewHeatTone(tile.effect)} ${sizeClass}" style="${tradeReviewHeatTileStyle(tile, maxWeight)}">
                <div class="tradeReviewHeatTileBody">
                  <span>${escapeHtml(tile.groupTitle || '模式')}</span>
                  <strong>${escapeHtml(tile.label || '--')}</strong>
                  <em class="${pctTone(tile.effect)}">${escapeHtml(formatCnyEffect(tile.effect))}</em>
                  <div class="tradeReviewHeatTileMeta">
                    <small>样本 ${escapeHtml(tile.sampleCount)}</small>
                    <small>正向 ${escapeHtml(formatDecisionRate(tile.row.win_rate))}</small>
                  </div>
                </div>
                ${primaryTrade ? `
                  <button type="button" class="tradeReviewHeatTrade tradeReviewPatternTrade" data-trade-review="${escapeHtml(String(primaryTrade.trade_id))}">
                    <span>${escapeHtml(primaryTrade.name || primaryTrade.symbol || '--')}</span>
                    <small>${escapeHtml(primaryTrade.trade_date || '--')} · ${escapeHtml(sideLabel(primaryTrade.side))}${tile.examples.length > 1 ? ` · +${tile.examples.length - 1}` : ''}</small>
                  </button>
                ` : '<div class="tradeReviewHeatTrades muted">暂无代表交易</div>'}
                ${tradeReviewPatternReasonFold(tile.examples, { limit: 5, compact: true })}
              </article>
            `;
          }).join('')}
        </div>
        <aside class="tradeReviewHeatmapAside">
          <div class="tradeReviewHeatSummary neutral">
            <span>展示口径</span>
            <strong>${escapeHtml(tiles.length)} 个模式</strong>
            <small>行为标签可重叠，净影响不加总</small>
          </div>
          <div class="tradeReviewHeatFocusRows">
            ${topLoss ? `
              <div class="tradeReviewHeatFocus loss">
                <span>最大拖累</span>
                <strong>${escapeHtml(topLoss.label || '--')}</strong>
                <em>${escapeHtml(formatCnyEffect(topLoss.effect))}</em>
              </div>
            ` : ''}
            ${topGain ? `
              <div class="tradeReviewHeatFocus gain">
                <span>最大贡献</span>
                <strong>${escapeHtml(topGain.label || '--')}</strong>
                <em>${escapeHtml(formatCnyEffect(topGain.effect))}</em>
              </div>
            ` : ''}
          </div>
        </aside>
      </div>
    </div>
  `;
}

function TradeReviewPatternList(patterns, opts = {}) {
  const rowLimit = opts.rowLimit || 3;
  return `
    <div class="tradeReviewPatternGrid">
      ${patterns.slice(0, opts.limit || patterns.length).map((group) => `
        <article class="tradeReviewPatternCard">
          <div class="tradeReviewPatternHead">
            <strong>${escapeHtml(group.title || '--')}</strong>
            <span>${escapeHtml(group.subtitle || '')}</span>
          </div>
          <div class="tradeReviewPatternRows">
            ${(group.rows || []).length ? (group.rows || []).slice(0, rowLimit).map((row) => `
              <div class="tradeReviewPatternRow">
                <div class="tradeReviewPatternRowMain">
                  <div>
                    <strong>${escapeHtml(reviewAttributionLabel(row.key || row.label))}</strong>
                    <small>样本 ${escapeHtml(row.sample_count || 0)} · 正向 ${escapeHtml(formatDecisionRate(row.win_rate))}</small>
                  </div>
                  <div>
                    <strong class="${pctTone(row.estimated_decision_effect_cny)}">${escapeHtml(formatCnyEffect(row.estimated_decision_effect_cny))}</strong>
                    <small>${escapeHtml(row.takeaway || '')}</small>
                  </div>
                </div>
                ${(row.examples || []).length ? `
                  <div class="tradeReviewPatternExamples">
                    <span>代表交易</span>
                    ${tradeReviewPatternReasonFold(row.examples, { limit: 6 })}
                  </div>
                ` : ''}
              </div>
            `).join('') : '<div class="emptyState">暂无足够样本。</div>'}
          </div>
        </article>
      `).join('')}
    </div>
  `;
}

function TradeReviewPatternPanel(workbench, opts = {}) {
  const focusPatterns = workbench?.patterns || [];
  const universePatterns = workbench?.pattern_universe || workbench?.pattern_distribution || focusPatterns;
  const modeUniversePatterns = tradeReviewActionPatterns(universePatterns);
  const modeFocusPatterns = tradeReviewActionPatterns(focusPatterns);
  const hasFocusPatterns = modeFocusPatterns.some((group) => (group.rows || []).length);
  return `
    <section class="tradeReviewPatternPanel">
      <div class="tradeReviewPatternIntro">
        <strong>只看三条模式主线</strong>
        <span>行为标签、买入模式、卖出模式；单笔流水和仓位生命周期回交易页。</span>
      </div>
      <div class="tradeReviewFocusCue">
        <span>复盘顺序</span>
        <strong>1 行为标签</strong>
        <strong>2 买入模式</strong>
        <strong>3 卖出模式</strong>
        <small>行为标签可重叠，金额不可相加</small>
      </div>
      <div class="tradeReviewPatternSwitch" role="tablist" aria-label="模式诊断视图">
        <button type="button" class="active" data-pattern-view-target="all" aria-selected="true">复盘主线</button>
        <button type="button" data-pattern-view-target="focus" aria-selected="false" ${hasFocusPatterns ? '' : 'disabled'}>问题优先级</button>
      </div>
      <div class="tradeReviewPatternView" data-pattern-view="all">
        ${TradeReviewPatternHeatmap(modeUniversePatterns, {
          ...opts,
          heatLimit: opts.heatLimit || 9,
          rowsPerGroup: opts.rowsPerGroup || 8,
          title: '模式归因热力图',
          subtitle: '只保留第三层模式：行为标签、买入模式、卖出模式。',
        })}
      </div>
      <div class="tradeReviewPatternView" data-pattern-view="focus" hidden>
        ${TradeReviewPatternHeatmap(modeFocusPatterns, {
          ...opts,
          heatLimit: opts.focusHeatLimit || 9,
          rowsPerGroup: opts.focusRowsPerGroup || 4,
          title: '问题优先级热力图',
          subtitle: '只看行为、买入、卖出三类中最该先复盘的拖累项。',
        })}
      </div>
      <details class="tradeReviewPatternListFold">
        <summary>
          <strong>查看列表明细</strong>
          <span>只展开三类模式和可归因代表交易</span>
        </summary>
        ${TradeReviewPatternList(modeUniversePatterns, { ...opts, rowLimit: opts.rowLimit || 5 })}
      </details>
    </section>
  `;
}

function TradeReviewDataGaps(workbench, payload, opts = {}) {
  const gaps = workbench?.data_gaps || [];
  const note = payload?.data_quality?.note || workbench?.method_note || '';
  return `
    <section class="tradeReviewDataPanel">
      <div class="tradeReviewDataGrid">
        ${gaps.length ? gaps.map((gap) => `
          <div class="tradeReviewDataGap">
            <span>${escapeHtml(gap.label || gap.kind || '数据缺口')}</span>
            <strong>${escapeHtml(gap.count ?? 0)}</strong>
            <p>${escapeHtml(gap.action || '')}</p>
          </div>
        `).join('') : '<div class="tradeReviewDataGap good"><span>价格覆盖</span><strong>OK</strong><p>当前样本没有阻塞性的价格缺口。</p></div>'}
      </div>
      ${opts.note === false ? '' : `<p class="tradeReviewMethodNote">${escapeHtml(note)}</p>`}
    </section>
  `;
}

function TradeReviewDetailPanels(workbench, payload) {
  return `
    <details class="tradeReviewAnalysisFold">
      <summary>
        <strong>展开模式诊断和数据质量</strong>
        <span>买入/卖出模式、行为标签、数据缺口</span>
      </summary>
      ${TradeReviewPatternPanel(workbench)}
      ${TradeReviewDataGaps(workbench, payload)}
    </details>
  `;
}

function TradeReviewWorkbench(payload) {
  const workbench = payload?.workbench || {};
  return `
    <div class="tradeReviewWorkbench tradeReviewAttributionWorkbench">
      <section class="tradeReviewModeAttribution">
        <header class="tradeReviewModeAttributionHead">
          <div>
            <span>交易归因闭环</span>
            <strong>先看标的真实盈亏，再解释买卖动作</strong>
          </div>
          <small>真实盈亏来自收益归因；动作贡献解释交易时点，未解释差额等待基准归因。</small>
        </header>
        ${TradeReviewAssetHeatmap(payload, workbench)}
        ${TradeReviewDetailPanels(workbench, payload)}
      </section>
    </div>
  `;
}

function TradeAttributionPanelV2(payload) {
  if (!payload) {
    return `
      <div id="tradeAttributionPanel" class="tradeAttributionPanel">
        <div class="muted">正在计算复盘概览...</div>
      </div>
    `;
  }
  return `
    <div id="tradeAttributionPanel" class="tradeAttributionPanel tradeAttributionPanelV2 tradeReviewWorkbenchPanel">
      ${TradeReviewWorkbench(payload)}
    </div>
  `;
}

function renderTradeAttribution(payload) {
  const panel = $('tradeAttributionPanel');
  if (!panel) return;
  panel.outerHTML = TradeAttributionPanelV2(payload);
}

function AutoEvidenceList(metrics) {
  const rows = autoEvidenceRows(metrics);
  if (!rows.length) return '';
  return `
    <div class="tradeAutoEvidenceList">
      ${rows.map((row) => `
        <div>
          <span>${escapeHtml(row.label)}</span>
          <strong class="${escapeHtml(row.tone)}">${escapeHtml(row.value)}</strong>
        </div>
      `).join('')}
    </div>
  `;
}

function AutoMetricDetails(metrics) {
  return `
    <details class="tradeAutoDetailFold">
      <summary>查看指标细节</summary>
      <div class="tradeAutoMetricGrid">
        <div><span>T+1</span><strong class="${pctTone(horizonMetric(metrics, 't1'))}">${escapeHtml(formatPctPoint(horizonMetric(metrics, 't1')))}</strong></div>
        <div><span>T+5</span><strong class="${pctTone(horizonMetric(metrics, 't5'))}">${escapeHtml(formatPctPoint(horizonMetric(metrics, 't5')))}</strong></div>
        <div><span>T+20</span><strong class="${pctTone(horizonMetric(metrics, 't20'))}">${escapeHtml(formatPctPoint(horizonMetric(metrics, 't20')))}</strong></div>
        <div><span>价格点</span><strong>${escapeHtml(String(metrics.points || '--'))}</strong></div>
      </div>
    </details>
  `;
}

function TradeAutoAttributionPanelV3(payload) {
  const metrics = payload?.event_metrics || {};
  const judgement = (payload?.judgements || [])[0] || {};
  const auto = payload?.auto_review || {};
  const tone = autoLevelTone(auto.level || judgement.level);
  if (!payload) {
    return `
      <section id="tradeAutoAttribution" class="tradeAutoAttribution">
        <div class="muted">正在读取系统初判...</div>
      </section>
    `;
  }
  const manualGaps = Array.isArray(auto.manual_gaps) ? auto.manual_gaps : [];
  if (metrics.coverage !== 'ok') {
    return `
      <section id="tradeAutoAttribution" class="tradeAutoAttribution mutedPanel tradeAutoAttribution-${tone}">
        <div class="tradeAutoHead tradeAutoDecisionHead">
          <div>
            <span>系统结论 · ${escapeHtml(autoConfidenceLabel(auto.confidence))}</span>
            <strong>${escapeHtml(auto.label || judgement.label || '数据不足')}</strong>
          </div>
        </div>
        <p class="tradeAutoSummary">${escapeHtml(auto.summary || (metrics.warnings || payload.data_quality?.warnings || ['暂无足够价格数据。'])[0])}</p>
        ${manualGaps.length ? `<div class="tradeAutoManualGaps"><span>待补</span><strong>${escapeHtml(manualGaps.join(' / '))}</strong></div>` : ''}
        <div class="tradeAutoNextStep"><span>下一步</span><strong>${escapeHtml(auto.action || '先补价格或确认标的映射')}</strong></div>
        ${autoSuggestedTagButtons(auto.suggested_tags)}
      </section>
    `;
  }
  return `
    <section id="tradeAutoAttribution" class="tradeAutoAttribution tradeAutoAttribution-${tone}">
      <div class="tradeAutoHead tradeAutoDecisionHead">
        <div>
          <span>系统结论 · ${escapeHtml(autoConfidenceLabel(auto.confidence))}</span>
          <strong>${escapeHtml(auto.label || judgement.label || '观察中')}</strong>
        </div>
        <em class="${pctTone(metrics.current_return_pct)}">${escapeHtml(formatPctPoint(metrics.current_return_pct))}</em>
      </div>
      <p class="tradeAutoSummary">${escapeHtml(auto.summary || judgement.message || '价格表现尚未形成强结论。')}</p>
      ${AutoEvidenceList(metrics)}
      <div class="tradeAutoNextStep"><span>下一步</span><strong>${escapeHtml(auto.action || '人工确认即可')}</strong></div>
      ${manualGaps.length ? `<div class="tradeAutoManualGaps"><span>待补</span><strong>${escapeHtml(manualGaps.join(' / '))}</strong></div>` : ''}
      ${autoSuggestedTagButtons(auto.suggested_tags)}
      ${AutoMetricDetails(metrics)}
    </section>
  `;
}

function renderTradeAutoAttribution(payload) {
  const panel = $('tradeAutoAttribution');
  if (!panel) return;
  panel.outerHTML = TradeAutoAttributionPanelV3(payload);
  const form = $('tradeReviewForm');
  if (form) {
    const auto = payload?.auto_review || {};
    form.dataset.autoReviewLevel = auto.level || '';
    form.dataset.autoReviewNeedsHuman = auto.needs_human_review ? '1' : '0';
    form.dataset.autoReviewKind = auto.kind || '';
    updateTradeReviewCompletion(form);
  }
}

function closeTradeReviewAssetFloat(panel) {
  const root = panel || scope.query('[data-asset-heatmap-panel]');
  if (!root) return;
  root.querySelector('[data-asset-heat-float]')?.setAttribute('hidden', '');
  root.querySelectorAll('[data-asset-heat-select]').forEach((button) => {
    button.classList.remove('active');
    button.setAttribute('aria-pressed', 'false');
  });
  root.querySelectorAll('[data-asset-heat-detail]').forEach((detail) => {
    detail.hidden = true;
  });
  scope.root.classList.remove('trade-review-asset-open');
}

function applyTradeReviewDirectionFilter(panel, filter = 'all') {
  if (!panel) return;
  const limit = Math.max(1, Number(panel.dataset.directionLimit || 10));
  const rows = [...panel.querySelectorAll('.tradeReviewDirectionTrade')];
  let matched = 0;
  let shown = 0;

  rows.forEach((row) => {
    const tone = row.dataset.directionTone || '';
    const side = row.dataset.directionSide || '';
    const match = filter === 'all' || tone === filter || side === filter;
    if (match) matched += 1;
    const visible = match && shown < limit;
    if (visible) shown += 1;
    row.hidden = !visible;
  });

  const countLabel = panel.querySelector('[data-direction-count]');
  if (countLabel) countLabel.textContent = `显示 ${shown} / ${matched} 笔`;
  panel.querySelectorAll('[data-direction-filter-value]').forEach((button) => {
    const active = button.dataset.directionFilterValue === filter;
    button.classList.toggle('active', active);
    button.setAttribute('aria-pressed', active ? 'true' : 'false');
  });
}

return {TradeReviewAssetAttributionBridge, TradeReviewAssetRuleInsights, TradeReviewAssetTradeCard, TradeReviewAssetAttributionDetail, TradeReviewDirectionalTradePanel, TradeReviewAssetHeatmap, TradeReviewPatternHeatmap, TradeReviewPatternList, TradeReviewPatternPanel, TradeReviewDataGaps, TradeReviewDetailPanels, TradeReviewWorkbench, TradeAttributionPanelV2, renderTradeAttribution, AutoEvidenceList, AutoMetricDetails, TradeAutoAttributionPanelV3, renderTradeAutoAttribution, closeTradeReviewAssetFloat, applyTradeReviewDirectionFilter};
}
