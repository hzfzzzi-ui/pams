import dayjs from 'dayjs'
import type { Currency } from '@/types'

/**
 * 格式化数字（带单位）
 */
export function formatNumber(num: number, precision = 2): string {
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

/**
 * 格式化货币
 */
export function formatCurrency(amount: number, currency: Currency = 'CNY', precision = 2): string {
  const formatted = formatNumber(amount, precision)

  switch (currency) {
    case 'CNY':
      return `¥${formatted}`
    case 'USD':
      return `$${formatted}`
    case 'MXN':
      return `MX$${formatted}`
    default:
      return formatted
  }
}

/**
 * 格式化百分比
 */
export function formatPercent(value: number, precision = 2): string {
  return `${(value * 100).toFixed(precision)}%`
}

/**
 * 格式化日期
 */
export function formatDate(date: string | Date, format = 'YYYY-MM-DD'): string {
  return dayjs(date).format(format)
}

/**
 * 格式化日期时间
 */
export function formatDateTime(date: string | Date): string {
  return dayjs(date).format('YYYY-MM-DD HH:mm:ss')
}

/**
 * 获取涨跌趋势
 */
export function getTrend(value: number): 'up' | 'down' | 'neutral' {
  if (value > 0) return 'up'
  if (value < 0) return 'down'
  return 'neutral'
}

/**
 * 获取涨跌符号
 */
export function getTrendSymbol(value: number): string {
  if (value > 0) return '+'
  if (value < 0) return ''
  return ''
}
