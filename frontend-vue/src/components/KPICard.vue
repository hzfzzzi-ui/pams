<template>
  <div class="kpi-card">
    <div class="kpi-header">
      <span class="kpi-label">{{ label }}</span>
      <el-icon v-if="trend" :class="['kpi-trend-icon', `trend-${trend}`]">
        <component :is="trendIcon" />
      </el-icon>
    </div>

    <div class="kpi-content">
      <div class="kpi-value" :class="valueClass">
        {{ displayValue }}
      </div>
      <div v-if="subtitle" class="kpi-subtitle">
        {{ subtitle }}
      </div>
    </div>

    <div v-if="change !== undefined" class="kpi-footer">
      <span :class="['kpi-change', changeClass]">
        {{ changePrefix }}{{ formatChange(change) }}
      </span>
      <span v-if="changeLabel" class="kpi-change-label">{{ changeLabel }}</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { ArrowUp, ArrowDown, Minus } from '@element-plus/icons-vue'
import type { TrendDirection } from '@/types'

interface Props {
  label: string
  value: number
  currency?: string
  trend?: TrendDirection
  subtitle?: string
  change?: number
  changeLabel?: string
  precision?: number
}

const props = withDefaults(defineProps<Props>(), {
  currency: 'CNY',
  precision: 2
})

const displayValue = computed(() => {
  if (props.currency === 'CNY') {
    return `¥${formatNumber(props.value, props.precision)}`
  } else if (props.currency === 'USD') {
    return `$${formatNumber(props.value, props.precision)}`
  } else if (props.currency === '%') {
    return `${formatNumber(props.value, props.precision)}%`
  } else {
    return formatNumber(props.value, props.precision)
  }
})

const valueClass = computed(() => {
  if (!props.trend) return ''
  return `text-${props.trend}`
})

const changeClass = computed(() => {
  if (props.change === undefined) return ''
  if (props.change > 0) return 'text-up'
  if (props.change < 0) return 'text-down'
  return 'text-neutral'
})

const changePrefix = computed(() => {
  if (props.change === undefined) return ''
  if (props.change > 0) return '+'
  if (props.change < 0) return ''
  return ''
})

const trendIcon = computed(() => {
  if (props.trend === 'up') return ArrowUp
  if (props.trend === 'down') return ArrowDown
  return Minus
})

function formatNumber(num: number, precision: number): string {
  const absNum = Math.abs(num)
  const sign = num < 0 ? '-' : ''

  if (absNum >= 1e8) {
    return `${sign}${(absNum / 1e8).toFixed(precision)}亿`
  } else if (absNum >= 1e4) {
    return `${sign}${(absNum / 1e4).toFixed(precision)}万`
  } else {
    return `${sign}${absNum.toFixed(precision)}`
  }
}

function formatChange(change: number): string {
  if (Math.abs(change) >= 1e8) {
    return `${(change / 1e8).toFixed(2)}亿`
  } else if (Math.abs(change) >= 1e4) {
    return `${(change / 1e4).toFixed(2)}万`
  } else {
    return change.toFixed(2)
  }
}
</script>

<style scoped lang="scss">
@import '@/styles/variables.scss';

.kpi-card {
  background-color: $bg-card;
  border-radius: $border-radius-base;
  padding: $spacing-md;
  box-shadow: $shadow-card;
  transition: all 0.3s ease;

  &:hover {
    box-shadow: $shadow-hover;
    transform: translateY(-2px);
  }
}

.kpi-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: $spacing-sm;
}

.kpi-label {
  font-size: $font-size-sm;
  color: $text-secondary;
  font-weight: 500;
}

.kpi-trend-icon {
  font-size: 18px;

  &.trend-up {
    color: $color-up;
  }

  &.trend-down {
    color: $color-down;
  }

  &.trend-neutral {
    color: $color-neutral;
  }
}

.kpi-content {
  margin-bottom: $spacing-sm;
}

.kpi-value {
  font-size: $font-size-xxl;
  font-weight: 700;
  line-height: 1.2;
  margin-bottom: $spacing-xs;
}

.kpi-subtitle {
  font-size: $font-size-sm;
  color: $text-secondary;
}

.kpi-footer {
  display: flex;
  align-items: center;
  gap: $spacing-sm;
  font-size: $font-size-sm;
}

.kpi-change {
  font-weight: 600;
}

.kpi-change-label {
  color: $text-secondary;
}
</style>
