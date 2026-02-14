// API 响应类型定义

export interface AssetClass {
  asset_class: string
  market_value: number
  holding_pnl?: number
  daily_pnl?: number
  children?: AssetClass[]
}

export interface Summary {
  as_of: string
  base_currency: string
  totals: {
    market_value: string | number
    holding_pnl: string | number
    daily_pnl: string | number
  }
  by_asset_class: AssetClass[]
  children: AssetClass[]
}

export interface Holding {
  venue: string
  account: string
  type: string
  symbol: string
  name: string
  currency: string
  market_value: number
  holding_pnl: number
  daily_pnl: number
  as_of: string
}

export interface NAVPoint {
  as_of: string
  total_market_value: number
  holdings_count: number
}

export interface DrawdownPoint {
  as_of: string
  drawdown_pct: number
}

export interface RiskEvent {
  as_of: string
  severity: 'info' | 'warn' | 'critical'
  code: string
  title: string
  details: string
}

export interface IPS {
  maxPositions: { core: number; satellite: number }
  maxDrawdown: number
  sectorLimit: number
  rebalanceBand: number
  alertMode: string
  executionRequiresApproval: boolean
}

export interface CashflowPoint {
  date: string
  income: number
  expense: number
  net: number
}

export interface WatchlistItem {
  id?: number
  market: string
  symbol: string
  name: string
  thesis: string
  status: 'candidate' | 'watch' | 'buy'
  created_at?: string
  updated_at?: string
}

// 格式化工具类型
export type Currency = 'CNY' | 'USD' | 'MXN'
export type TrendDirection = 'up' | 'down' | 'neutral'
