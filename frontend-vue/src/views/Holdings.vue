<script lang="ts">
export default {
  name: 'Holdings'
}
</script>

<template>
  <div class="holdings">
    <div class="page-header">
      <div>
        <h2>持仓明细</h2>
        <p class="page-subtitle">
          最新快照：{{ assetStore.summary?.as_of ? formatDate(assetStore.summary.as_of) : '--' }}
        </p>
      </div>
      <div class="header-actions">
        <el-select
          v-model="selectedType"
          placeholder="全部类型"
          clearable
          size="small"
          class="type-filter"
        >
          <el-option label="全部类型" value="" />
          <el-option
            v-for="type in typeOptions"
            :key="type.value"
            :label="type.label"
            :value="type.value"
          />
        </el-select>
      </div>
    </div>

    <div class="holdings-card">
      <el-table
        :data="filteredHoldings"
        :loading="assetStore.loading"
        stripe
        style="width: 100%"
        :max-height="600"
        :default-sort="{ prop: 'market_value', order: 'descending' }"
      >
        <el-table-column
          prop="venue"
          label="平台"
          width="140"
          :formatter="(row) => venueLabel(row.venue)"
        />
        <el-table-column
          prop="account"
          label="账户"
          width="120"
          :formatter="(row) => accountLabel(row.account)"
        />
        <el-table-column
          prop="type"
          label="类型"
          width="100"
          :formatter="(row) => typeLabel(row.type)"
        />
        <el-table-column
          prop="symbol"
          label="代码"
          width="100"
        >
          <template #default="{ row }">
            <span class="mono">{{ row.symbol || '--' }}</span>
          </template>
        </el-table-column>
        <el-table-column
          prop="name"
          label="名称"
          min-width="200"
          show-overflow-tooltip
        />
        <el-table-column
          prop="currency"
          label="币种"
          width="80"
          align="center"
        />
        <el-table-column
          prop="market_value"
          label="市值"
          width="130"
          align="right"
          sortable
        >
          <template #default="{ row }">
            <span class="mono">{{ formatCurrency(row.market_value, row.currency) }}</span>
          </template>
        </el-table-column>
        <el-table-column
          prop="holding_pnl"
          label="持仓收益"
          width="130"
          align="right"
          sortable
        >
          <template #default="{ row }">
            <span :class="['mono', getPnlClass(row.holding_pnl)]">
              {{ formatPnl(row.holding_pnl, row.currency) }}
            </span>
          </template>
        </el-table-column>
        <el-table-column
          prop="daily_pnl"
          label="昨日收益"
          width="130"
          align="right"
          sortable
        >
          <template #default="{ row }">
            <span :class="['mono', getPnlClass(row.daily_pnl)]">
              {{ formatPnl(row.daily_pnl, row.currency) }}
            </span>
          </template>
        </el-table-column>
      </el-table>

      <div class="table-footer">
        <div class="table-stats">
          <span>共 {{ filteredHoldings.length }} 条记录</span>
          <span class="separator">|</span>
          <span>总市值：{{ formatCurrency(totalMarketValue, 'CNY') }}</span>
          <span class="separator">|</span>
          <span :class="getPnlClass(totalHoldingPnl)">
            持仓收益：{{ formatPnl(totalHoldingPnl, 'CNY') }}
          </span>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useAssetStore } from '@/stores/asset'
import { formatDate } from '@/utils/format'
import type { Holding } from '@/types'
import { Filter, ArrowDown, Grid, Document } from '@element-plus/icons-vue'

const assetStore = useAssetStore()
const selectedType = ref<string>('')

// Type options for filter
const typeOptions = computed(() => {
  const types = new Set(assetStore.holdings.map(h => h.type).filter(Boolean))
  return Array.from(types).sort().map(type => ({
    value: type,
    label: typeLabel(type)
  }))
})

// Filtered holdings
const filteredHoldings = computed(() => {
  if (!selectedType.value) {
    return assetStore.holdings
  }
  return assetStore.holdings.filter(h => h.type === selectedType.value)
})

// Calculate totals
const totalMarketValue = computed(() => {
  return filteredHoldings.value.reduce((sum, h) => sum + (h.market_value || 0), 0)
})

const totalHoldingPnl = computed(() => {
  return filteredHoldings.value.reduce((sum, h) => sum + (h.holding_pnl || 0), 0)
})

// Label mapping functions
function venueLabel(venue: string): string {
  const labels: Record<string, string> = {
    'ibkr': '盈透证券',
    'IBKR': '盈透证券',
    'binance': '币安',
    'BINANCE': '币安',
    'guosen': '国信证券',
    'tencent_licaitong': '腾讯理财通',
    'manual': '手动录入',
    'CN': '国内券商',
    'CCB': '建设银行',
    'LICAITONG': '理财通',
    'HUAWEI': '华为'
  }
  return labels[venue] || labels[venue.toUpperCase()] || venue
}

function accountLabel(account: string): string {
  const labels: Record<string, string> = {
    'huawei_emp': '华为员工',
    'santander4209': 'Santander 4209',
    'cn_a_rmb': 'A股账户',
    'cn_hpf': '公积金',
    'recv_loan': '借款应收',
    'ccb_wm47': '建行理财47',
    'main': '主账户',
    'recv_reimburse': '待报销',
    'ccb_gold': '建行黄金',
    'us_main': '美股账户',
    'tencent_mm': '理财通',
    'wechat_mm': '微信零钱通',
    'jianhang7332': '建行7332',
    'guangfa_cc_7443': '广发7443',
    'guangfa_cc_9711': '广发9711',
    'ccb_cc_0453': '建行0453'
  }
  return labels[account] || account
}

function typeLabel(type: string): string {
  const labels: Record<string, string> = {
    'stock': '股票',
    'stock_cn': 'A股',
    'fund': '基金',
    'etf': 'ETF',
    'crypto': '加密货币',
    'cash': '现金',
    'fixed_income': '固收',
    'esop': '期权',
    'equity_private': '非上市股权',
    'asset_hpf': '住房公积金',
    'receivable_loan': '借款应收',
    'receivable_reimburse': '待报销',
    'wealth': '理财产品',
    'gold': '黄金',
    'liability': '负债',
    'liability_cc': '信用卡'
  }
  return labels[type] || type
}

function formatCurrency(value: number, currency: string): string {
  const absValue = Math.abs(value)
  const sign = value < 0 ? '-' : ''

  let formatted = ''
  if (absValue >= 1e8) {
    formatted = `${(absValue / 1e8).toFixed(2)}亿`
  } else if (absValue >= 1e4) {
    formatted = `${(absValue / 1e4).toFixed(2)}万`
  } else {
    formatted = absValue.toFixed(2)
  }

  const symbol = currency === 'USD' ? '$' : currency === 'CNY' ? '¥' : ''
  return `${sign}${symbol}${formatted}`
}

function formatPnl(value: number, currency: string): string {
  if (value === 0) return '--'
  const prefix = value > 0 ? '+' : ''
  return prefix + formatCurrency(value, currency)
}

function getPnlClass(value: number): string {
  if (value > 0) return 'text-up'
  if (value < 0) return 'text-down'
  return ''
}

onMounted(async () => {
  // 每次进入都刷新
  await assetStore.fetchHoldings()
})
</script>

<style scoped lang="scss">
@import '@/styles/variables.scss';

.holdings {
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
.type-filter {
  width: 160px;

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

.holdings-card {
  background-color: $bg-card;
  border-radius: $border-radius-base;
  padding: $spacing-md;
  box-shadow: $shadow-card;
}

.mono {
  font-family: 'SF Mono', Monaco, 'Cascadia Code', 'Roboto Mono', Consolas, monospace;
  font-size: $font-size-sm;
}

.table-footer {
  margin-top: $spacing-md;
  padding-top: $spacing-md;
  border-top: 1px solid $border-color;
}

.table-stats {
  display: flex;
  gap: $spacing-md;
  align-items: center;
  font-size: $font-size-sm;
  color: $text-secondary;

  .separator {
    color: $border-color;
  }
}

// Override Element Plus table styles for dark theme
:deep(.el-table) {
  --el-table-bg-color: #{$bg-card};
  --el-table-tr-bg-color: #{$bg-card};
  --el-table-row-hover-bg-color: #{$bg-hover};
  --el-table-header-bg-color: #{$bg-input};
  --el-table-header-text-color: #{$text-primary};
  --el-table-text-color: #{$text-primary};
  --el-table-border-color: #{$border-color};

  .el-table__header th {
    font-weight: 600;
  }

  .el-table__row--striped {
    --el-table-tr-bg-color: rgba(42, 51, 68, 0.3);
  }
}
</style>
