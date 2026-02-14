<template>
  <div class="chart-card">
    <div class="chart-header">
      <h3 class="chart-title">{{ title }}</h3>
      <div v-if="$slots.actions" class="chart-actions">
        <slot name="actions"></slot>
      </div>
    </div>

    <div class="chart-body" :style="{ height: height }">
      <canvas ref="chartCanvas"></canvas>
    </div>

    <div v-if="$slots.footer" class="chart-footer">
      <slot name="footer"></slot>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount, watch } from 'vue'
import {
  Chart,
  CategoryScale,
  LinearScale,
  PointElement,
  LineElement,
  BarElement,
  ArcElement,
  LineController,
  BarController,
  PieController,
  DoughnutController,
  Title,
  Tooltip,
  Legend,
  Filler
} from 'chart.js'
import type { ChartConfiguration, ChartType } from 'chart.js'

// Register Chart.js components
Chart.register(
  CategoryScale,
  LinearScale,
  PointElement,
  LineElement,
  BarElement,
  ArcElement,
  LineController,
  BarController,
  PieController,
  DoughnutController,
  Title,
  Tooltip,
  Legend,
  Filler
)

interface Props {
  title: string
  height?: string
  config: ChartConfiguration
}

const props = withDefaults(defineProps<Props>(), {
  height: '300px'
})

const chartCanvas = ref<HTMLCanvasElement>()
let chartInstance: Chart | null = null

const initChart = () => {
  if (!chartCanvas.value) return

  // Destroy existing chart
  if (chartInstance) {
    chartInstance.destroy()
  }

  // Create new chart with dark theme defaults
  const darkThemeConfig: ChartConfiguration = {
    ...props.config,
    options: {
      ...props.config.options,
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        ...props.config.options?.plugins,
        legend: {
          ...props.config.options?.plugins?.legend,
          labels: {
            ...props.config.options?.plugins?.legend?.labels,
            color: '#cfe0ff',
            font: {
              family: '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto',
              size: 12
            }
          }
        },
        tooltip: {
          ...props.config.options?.plugins?.tooltip,
          backgroundColor: 'rgba(26, 31, 46, 0.95)',
          titleColor: '#cfe0ff',
          bodyColor: '#8b9cb6',
          borderColor: '#2a3344',
          borderWidth: 1
        }
      },
      scales: props.config.type !== 'pie' && props.config.type !== 'doughnut' ? {
        ...props.config.options?.scales,
        x: {
          ...props.config.options?.scales?.x,
          grid: {
            ...props.config.options?.scales?.x?.grid,
            color: 'rgba(42, 51, 68, 0.5)'
          },
          ticks: {
            ...props.config.options?.scales?.x?.ticks,
            color: '#8b9cb6'
          }
        },
        y: {
          ...props.config.options?.scales?.y,
          grid: {
            ...props.config.options?.scales?.y?.grid,
            color: 'rgba(42, 51, 68, 0.5)'
          },
          ticks: {
            ...props.config.options?.scales?.y?.ticks,
            color: '#8b9cb6'
          }
        }
      } : undefined
    }
  }

  chartInstance = new Chart(chartCanvas.value, darkThemeConfig)
}

onMounted(() => {
  initChart()
})

onBeforeUnmount(() => {
  if (chartInstance) {
    chartInstance.destroy()
  }
})

// Watch for config changes and recreate chart
watch(() => props.config, () => {
  initChart()
}, { deep: true })

defineExpose({
  chart: chartInstance
})
</script>

<style scoped lang="scss">
@import '@/styles/variables.scss';

.chart-card {
  background-color: $bg-card;
  border-radius: $border-radius-base;
  padding: $spacing-md;
  box-shadow: $shadow-card;
}

.chart-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: $spacing-md;
}

.chart-title {
  font-size: $font-size-lg;
  font-weight: 600;
  color: $text-primary;
  margin: 0;
}

.chart-actions {
  display: flex;
  gap: $spacing-sm;
}

.chart-body {
  position: relative;
}

.chart-footer {
  margin-top: $spacing-md;
  padding-top: $spacing-md;
  border-top: 1px solid $border-color;
}
</style>
