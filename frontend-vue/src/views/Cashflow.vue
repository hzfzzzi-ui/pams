<script lang="ts">
export default {
  name: 'Cashflow'
}
</script>

<template>
  <div class="cashflow">
    <div class="page-header">
      <div>
        <h2>收支分析</h2>
        <p class="page-subtitle">
          按日汇总：收入 / 支出 / 净额（对接 Firefly 后自动更新）
        </p>
      </div>
      <div class="header-actions">
        <el-select
          v-model="limitDays"
          size="small"
          class="days-filter"
        >
          <el-option label="近30天" :value="30" />
          <el-option label="近60天" :value="60" />
          <el-option label="近90天" :value="90" />
          <el-option label="近180天" :value="180" />
        </el-select>
      </div>
    </div>

    <div class="cashflow-card">
      <div class="stats-grid">
        <div class="stat-item">
          <div class="stat-label">总收入</div>
          <div class="stat-value text-up">{{ formatCurrency(totalIncome, 'CNY') }}</div>
        </div>
        <div class="stat-item">
          <div class="stat-label">总支出</div>
          <div class="stat-value text-down">{{ formatCurrency(Math.abs(totalExpense), 'CNY') }}</div>
        </div>
        <div class="stat-item">
          <div class="stat-label">净额</div>
          <div :class="['stat-value', netAmount >= 0 ? 'text-up' : 'text-down']">
            {{ formatCurrency(netAmount, 'CNY') }}
          </div>
        </div>
        <div class="stat-item">
          <div class="stat-label">数据范围</div>
          <div class="stat-value stat-range">
            {{ dateRange }}
          </div>
        </div>
      </div>

      <ChartCard
        title="收支曲线"
        height="400px"
        :config="cashflowChartConfig"
      >
        <template #footer>
          <div v-if="assetStore.cashflowSeries.length === 0" class="empty-note">
            暂无收支数据（等待 Firefly 对接流水入库）
          </div>
          <div v-else class="chart-note">
            CNY 口径 · 区间：{{ dateRange }}
          </div>
        </template>
      </ChartCard>
    </div>

    <!-- Future: Monthly breakdown table -->
    <div class="cashflow-card">
      <h3 class="section-title">按月汇总</h3>
      <div class="placeholder-content">
        <el-icon size="48"><DocumentAdd /></el-icon>
        <p>此功能待开发</p>
        <p class="text-muted">将显示按月汇总的收支数据和趋势分析</p>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, onMounted } from 'vue'
import { DocumentAdd, Calendar, Clock, ArrowDown } from '@element-plus/icons-vue'
import { useAssetStore } from '@/stores/asset'
import ChartCard from '@/components/ChartCard.vue'
import { formatDate, formatCurrency } from '@/utils/format'
import type { ChartConfiguration } from 'chart.js'

const assetStore = useAssetStore()
const limitDays = ref(90)

// Computed stats
const totalIncome = computed(() => {
  return assetStore.cashflowSeries.reduce((sum, item) => sum + (item.income || 0), 0)
})

const totalExpense = computed(() => {
  return assetStore.cashflowSeries.reduce((sum, item) => sum + (item.expense || 0), 0)
})

const netAmount = computed(() => {
  return assetStore.cashflowSeries.reduce((sum, item) => sum + (item.net || 0), 0)
})

const dateRange = computed(() => {
  if (assetStore.cashflowSeries.length === 0) return '--'
  const first = assetStore.cashflowSeries[0]?.date
  const last = assetStore.cashflowSeries[assetStore.cashflowSeries.length - 1]?.date
  return `${first} ~ ${last}`
})

// Chart configuration
const cashflowChartConfig = computed<ChartConfiguration>(() => ({
  data: {
    labels: assetStore.cashflowSeries.map(item => formatDate(item.date, 'MM-DD')),
    datasets: [
      {
        type: 'bar',
        label: '收入',
        data: assetStore.cashflowSeries.map(item => item.income || 0),
        backgroundColor: 'rgba(67, 209, 158, 0.35)',
        borderColor: '#43d19e',
        borderWidth: 1,
        yAxisID: 'y'
      },
      {
        type: 'bar',
        label: '支出',
        data: assetStore.cashflowSeries.map(item => -Math.abs(item.expense || 0)),
        backgroundColor: 'rgba(255, 107, 107, 0.28)',
        borderColor: '#ff6b6b',
        borderWidth: 1,
        yAxisID: 'y'
      },
      {
        type: 'line',
        label: '净额',
        data: assetStore.cashflowSeries.map(item => item.net || 0),
        borderColor: '#6aa6ff',
        backgroundColor: 'rgba(106, 166, 255, 0.15)',
        tension: 0.25,
        fill: true,
        yAxisID: 'y'
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
      },
      tooltip: {
        callbacks: {
          label: (context) => {
            const value = context.parsed.y
            return `${context.dataset.label}: ¥${value.toFixed(2)}`
          }
        }
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
        ticks: {
          callback: (value) => {
            const num = Number(value)
            if (num >= 10000) return `¥${(num / 10000).toFixed(1)}万`
            if (num <= -10000) return `-¥${(Math.abs(num) / 10000).toFixed(1)}万`
            return `¥${num.toFixed(0)}`
          }
        }
      }
    }
  }
}))

// Watch limit change
watch(limitDays, async (newLimit) => {
  await assetStore.fetchCashflowSeries(newLimit, 'CNY')
})

onMounted(async () => {
  // 每次进入都刷新
  await assetStore.fetchCashflowSeries(limitDays.value, 'CNY')
})
</script>

<style scoped lang="scss">
@import '@/styles/variables.scss';

.cashflow {
  display: flex;
  flex-direction: column;
  gap: $spacing-lg;
}

.page-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;

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

.header-actions {
  display: flex;
  gap: $spacing-md;
  align-items: center;
}

// 筛选器样式
.days-filter {
  width: 120px;

  :deep(.el-input__wrapper) {
    height: 32px;
    background-color: rgba(255, 255, 255, 0.04);
    border: 1px solid rgba(255, 255, 255, 0.1);
    box-shadow: none;
    padding: 0 10px;

    &:hover {
      border-color: rgba(106, 166, 255, 0.4);
    }

    .el-input__inner {
      color: $text-primary;
      font-size: 14px;
      height: 30px;
      line-height: 30px;
    }

    .el-input__suffix {
      .el-icon {
        font-size: 16px;
      }
    }
  }
}

// 下拉菜单
:deep(.el-select-dropdown) {
  background-color: $bg-card;
  border: 1px solid rgba(255, 255, 255, 0.1);
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.3);

  .el-select-dropdown__item {
    color: $text-primary;
    font-size: 13px;
    height: 32px;
    line-height: 32px;
    padding: 0 12px;

    &:hover {
      background-color: rgba(106, 166, 255, 0.1);
    }

    &.is-selected {
      color: $color-primary;
      background-color: rgba(106, 166, 255, 0.15);
    }
  }
}

.cashflow-card {
  background-color: $bg-card;
  border-radius: $border-radius-base;
  padding: $spacing-md;
  box-shadow: $shadow-card;
}

.stats-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: $spacing-md;
  margin-bottom: $spacing-lg;
}

.stat-item {
  text-align: center;
  padding: $spacing-md;
  background-color: $bg-input;
  border-radius: $border-radius-small;
}

.stat-label {
  font-size: $font-size-sm;
  color: $text-secondary;
  margin-bottom: $spacing-xs;
}

.stat-value {
  font-size: $font-size-xl;
  font-weight: 700;
  color: $text-primary;

  &.stat-range {
    font-size: $font-size-sm;
    font-weight: 400;
  }
}

.section-title {
  font-size: $font-size-lg;
  font-weight: 600;
  color: $text-primary;
  margin: 0 0 $spacing-md 0;
}

.chart-note {
  font-size: $font-size-sm;
  color: $text-secondary;
  text-align: center;
}

.empty-note {
  font-size: $font-size-sm;
  color: $color-warning;
  text-align: center;
}

.placeholder-content {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: $spacing-xxl;
  color: $text-secondary;

  .el-icon {
    color: $text-disabled;
    margin-bottom: $spacing-md;
  }

  p {
    margin: $spacing-xs 0;
  }

  .text-muted {
    font-size: $font-size-sm;
    color: $text-disabled;
  }
}

// Responsive
@media (max-width: $breakpoint-md) {
  .stats-grid {
    grid-template-columns: repeat(2, 1fr);
  }
}

@media (max-width: $breakpoint-sm) {
  .stats-grid {
    grid-template-columns: 1fr;
  }
}
</style>
