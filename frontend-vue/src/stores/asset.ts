import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import api from '@/api/client'
import type {
  Summary,
  Holding,
  NAVPoint,
  DrawdownPoint,
  RiskEvent,
  IPS,
  CashflowPoint
} from '@/types'

export const useAssetStore = defineStore('asset', () => {
  // State
  const summary = ref<Summary | null>(null)
  const holdings = ref<Holding[]>([])
  const navSeries = ref<NAVPoint[]>([])
  const drawdownSeries = ref<DrawdownPoint[]>([])
  const riskEvents = ref<RiskEvent[]>([])
  const ips = ref<IPS | null>(null)
  const cashflowSeries = ref<CashflowPoint[]>([])
  const loading = ref(false)
  const error = ref<string | null>(null)
  const lastFetchTime = ref<number>(0) // 最后一次数据获取时间戳

  // Computed
  const totalAssets = computed(() => {
    if (!summary.value?.by_asset_class) return 0
    const assets = summary.value.by_asset_class
      .filter(c => c.asset_class !== 'LIABILITY')
      .reduce((sum, c) => sum + Number(c.market_value || 0), 0)
    return assets
  })

  const totalLiabilities = computed(() => {
    if (!summary.value?.by_asset_class) return 0
    const liability = summary.value.by_asset_class.find(c => c.asset_class === 'LIABILITY')
    return Number(liability?.market_value || 0)
  })

  const netAssets = computed(() => {
    return totalAssets.value + totalLiabilities.value
  })

  const totalHoldingPnl = computed(() => {
    return Number(summary.value?.totals?.holding_pnl || 0)
  })

  const totalDailyPnl = computed(() => {
    return Number(summary.value?.totals?.daily_pnl || 0)
  })

  const holdingsCount = computed(() => {
    if (!summary.value?.children) return 0
    return summary.value.children.filter(c => c.asset_class !== 'LIABILITY').length
  })

  const maxDrawdown = computed(() => {
    if (drawdownSeries.value.length === 0) return 0
    return Math.min(...drawdownSeries.value.map(d => d.drawdown_pct))
  })

  const assetClasses = computed(() => {
    if (!summary.value?.by_asset_class) return []
    return summary.value.by_asset_class.filter(c => c.asset_class !== 'LIABILITY')
  })

  const liabilities = computed(() => {
    if (!summary.value?.children) return []
    return summary.value.children.filter(c => c.asset_class === 'LIABILITY')
  })

  // Actions
  async function fetchSummary(baseCurrency = 'CNY') {
    loading.value = true
    error.value = null
    try {
      summary.value = await api.get<Summary>(`/summary2/latest?baseCurrency=${baseCurrency}`)
    } catch (err: any) {
      error.value = err.message || 'Failed to fetch summary'
      console.error('Failed to fetch summary:', err)
    } finally {
      loading.value = false
    }
  }

  async function fetchHoldings() {
    loading.value = true
    error.value = null
    try {
      const response = await api.get<{ rows: Holding[] }>('/holdings/latest')
      holdings.value = response.rows || []
    } catch (err: any) {
      error.value = err.message || 'Failed to fetch holdings'
      console.error('Failed to fetch holdings:', err)
    } finally {
      loading.value = false
    }
  }

  async function fetchNAVSeries(limit = 90) {
    try {
      const response = await api.get<{ rows: NAVPoint[] }>(`/nav/series?limit=${limit}`)
      navSeries.value = response.rows || []
    } catch (err) {
      console.error('Failed to fetch NAV series:', err)
      navSeries.value = []
    }
  }

  async function fetchDrawdownSeries(limit = 365) {
    try {
      const response = await api.get<{ rows: DrawdownPoint[] }>(`/nav/drawdown?limit=${limit}`)
      drawdownSeries.value = response.rows || []
    } catch (err) {
      console.error('Failed to fetch drawdown series:', err)
      drawdownSeries.value = []
    }
  }

  async function fetchRiskEvents(limit = 100) {
    try {
      const response = await api.get<{ events: RiskEvent[] }>(`/risk/latest?limit=${limit}`)
      riskEvents.value = response.events || []
    } catch (err) {
      console.error('Failed to fetch risk events:', err)
    }
  }

  async function fetchIPS() {
    try {
      ips.value = await api.get<IPS>('/policy/ips')
    } catch (err) {
      console.error('Failed to fetch IPS:', err)
    }
  }

  async function fetchCashflowSeries(limit = 30, currency = 'CNY') {
    try {
      const response = await api.get<{ rows: CashflowPoint[] }>(
        `/cashflow/series?limit=${limit}&currency=${currency}`
      )
      cashflowSeries.value = response.rows || []
    } catch (err) {
      console.error('Failed to fetch cashflow series:', err)
      cashflowSeries.value = []
    }
  }

  async function refreshAll(force = false) {
    // 暂时禁用缓存，确保每次都加载
    await Promise.all([
      fetchSummary(),
      fetchHoldings(),
      fetchNAVSeries(),
      fetchDrawdownSeries(),
      fetchRiskEvents(),
      fetchIPS(),
      fetchCashflowSeries()
    ])
  }

  async function forceRefresh() {
    await refreshAll(true)
  }

  return {
    // State
    summary,
    holdings,
    navSeries,
    drawdownSeries,
    riskEvents,
    ips,
    cashflowSeries,
    loading,
    error,
    // Computed
    totalAssets,
    totalLiabilities,
    netAssets,
    totalHoldingPnl,
    totalDailyPnl,
    holdingsCount,
    maxDrawdown,
    assetClasses,
    liabilities,
    // Actions
    fetchSummary,
    fetchHoldings,
    fetchNAVSeries,
    fetchDrawdownSeries,
    fetchRiskEvents,
    fetchIPS,
    fetchCashflowSeries,
    refreshAll,
    forceRefresh
  }
})
