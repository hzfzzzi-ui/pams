# PAMS legacy research entrypoints

## 结论

这些入口不再是个人资产管理系统的正式实盘建议来源。PAMS 正式 live 策略输入只来自已校验入库的 `daily_position_adjustment_package@1`。

生产调度默认关闭旧策略扫描：

```text
PAMS_LEGACY_STRATEGY_JOBS_ENABLED=false
```

只有临时审计、回滚或人工排障时，才允许显式设置为 `true`。

## 调度入口

`backend/scheduler/scheduler.py` 中以下任务被视为 legacy strategy/watchlist/order_drafts 路径：

| 调度函数 | API | 风险 |
|---|---|---|
| `refresh_trend_state` | `POST /api/strategy/state/refresh` | 刷新旧策略状态 |
| `sentiment_refresh` | `POST /api/strategy/sentiment/refresh` | 刷新旧策略情绪输入 |
| `intraday_refresh` | `POST /api/strategy/intraday/refresh` | 盘中更新旧候选和订单草稿 |
| `cooldowns_expire` | `POST /api/strategy/cooldowns/expire` | 维护旧策略冷却状态 |
| `watchlist_daily_exit_check` | `POST /api/strategy/watchlist/exit-check` | 维护旧观察池 |
| `watchlist_weekly_scan` | `POST /api/strategy/watchlist/scan` | 生成旧观察池候选 |
| `strategy_eod_scan` | `POST /api/strategy/scan/eod`、`POST /api/strategy/orders/expire` | 生成旧 `pams_order_drafts` 和 strategy 通知 |

默认禁用后，这些函数直接返回 `legacy_strategy_jobs_disabled`，不会调用 API。

## API 入口

`backend/api/strategy/router.py` 仍保留以下只读、审计、手动兼容或 legacy 操作入口：

- `/api/strategy/candidates`
- `/api/strategy/orders`
- `/api/strategy/orders/{order_id}/action`
- `/api/strategy/orders/{order_id}/execute`
- `/api/strategy/orders/expire`
- `/api/strategy/risk-snapshot/refresh`
- `/api/strategy/state/refresh`
- `/api/strategy/scan/eod`
- `/api/strategy/scan/latest`
- `/api/strategy/scan/history`
- `/api/strategy/sentiment/refresh`
- `/api/strategy/intraday/refresh`
- `/api/strategy/cooldowns/expire`
- `/api/strategy/watchlist/scan`
- `/api/strategy/watchlist/exit-check`

这些入口不得作为新的 app/web 实盘建议来源。新功能必须读取 `/api/live-signals/*`。

## 数据库对象

旧策略路径写入或依赖：

- `pams_strategy_candidates`
- `pams_order_drafts`
- `pams_strategy_blacklist`
- `pams_risk_snapshot`
- `pams_watchlist`
- `pams_watchlist_log`
- `pams_notifications` 中 `source='strategy'` 或 `source` 来源于旧扫描的记录

正式 live signal 路径写入：

- `pams_live_signal_packages`
- `pams_live_signal_actions`
- `pams_live_signal_transaction_lifecycle`
- `pams_notifications` 中 `source='live_signal_package'`

## 前端依赖

`frontend/public/strategy.html` 和 `frontend/public/assets/js/strategy.js` 仍展示旧 `strategy`/`order_drafts` 页面。该页面只保留为 legacy/审计入口，不应作为正式实盘建议主页面。

正式 app/web 应改为读取 `GET /api/live-signals/packages`、`GET /api/live-signals/packages/latest` 和 transaction lifecycle 状态。

## 回滚说明

如需临时恢复旧调度：

1. 设置 `PAMS_LEGACY_STRATEGY_JOBS_ENABLED=true`。
2. 重启 `backend/scheduler`。
3. 确认新 package 通知已关闭，避免同一策略产生双通知。
4. 回滚期间仍禁止在 PAMS 内新增研究策略或把旧扫描结果升级为正式长期边界。
