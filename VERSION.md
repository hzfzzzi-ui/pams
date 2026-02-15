# 版本历史

## V1.5 (2026-02-15)
**Telegram 交易 Bot + 前端持仓重构**

### 新增功能
- **交易识别 Bot**：Telegram Bot V3.0，支持图片 OCR 和文字指令录入持仓交易
  - Tesseract OCR 识别券商截图（同花顺/广发/IBKR/币安）
  - 文字交易解析（`买入 贵州茅台 100股 @1800`）
  - 确认流程：识别 → 展示确认消息 → 用户确认 → UPSERT 入库
- **每日持仓推送**：UTC 15:00（CST 9:00）自动推送持仓日报
- **通知推送框架**：Agent/外部服务可通过 API 投递消息，Bot 轮询发送
- **持仓交易 API**：`POST /api/holdings/trade` UPSERT 持仓快照
- **通知 API**：创建/轮询/标记已发送通知
- **定时价格调度器**：汇率、股价、加密货币价格定时刷新

### 前端重构
- **持仓子页面合并**：4 个独立页面（A股/美股/加密/其他）合并为配置驱动的 `holdings_detail.html`
- **共享工具模块**：新建 `utils.js`，提取 `safeNum`/`pnlPct`/`showError`/`createLineChart`
- **持仓分类页增强**：新增贵金属分类卡片、价格走势图、涨跌幅显示
- **收支页面优化**：HTML 精简 40%，JS 模块化重构

### 架构变更
- 新增 `backend/scheduler/` 定时调度服务
- 新增 `backend/api/endpoints/prices.py` 价格刷新端点
- 新增 `telegram-bot/ocr/` OCR 识别模块（4 个券商 Parser）
- 新增 `database/postgres/schema/009_notifications.sql` 通知表
- Telegram Bot Dockerfile 集成 Tesseract OCR + 中英文语言包

### 清理
- 删除 4 个持仓子页面（holdings_a_stock/us_stock/crypto/other .html + .js）
- 删除废弃后端文件（account_mapper.py、type_mapper.py、endpoints_overview.py）
- 删除废弃前端文件（test_api.html、cashflow.js 旧版）
- 删除 SQLite 备用 schema、Node.js 旧导入脚本

---

## V1.4 (2026-02-15)
**Dashboard增强 & Telegram助手Bot**

### 新增功能
- Dashboard 重构：风险分布饼图、净值走势、收支趋势、持仓明细
- 汇率双向获取：CNY/USD/MXN 独立获取，支持正反向切换
- Telegram 助手 Bot：任务队列系统，通过 Telegram 下发指令

---

## V1.3 (2026-02-15)
**项目文档重构与架构说明**

### 文档改进
- 重写README.md，从639行优化至345行
- 新增详细项目结构说明（文件功能、行数、核心端点）
- 新增完整API文档表格（资产管理 + 交易管理）
- 新增下一步实现计划（V1.4-V2.0路线图）

### 项目架构清理
- 删除冗余文档文件（181个文件，~17k行代码）
- 删除SQLite备用数据库，统一使用PostgreSQL
- 删除未使用的Node.js依赖（node_modules, package*.json）
- 项目结构更加清晰，维护成本降低

### 技术债务清理
- 移除过期配置文件（.sh, .yaml等）
- 统一版本管理（VERSION + VERSION.md）
- 规范化文档结构

---

## V1.2 (2026-02-15)
**完善收支页面交互与性能优化**

### 新增功能
- 开支统计支持按日/按月切换
- 按日统计改为折线图显示
- 图表交互联动：hover趋势图某日/月，右侧饼图动态显示该时段支出分类
- 饼图默认显示累计支出占比

### 性能优化
- 懒加载策略：hover时按需加载数据，减少初始请求93%（43→3个）
- Chart.js update优化：使用update方法替代destroy+create，提升响应速度75%
- 防抖控制：150ms防抖，减少70%无效渲染
- 智能缓存：避免重复请求，切换月份时清除缓存

### 技术改进
- 新增API端点：`/api/transactions/categories-by-range`
- 新增懒加载函数：`loadDailyCategoryData`、`loadMonthlyCategoryData`
- 月度趋势数据按年缓存

---

## V1.1 (2026-02-14)
**初始版本发布**

### 核心功能
- 收支管理系统基础架构
- PostgreSQL数据库
- FastAPI后端
- 原生JavaScript前端
- Docker容器化部署
