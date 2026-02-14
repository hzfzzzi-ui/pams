<script lang="ts">
export default {
  name: 'Dashboard'
}
</script>

<template>
  <div class="dashboard">
    <!-- KPI Cards -->
    <div class="kpi-grid">
      <KPICard
        label="总资产"
        :value="assetStore.totalAssets"
        currency="CNY"
        trend="neutral"
      />
      <KPICard
        label="总负债"
        :value="Math.abs(assetStore.totalLiabilities)"
        currency="CNY"
        trend="neutral"
      />
      <KPICard
        label="净资产"
        :value="assetStore.netAssets"
        currency="CNY"
        :trend="getTrend(assetStore.totalDailyPnl)"
        :change="assetStore.totalDailyPnl"
        change-label="日涨跌"
      />
      <KPICard
        label="最大回撤"
        :value="assetStore.maxDrawdown"
        currency="%"
        :trend="assetStore.maxDrawdown < -0.2 ? 'down' : 'neutral'"
      />
      <KPICard
        label="持仓数"
        :value="assetStore.holdingsCount"
        currency=""
        trend="neutral"
        :precision="0"
      />
    </div>

    <!-- Charts Grid -->
    <div class="charts-grid">
      <!-- Asset Allocation Pie -->
      <div class="chart-col">
        <ChartCard title="资产结构" height="350px" :config="assetPieConfig" />
      </div>

      <!-- Liability Pie -->
      <div class="chart-col">
        <ChartCard title="负债结构" height="350px" :config="liabilityPieConfig" />
      </div>

      <!-- NAV Series Line -->
      <div class="chart-col-full">
        <ChartCard title="净资产曲线 (近90天)" height="400px" :config="navLineConfig" />
      </div>

      <!-- Cashflow Bar -->
      <div class="chart-col-full">
        <ChartCard title="近30天收支" height="350px" :config="cashflowBarConfig" />
      </div>
    </div>

    <!-- Risk & Status -->
    <div class="status-grid">
      <!-- Risk Events -->
      <div class="status-card">
        <h3 class="status-title">
          <el-icon><WarningFilled /></el-icon>
          <span>风险告警</span>
        </h3>
        <div v-if="assetStore.riskEvents.length === 0" class="empty-state">
          <el-icon><SuccessFilled /></el-icon>
          <span>暂无风险事件</span>
        </div>
        <div v-else class="risk-list">
          <div
            v-for="event in assetStore.riskEvents.slice(0, 5)"
            :key="event.code"
            :class="['risk-item', `severity-${event.severity}`]"
          >
            <div class="risk-header">
              <el-tag :type="getSeverityType(event.severity)" size="small">
                {{ event.severity.toUpperCase() }}
              </el-tag>
              <span class="risk-title">{{ event.title }}</span>
            </div>
            <div class="risk-details">{{ event.details }}</div>
          </div>
        </div>
      </div>

      <!-- System Status -->
      <div class="status-card">
        <h3 class="status-title">
          <el-icon><Monitor /></el-icon>
          <span>系统状态</span>
        </h3>
        <div class="status-list">
          <div class="status-item">
            <span class="status-label">API连接</span>
            <el-tag :type="assetStore.error ? 'danger' : 'success'" size="small">
              {{ assetStore.error ? '异常' : '正常' }}
            </el-tag>
          </div>
          <div class="status-item">
            <span class="status-label">数据时间</span>
            <span class="status-value">
              {{ assetStore.summary ? formatDate(assetStore.summary.as_of) : '-' }}
            </span>
          </div>
          <div class="status-item">
            <span class="status-label">持仓条目</span>
            <span class="status-value">{{ assetStore.holdingsCount }}</span>
          </div>
          <div class="status-item">
            <span class="status-label">基准币种</span>
            <span class="status-value">{{ assetStore.summary?.base_currency || 'CNY' }}</span>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { WarningFilled, SuccessFilled, Monitor } from '@element-plus/icons-vue'
import { useAssetStore } from '@/stores/asset'
import KPICard from '@/components/KPICard.vue'
import ChartCard from '@/components/ChartCard.vue'
import { formatDate, getTrend } from '@/utils/format'
import type { ChartConfiguration } from 'chart.js'

const assetStore = useAssetStore()

// 中文标签映射
const assetClassLabels: Record<string, string> = {
  'CN_EQUITY': 'A股权益',
  'CN_FUND': '国内基金',
  'CN_PRIVATE': '非上市股权',
  'US_EQUITY': '美股权益',
  'US_FUND': '美国基金',
  'CRYPTO': '加密货币',
  'CASH_MM': '现金货基',
  'FIXED_INCOME': '固定收益',
  'ESOP': '员工期权',
  'GOLD_COMMODITIES': '黄金商品',
  'WEALTH': '理财产品',
  'HPF': '住房公积金',
  'METAL': '贵金属',
  'RECEIVABLE': '应收款项',
  'US_CASH': '美元现金',
  'LIABILITY': '负债'
}

function getAssetClassLabel(assetClass: string): string {
  return assetClassLabels[assetClass] || assetClass
}

// Asset Pie Chart
const assetPieConfig = computed<ChartConfiguration>(() => ({
  type: 'pie',
  data: {
    labels: assetStore.assetClasses.map(c => getAssetClassLabel(c.asset_class)),
    datasets: [{
      data: assetStore.assetClasses.map(c => c.market_value),
      backgroundColor: [
        '#6aa6ff', '#ff6b9d', '#ffd93d', '#6bcf7f',
        '#c084fc', '#fb923c', '#38bdf8', '#f87171'
      ]
    }]
  },
  options: {
    plugins: {
      legend: {
        position: 'right'
      },
      tooltip: {
        callbacks: {
          label: (context) => {
            const value = context.parsed
            const total = context.dataset.data.reduce((a: number, b: number) => a + b, 0)
            const percent = ((value / total) * 100).toFixed(1)
            return `${context.label}: ¥${(value / 10000).toFixed(2)}万 (${percent}%)`
          }
        }
      }
    }
  }
}))

// Liability Pie Chart
const liabilityPieConfig = computed<ChartConfiguration>(() => ({
  type: 'pie',
  data: {
    labels: assetStore.liabilities.map(l => l.name || l.account || getAssetClassLabel(l.asset_class)),
    datasets: [{
      data: assetStore.liabilities.map(l => Math.abs(Number(l.market_value))),
      backgroundColor: [
        '#f87171', '#fb923c', '#fbbf24', '#facc15'
      ]
    }]
  },
  options: {
    plugins: {
      legend: {
        position: 'right'
      },
      tooltip: {
        callbacks: {
          label: (context) => {
            const value = context.parsed
            return `${context.label}: ¥${(value / 10000).toFixed(2)}万`
          }
        }
      }
    }
  }
}))

// NAV Line Chart
const navLineConfig = computed<ChartConfiguration>(() => ({
  type: 'line',
  data: {
    labels: assetStore.navSeries.map(n => formatDate(n.as_of, 'MM-DD')),
    datasets: [{
      label: '净资产',
      data: assetStore.navSeries.map(n => n.total_market_value),
      borderColor: '#6aa6ff',
      backgroundColor: 'rgba(106, 166, 255, 0.1)',
      fill: true,
      tension: 0.4
    }]
  },
  options: {
    plugins: {
      legend: {
        display: false
      },
      tooltip: {
        callbacks: {
          label: (context) => {
            return `净资产: ¥${(context.parsed.y / 10000).toFixed(2)}万`
          }
        }
      }
    },
    scales: {
      y: {
        ticks: {
          callback: (value) => `¥${(Number(value) / 10000).toFixed(0)}万`
        }
      }
    }
  }
}))

// Cashflow Bar Chart
const cashflowBarConfig = computed<ChartConfiguration>(() => ({
  type: 'bar',
  data: {
    labels: assetStore.cashflowSeries.map(c => formatDate(c.date, 'MM-DD')),
    datasets: [
      {
        label: '收入',
        data: assetStore.cashflowSeries.map(c => c.income),
        backgroundColor: '#67c23a'
      },
      {
        label: '支出',
        data: assetStore.cashflowSeries.map(c => Math.abs(c.expense)),
        backgroundColor: '#f56c6c'
      }
    ]
  },
  options: {
    plugins: {
      tooltip: {
        callbacks: {
          label: (context) => {
            return `${context.dataset.label}: ¥${context.parsed.y.toFixed(2)}`
          }
        }
      }
    },
    scales: {
      y: {
        ticks: {
          callback: (value) => `¥${Number(value).toFixed(0)}`
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
  // 每次进入都刷新（依赖store的5分钟缓存机制）
  await assetStore.refreshAll()
})
</script>

<style scoped lang="scss">
@import '@/styles/variables.scss';

.dashboard {
  display: flex;
  flex-direction: column;
  gap: $spacing-lg;
}

.kpi-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: $spacing-md;
}

.charts-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: $spacing-md;
}

.chart-col {
  grid-column: span 1;
}

.chart-col-full {
  grid-column: span 2;
}

.status-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: $spacing-md;
}

.status-card {
  background-color: $bg-card;
  border-radius: $border-radius-base;
  padding: $spacing-md;
  box-shadow: $shadow-card;
}

.status-title {
  display: flex;
  align-items: center;
  gap: $spacing-sm;
  font-size: $font-size-lg;
  font-weight: 600;
  color: $text-primary;
  margin: 0 0 $spacing-md 0;
}

.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: $spacing-xl 0;
  color: $text-secondary;
  gap: $spacing-sm;

  .el-icon {
    font-size: 48px;
    color: $color-success;
  }
}

.risk-list {
  display: flex;
  flex-direction: column;
  gap: $spacing-sm;
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
}

.risk-details {
  font-size: $font-size-sm;
  color: $text-secondary;
}

.status-list {
  display: flex;
  flex-direction: column;
  gap: $spacing-md;
}

.status-item {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.status-label {
  font-size: $font-size-sm;
  color: $text-secondary;
}

.status-value {
  font-size: $font-size-sm;
  color: $text-primary;
  font-weight: 600;
}

// Responsive
@media (max-width: $breakpoint-md) {
  .charts-grid,
  .status-grid {
    grid-template-columns: 1fr;
  }

  .chart-col-full {
    grid-column: span 1;
  }
}
</style>
