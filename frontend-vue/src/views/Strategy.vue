<script lang="ts">
export default {
  name: 'Strategy'
}
</script>

<template>
  <div class="strategy">
    <div class="page-header">
      <div>
        <h2>投资策略 / IPS</h2>
        <p class="page-subtitle">
          这里承载决策树、IPS 参数、风险事件、回撤曲线等
        </p>
      </div>
    </div>

    <div class="grid-container">
      <!-- IPS Parameters -->
      <div class="strategy-card">
        <h3 class="card-title">IPS v1</h3>
        <p class="card-subtitle">你的投资章程（硬约束）</p>

        <div class="ips-grid" v-if="assetStore.ips">
          <div class="ips-item">
            <div class="ips-label">持仓数量目标</div>
            <div class="ips-value">
              {{ assetStore.ips.maxPositions?.core || 10 }}–{{ assetStore.ips.maxPositions?.satellite || 5 }}
            </div>
            <div class="ips-hint">核心仓+卫星仓</div>
          </div>

          <div class="ips-item">
            <div class="ips-label">组合最大回撤</div>
            <div class="ips-value">{{ Math.round((assetStore.ips.maxDrawdown || -0.25) * 100) }}%</div>
            <div class="ips-hint">触发后生成降风险草案</div>
          </div>

          <div class="ips-item">
            <div class="ips-label">行业上限</div>
            <div class="ips-value">{{ Math.round((assetStore.ips.sectorLimit || 0.4) * 100) }}%</div>
            <div class="ips-hint">目前行业先用type代理</div>
          </div>

          <div class="ips-item">
            <div class="ips-label">再平衡带宽</div>
            <div class="ips-value">{{ Math.round((assetStore.ips.rebalanceBand || 0.1) * 100) }}%</div>
            <div class="ips-hint">偏离触发再平衡草案</div>
          </div>

          <div class="ips-item">
            <div class="ips-label">提醒模式</div>
            <div class="ips-value">{{ assetStore.ips.alertMode || 'A2' }}</div>
            <div class="ips-hint">重大新闻提醒</div>
          </div>

          <div class="ips-item">
            <div class="ips-label">交易执行</div>
            <div class="ips-value">
              {{ assetStore.ips.executionRequiresApproval ? '需确认' : '自动' }}
            </div>
            <div class="ips-hint">股东确认后执行</div>
          </div>
        </div>

        <el-divider />

        <h3 class="card-title">策略 v1（摘要）</h3>
        <p class="strategy-summary">
          核心：质量现金流价值 + 分批建仓；风险：-25% 组合回撤、行业≤40%、再平衡带宽10%。
          交易：仅生成草案，必须股东确认才执行。
        </p>
      </div>

      <!-- NAV / Drawdown Chart -->
      <div class="strategy-card">
        <h3 class="card-title">NAV / 回撤</h3>
        <p class="card-subtitle">scope 不全会失真（会提示）</p>

        <ChartCard
          title=""
          height="350px"
          :config="drawdownChartConfig"
        >
          <template #footer>
            <div v-if="hasIncompleteData" class="warning-note">
              ⚠️ 提示：当前数据覆盖不完整（completeness!=ok），回撤曲线可能失真。
            </div>
            <div v-else class="success-note">
              ✓ 数据覆盖正常
            </div>
          </template>
        </ChartCard>
      </div>

      <!-- Risk Events -->
      <div class="strategy-card">
        <h3 class="card-title">风险事件（最新）</h3>
        <p class="card-subtitle">按严重度排序</p>

        <div v-if="assetStore.riskEvents.length === 0" class="empty-state">
          <el-icon size="48"><SuccessFilled /></el-icon>
          <p>暂无风险事件</p>
        </div>
        <div v-else class="risk-list">
          <div
            v-for="event in assetStore.riskEvents"
            :key="event.code"
            :class="['risk-item', `severity-${event.severity}`]"
          >
            <div class="risk-header">
              <el-tag :type="getSeverityType(event.severity)" size="small">
                {{ event.severity.toUpperCase() }}
              </el-tag>
              <span class="risk-title">{{ event.title }}</span>
            </div>
            <div class="risk-code">{{ event.code }}</div>
            <div class="risk-details">{{ event.details }}</div>
          </div>
        </div>
      </div>

      <!-- Debug JSON -->
      <div class="strategy-card">
        <h3 class="card-title">机器可读（调试）</h3>
        <p class="card-subtitle">后续可隐藏</p>

        <el-collapse>
          <el-collapse-item title="展开 IPS JSON" name="1">
            <pre class="json-pre">{{ JSON.stringify(assetStore.ips, null, 2) }}</pre>
          </el-collapse-item>
          <el-collapse-item title="展开 Drawdown JSON" name="2">
            <pre class="json-pre">{{ JSON.stringify(assetStore.drawdownSeries, null, 2) }}</pre>
          </el-collapse-item>
        </el-collapse>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { SuccessFilled } from '@element-plus/icons-vue'
import { useAssetStore } from '@/stores/asset'
import ChartCard from '@/components/ChartCard.vue'
import type { ChartConfiguration } from 'chart.js'

const assetStore = useAssetStore()

// Check for incomplete data
const hasIncompleteData = computed(() => {
  return assetStore.drawdownSeries.some((d: any) =>
    d.completeness && d.completeness !== 'ok'
  )
})

// Drawdown chart config
const drawdownChartConfig = computed<ChartConfiguration>(() => ({
  data: {
    labels: assetStore.drawdownSeries.map((d: any) => d.as_of),
    datasets: [
      {
        type: 'line',
        label: '总市值',
        data: assetStore.drawdownSeries.map((d: any) => d.total_market_value || 0),
        yAxisID: 'y',
        borderColor: '#6aa6ff',
        backgroundColor: 'rgba(106, 166, 255, 0.15)',
        tension: 0.25,
        fill: true
      },
      {
        type: 'line',
        label: '回撤(%)',
        data: assetStore.drawdownSeries.map(d => (d.drawdown_pct || 0) * 100),
        yAxisID: 'y1',
        borderColor: '#ff6b6b',
        backgroundColor: 'rgba(255, 107, 107, 0.12)',
        tension: 0.25,
        fill: true
      }
    ]
  },
  options: {
    responsive: true,
    maintainAspectRatio: false,
    interaction: {
      mode: 'index',
      intersect: false
    },
    plugins: {
      legend: {
        display: true,
        position: 'top'
      }
    },
    scales: {
      x: {
        ticks: {
          maxRotation: 0,
          autoSkip: true
        }
      },
      y: {
        position: 'left',
        title: {
          display: true,
          text: '总市值'
        },
        ticks: {
          callback: (value) => {
            const num = Number(value)
            if (num >= 1e8) return `¥${(num / 1e8).toFixed(1)}亿`
            if (num >= 1e4) return `¥${(num / 1e4).toFixed(0)}万`
            return `¥${num.toFixed(0)}`
          }
        }
      },
      y1: {
        position: 'right',
        title: {
          display: true,
          text: '回撤%'
        },
        grid: {
          drawOnChartArea: false
        },
        ticks: {
          callback: (value) => `${Number(value).toFixed(1)}%`
        }
      }
    }
  }
}))

function getSeverityType(severity: string): 'success' | 'warning' | 'danger' | 'info' {
  switch (severity) {
    case 'critical':
      return 'danger'
    case 'warn':
      return 'warning'
    case 'info':
      return 'info'
    default:
      return 'info'
  }
}

onMounted(async () => {
  // 每次进入都刷新
  await Promise.all([
    assetStore.fetchIPS(),
    assetStore.fetchDrawdownSeries(),
    assetStore.fetchRiskEvents()
  ])
})
</script>

<style scoped lang="scss">
@import '@/styles/variables.scss';

.strategy {
  display: flex;
  flex-direction: column;
  gap: $spacing-lg;
}

.page-header {
  h2 {
    font-size: $font-size-xl;
    font-weight: 600;
    color: $text-primary;
    margin: 0 0 $spacing-xs 0;
  }

  .page-subtitle {
    font-size: $font-size-sm;
    color: $text-secondary;
    margin: 0;
  }
}

.grid-container {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: $spacing-md;
}

.strategy-card {
  background-color: $bg-card;
  border-radius: $border-radius-base;
  padding: $spacing-md;
  box-shadow: $shadow-card;
}

.card-title {
  font-size: $font-size-lg;
  font-weight: 600;
  color: $text-primary;
  margin: 0 0 $spacing-xs 0;
}

.card-subtitle {
  font-size: $font-size-sm;
  color: $text-secondary;
  margin: 0 0 $spacing-md 0;
}

.ips-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: $spacing-md;
  margin-bottom: $spacing-md;
}

.ips-item {
  padding: $spacing-md;
  background-color: $bg-input;
  border-radius: $border-radius-small;
}

.ips-label {
  font-size: $font-size-sm;
  color: $text-secondary;
  margin-bottom: $spacing-xs;
}

.ips-value {
  font-size: $font-size-xl;
  font-weight: 700;
  color: $color-primary;
  margin-bottom: $spacing-xs;
}

.ips-hint {
  font-size: $font-size-xs;
  color: $text-disabled;
}

.strategy-summary {
  font-size: $font-size-sm;
  color: $text-secondary;
  line-height: 1.6;
  margin: 0;
}

.warning-note {
  font-size: $font-size-sm;
  color: $color-warning;
  text-align: center;
  padding: $spacing-sm;
  background-color: rgba($color-warning, 0.1);
  border-radius: $border-radius-small;
}

.success-note {
  font-size: $font-size-sm;
  color: $color-success;
  text-align: center;
}

.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: $spacing-xl;
  color: $text-secondary;

  .el-icon {
    color: $color-success;
    margin-bottom: $spacing-sm;
  }
}

.risk-list {
  display: flex;
  flex-direction: column;
  gap: $spacing-sm;
  max-height: 400px;
  overflow-y: auto;
}

.risk-item {
  padding: $spacing-sm;
  background-color: $bg-input;
  border-radius: $border-radius-small;
  border-left: 3px solid transparent;

  &.severity-critical {
    border-left-color: $color-danger;
  }

  &.severity-warn {
    border-left-color: $color-warning;
  }

  &.severity-info {
    border-left-color: $color-info;
  }
}

.risk-header {
  display: flex;
  align-items: center;
  gap: $spacing-sm;
  margin-bottom: $spacing-xs;
}

.risk-title {
  font-weight: 600;
  color: $text-primary;
  font-size: $font-size-sm;
}

.risk-code {
  font-size: $font-size-xs;
  color: $text-disabled;
  font-family: monospace;
  margin-bottom: $spacing-xs;
}

.risk-details {
  font-size: $font-size-sm;
  color: $text-secondary;
}

.json-pre {
  background-color: $bg-base;
  padding: $spacing-sm;
  border-radius: $border-radius-small;
  font-size: $font-size-xs;
  color: $text-secondary;
  overflow-x: auto;
  margin: 0;
  font-family: 'SF Mono', Monaco, 'Cascadia Code', monospace;
}

// Override Element Plus collapse styles
:deep(.el-collapse) {
  border: none;

  .el-collapse-item__header {
    background-color: transparent;
    color: $text-secondary;
    border-color: $border-color;
  }

  .el-collapse-item__wrap {
    background-color: transparent;
    border-color: $border-color;
  }

  .el-collapse-item__content {
    padding: $spacing-md 0;
  }
}

// Responsive
@media (max-width: $breakpoint-md) {
  .grid-container {
    grid-template-columns: 1fr;
  }

  .ips-grid {
    grid-template-columns: 1fr;
  }
}
</style>
