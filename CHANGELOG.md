# PAMS 变更日志

本文档记录 PAMS 项目的所有重要变更。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

---

## [v1.1] - 2026-02-15 📊

### 🎯 持仓分类系统完成

本版本实现了完整的持仓分类页面体系，对齐资产负债表的大类/小类结构，提供A股/美股/加密/其他四个分类的详细展示。

#### ✨ 新增功能

##### 持仓分类页面 (Holdings Categories)
- **统一分类页面** (`holdings_categories.html`)
  - 单页顺序展示A股/美股/加密/其他四个分类
  - 每个分类包含：市值走势图 + KPI卡片 + 持仓明细表
  - 快速导航跳转各分类区域

- **市值走势可视化** (Chart.js)
  - A股/美股/加密：近90天日线图
  - 其他类别：近90天周线图
  - 深色主题适配
  - 响应式图表设计

- **KPI汇总卡片** (每个分类4个指标)
  - 当前市值：分类总市值
  - 持仓收益：累计盈亏（带颜色）
  - 昨日收益：日盈亏变化（带颜色）
  - 持仓数量：标的数量统计

- **持仓明细表格**
  - 账户、小类、名称、币种
  - 市值、持仓收益、昨日收益
  - 盈亏颜色标识（绿涨红跌）

##### 后端增强

- **类型映射系统** (`backend/api/type_mapper.py`)
  - 建立完整的 type → 大类/小类映射规则
  - 支持的大类：
    - **A股**: stock, fund, stock_cn, equity_cn
    - **美股**: stock_us, equity_us
    - **加密**: crypto
    - **其他**: esop, equity_private, gold, wealth, cash, 等
  - 小类细分（如：A股基金、员工持股、ETH等）

- **账户标准化** (`backend/api/account_mapper.py`)
  - 统一账户名称显示规则
  - 中文友好名称映射
  - 示例：`cn_a_rmb` → `A股账户`

- **新增API端点**
  - `GET /holdings/by-category/{category}`
    - 按大类筛选持仓
    - 参数：category (A股/美股/加密/其他), limit
  - `GET /holdings/history/{category}`
    - 获取分类历史市值时间序列
    - 参数：category, days (默认90), interval (daily/weekly)
  - 增强 `GET /holdings/latest`
    - 返回字段新增：major_category, minor_category

##### 前端模块化

- **JavaScript模块** (`frontend/public/assets/js/`)
  - `holdings_categories.js` - 统一分类页面主逻辑
    - 并行加载4个分类数据
    - 渲染表格和KPI汇总
    - 绘制折线图
  - `holdings_a_stock.js` - A股独立页面（预留）
  - `holdings_us_stock.js` - 美股独立页面（预留）
  - `holdings_crypto.js` - 加密独立页面（预留）
  - `holdings_other.js` - 其他独立页面（预留）

- **HTML页面**
  - `holdings_categories.html` - 主分类页面
  - `holdings_a_stock.html` - A股独立页（预留）
  - `holdings_us_stock.html` - 美股独立页（预留）
  - `holdings_crypto.html` - 加密独立页（预留）
  - `holdings_other.html` - 其他独立页（预留）
  - `test_api.html` - API调试页面

- **页面导航更新**
  - `holdings.html` 添加跳转到分类详情的按钮
  - 导航文本：📊 查看分类详情（A股/美股/加密/其他）

#### 🗄️ 数据管理

- **数据库Schema更新**
  - `database/postgres/schema/006_update_accounts.sql`
  - 账户标准化脚本

- **分类映射调整**
  - **华为员工持股**：从 A股 移至 其他（内部持股）
    - 类型：`equity_private` → 大类：其他, 小类：员工持股
  - **ETH现金**：添加到 加密 类别
    - 新增记录：venue=binance, type=crypto, symbol=ETH
    - 市值：$679.26 USD

- **当前数据状态** (2026-02-15)
  - **A股**: 14条持仓
    - 紫金矿业 ¥32,113
    - 华夏科创50ETF联接C ¥28,216
    - 华夏人工智能ETF联接A ¥10,519
    - 等...
  - **美股**: 0条持仓
  - **加密**: 1条持仓
    - ETH现金 $679.26
  - **其他**: 16条持仓
    - 华为员工内部持股 ¥318,000
    - 支付宝余额宝 ¥21,256
    - 等...

#### 📚 文档完善

- **HOLDINGS_CATEGORIES_UPDATE.md**
  - 持仓分类系统设计文档
  - 类型映射规则说明
  - API端点使用指南

- **HOLDINGS_PAGE_UPDATE.md**
  - 持仓页面结构说明
  - 前端组件文档
  - 数据流设计

- **ACCOUNT_MAPPING.md**
  - 账户映射规则详解
  - 标准化命名规范

- **ACCOUNT_STANDARDIZATION.md**
  - 账户名称标准化流程
  - 显示名称规范

- **ACCESS_GUIDE.md**
  - GitHub仓库访问指南
  - 权限配置说明

- **SET_REPO_PRIVATE.md**
  - 仓库隐私设置指南
  - GitHub私有化步骤

#### 🛠️ 开发工具

- **快速提交脚本**
  - `push-now.sh` - Linux/Mac快速提交脚本
  - `quick-push.bat` - Windows批处理脚本
  - `quick-push.ps1` - PowerShell脚本
  - 自动添加、提交、推送到GitHub

#### 🐛 Bug修复

- **分类过滤逻辑错误**
  - 修复：type映射到major_category的逻辑
  - 确保esop/equity_private正确分类到"其他"

- **API路径参数处理**
  - 修复：移除错误的`Query(...)`用法
  - 路径参数直接使用 `category: str`

- **浏览器缓存问题**
  - 解决：容器重建后页面不刷新
  - 方案：`docker compose build --no-cache` + 强制刷新

- **KPI数据显示为0**
  - 修复：前端聚合计算逻辑
  - 确保正确累加市值和盈亏数据

- **图表数据缺失**
  - 修复：历史数据API端点
  - 清理旧的假数据，以真实数据为准

#### 📊 技术统计

- **新增文件**: 23个
  - 后端：2个Python模块
  - 前端：6个HTML + 6个JS模块
  - 文档：5个Markdown
  - 脚本：3个
  - 数据库：1个SQL

- **修改文件**: 8个
  - 后端：main.py, transactions.py
  - 前端：holdings.html, cashflow.html, 4个JS模块

- **代码增量**: 3,975行新增, 28行删除

#### 🚀 部署说明

- **容器更新**: 重建 `pams-web` 和 `pams-api` 容器
- **数据库**: 执行 `006_update_accounts.sql` 更新脚本
- **访问**: http://pams.local/holdings_categories.html
- **浏览器**: 需清除缓存或强制刷新 (Ctrl+Shift+R)

#### 🔒 已知限制

- **美股数据为空**: 暂无美股持仓记录
- **历史数据不足**: 仅当日快照，无历史趋势
- **周线图空白**: 其他类别因数据点少，图表可能为空

#### 📝 待优化

- [ ] 补充历史持仓快照数据（至少30天）
- [ ] 添加分类筛选器（按账户/小类筛选）
- [ ] 持仓明细支持导出CSV
- [ ] 图表交互增强（tooltip、zoom）
- [ ] 移动端响应式优化

---

## [v1.0] - 2026-02-15 🎉

**首个正式发布版本** - 收支分析模块与项目架构清理

### ✨ 新增功能

#### 收支分析模块 (Cashflow)
- **月度统计**：自动汇总每月收入、支出和净额
- **智能分类**：基于关键词的自动分类引擎（置信度评分）
- **分类占比**：支出分类饼图可视化（Chart.js）
- **交易记录**：最近30条交易明细表格
- **账户管理**：支持多账户来源（微信、支付宝、信用卡等）
- **数据库表**：
  - `pams_transactions` - 交易记录主表
  - `pams_category_rules` - 分类规则表
  - `pams_budgets` - 预算管理表（预留）

#### 资产总览 (Dashboard)
- **5大KPI指标**：总资产、总负债、净资产、最大回撤、持仓数
- **资产结构饼图**：按资产类别分组可视化
- **负债结构分析**：债务分类和占比
- **净资产曲线**：90天历史NAV时间序列
- **近期收支**：30天收支柱状图迷你图表

#### 持仓管理 (Holdings)
- **完整持仓表格**：平台、账户、类型、代码、名称、市值、盈亏
- **多维度筛选**：按资金类型、平台、账户筛选
- **实时盈亏**：持仓盈亏、日涨跌显示

#### 投资策略 (Strategy)
- **IPS策略管理**：投资政策声明参数化配置
- **风险监控**：集中度检查、数据完整性、回撤分析
- **NAV/回撤双轴图**：净资产与回撤对比
- **风险事件列表**：info/warn/critical三级分类

#### 观察池 (Watchlist)
- **候选标的管理**：CN/US市场标的追踪
- **状态分类**：candidate/active/archived
- **投资论点**：记录每个标的的投资理由

### 🏗️ 项目重构

#### 架构清理
- **归档旧版代码**：1.4MB代码归档到 `archive/`
  - `web/` → `archive/v1.0-legacy/web/`
  - `api/` → `archive/v1.0-legacy/api/`
  - `src/` → `archive/v1.0-legacy/src/`
- **归档重复配置**：
  - `ingest/` → `archive/redundant/ingest/`
  - `import/` → `archive/redundant/import/`
  - `Caddyfile`, `schema.sql` 等
- **目录优化**：从22个目录精简到14个（精简36%）
- **结构规范化**：`frontend/`, `backend/`, `gateway/`, `database/`

#### 文档完善
- **README.md**：详细的项目简介、安装指南、API文档
- **CURRENT_ARCHITECTURE.md**：完整系统架构文档（19KB）
- **CLEANUP_PLAN.md**：清理计划和执行方案（7.9KB）
- **ARCHITECTURE_CLEAN.md**：清理后架构总览
- **LICENSE**：MIT开源许可证

### 🐛 Bug修复
- **总览页404错误**：修复API路径不一致导致的数据加载失败
- **前端资源路径**：统一CSS和JS模块导入路径
- **API端点路由**：区分带/不带 `/api` 前缀的端点

### 🔧 技术改进
- **Git版本控制**：初始化仓库、.gitignore、规范提交
- **Docker优化**：优化镜像构建、容器编排
- **API规范化**：统一响应格式、错误处理

### 📊 数据统计
- **代码量**：17,769行
- **文件数**：202个
- **提交数**：3个
- **归档代码**：1.4MB

### 🚀 部署说明
- **技术栈**：Python 3.11 + FastAPI + PostgreSQL 16 + HTML5 + Chart.js
- **部署方式**：Docker Compose（LAN-only）
- **访问地址**：http://pams.local 或 http://192.168.0.143:8080

---

## [未发布] - Vue 3 迁移进行中

### 🚀 Vue 3 前端框架化 (V1.5.0 开发中)

#### ✨ 已完成
- **项目初始化**
  - 创建 `frontend-vue/` 新前端目录
  - 配置 Vite 5 + Vue 3.4 + TypeScript 5 技术栈
  - 集成 Element Plus UI 组件库 (自动导入)
  - 配置 Pinia 2.1 状态管理
  - 配置 Vue Router 4 路由

- **设计系统建立**
  - 定义完整的设计代币 (Design Tokens)
  - SCSS 变量系统: 颜色、间距、字体、阴影等
  - 深色主题适配
  - 响应式断点定义

- **核心组件开发**
  - `KPICard.vue`: KPI 指标卡片
    - 自动数值格式化 (万/亿)
    - 趋势指示器 (上涨/下跌/中性)
    - 多币种支持 (CNY/USD/%)
    - 变化值显示
  - `ChartCard.vue`: 图表容器组件
    - Chart.js 4.4 集成
    - 深色主题自动适配
    - 响应式图表
    - 支持所有图表类型 (折线/柱状/饼图等)

- **状态管理**
  - `useAssetStore`: 资产数据 Pinia store
    - 汇总数据、持仓、NAV 序列、风险事件
    - Computed 自动计算总资产/负债/净资产
    - 统一 API 调用封装

- **页面开发**
  - `MainLayout.vue`: 主布局
    - 顶部导航栏 (logo、快照时间、页面导航)
    - 刷新按钮
    - 页面切换动画
  - `Dashboard.vue`: 总览页面 (完整实现)
    - 5 个 KPI 卡片
    - 4 个图表 (资产饼图、负债饼图、NAV 曲线、收支柱图)
    - 风险告警面板
    - 系统状态面板
  - `Holdings.vue`: 持仓明细页面 ✅
  - `Cashflow.vue`: 收支分析页面 ✅
  - `Strategy.vue`: 策略/IPS页面 ✅
  - `Watchlist.vue`: 观察池页面 ✅

- **工具函数**
  - `format.ts`: 数值格式化、日期格式化、趋势判断
  - `api/client.ts`: Axios 封装、请求拦截、错误处理

- **TypeScript 类型定义**
  - 完整的 API 响应类型
  - Summary, Holding, NAVPoint, RiskEvent, IPS 等

- **Docker 部署配置**
  - 多阶段构建 Dockerfile
  - Nginx 生产配置 (Gzip、缓存、SPA 路由)
  - Health check 端点

#### 📝 技术亮点
- **开发服务器**: 本地 http://localhost:5173 正常运行
- **生产构建**: `npm run build` 生成优化后的静态文件
- **类型安全**: 全栈 TypeScript 类型检查
- **按需加载**: Element Plus 组件自动按需导入
- **代码分割**: 路由级别懒加载

#### ✅ 页面迁移完成 (2026-02-15)
- [x] **Holdings 持仓页面** - 完整功能实现
  - Element Plus 表格组件 + 斑马纹
  - 类型筛选器 (stock/fund/crypto等)
  - 列排序功能 (市值/盈亏)
  - 底部统计栏 (总市值/持仓收益)
  - 等宽字体显示数值

- [x] **Cashflow 收支页面** - 增强版
  - 4格统计卡片 (总收入/支出/净额/数据范围)
  - 收支曲线图 (柱状图+折线图组合)
  - 天数筛选器 (30/60/90/180天)
  - 按月汇总预留位置
  - 空状态提示

- [x] **Strategy 策略页面** - 完整功能
  - IPS v1 参数卡片展示 (6个参数)
  - NAV/回撤双轴图表
  - 风险事件列表 (按严重度分类)
  - JSON调试面板 (可折叠)
  - 数据完整性检查提示

- [x] **Watchlist 观察池页面** - 增强交互
  - 观察池列表表格
  - 市场/状态筛选器
  - 添加/编辑/删除功能 (对话框表单)
  - 表单验证
  - 空状态提示
  - Popconfirm 删除确认

#### 🎨 UI 增强
- 所有页面统一设计语言
- Element Plus 深色主题定制
- 响应式网格布局
- 等宽字体显示代码/数值
- 统一的空状态设计

#### 🔄 待完成 (下一阶段)
- [ ] 更新 Docker Compose 集成 Vue 前端
- [ ] 响应式优化 (移动端适配)
- [ ] 性能优化 (虚拟滚动、缓存策略)
- [ ] E2E 测试
- [ ] 部署验证

---

## [V1.0.0] - 2026-02-14

### 🎉 首个生产版本上线

这是 PAMS 个人资产管理系统的首个完整可用版本，实现了核心的资产总览功能。

### ✨ 新增功能

#### Dashboard 总览页
- **5个核心KPI卡片**
  - 总资产: 汇总所有非负债资产
  - 总负债: 汇总所有债务
  - 净资产: 总资产 - 总负债
  - 最大回撤(MDD): 占位符,待接入NAV时间序列
  - 条目数: 持仓数量统计

- **资产结构可视化**
  - 资产结构饼图: 按 asset_class 分类展示
  - 资产明细列表: 显示各资产类别及其子分类(按venue)
  - 支持中文映射: CN_EQUITY → A股权益, US_EQUITY → 美股权益等

- **负债结构可视化**
  - 负债结构饼图: 按账户名称分组展示
  - 显示各信用卡账户的负债金额

- **净资产曲线图**
  - 显示近90天净资产走势
  - 支持时间序列可视化

- **收支迷你图**
  - 显示近30天收支情况
  - 柱状图展示净额变化

- **风险告警面板**
  - 集成 `/api/risk/latest` 端点
  - 显示集中度告警、数据完整性告警等

- **系统状态面板**
  - 显示API后端类型、数据截至日期等信息

### 🔧 技术架构更新

#### 前后端分离
- **前端**: Nginx 容器托管静态文件 (pams-web)
- **后端**: FastAPI Python服务 (pams-api)
- **网关**: Caddy 反向代理,统一路由 (:80端口)

#### API端点迁移
从旧的 Express API (Node.js, 端口18888) 迁移到 FastAPI (Python, 端口8000):
- `/api/overview` (废弃) → `/api/summary2/latest` + `/api/holdings/latest`
- `/api/nav/series` (保留)
- `/api/cashflow/series` (保留)
- `/api/risk/latest` (新增)

#### Docker Compose 路径规范化
- `pams-web`: `../../frontend`
- `pams-api`: `../../backend/api`
- `caddy`: `../../gateway/Caddyfile`

### 🐛 Bug修复

#### 1. API路由404错误
- **问题**: dashboard.js 调用不存在的 `/api/overview` 端点
- **修复**: 更新为调用 FastAPI 的 `/summary2/latest` 和 `/holdings/latest`
- **文件**: `frontend/public/assets/js/dashboard.js`
- **提交**: 2026-02-14 23:09

#### 2. Chart.js未定义错误
- **问题**: index.html 缺少 Chart.js 库引用
- **修复**: 添加 `<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>`
- **文件**: `frontend/public/index.html`
- **提交**: 2026-02-14 23:16

#### 3. 资产金额显示为¥0
- **问题**: dashboard.js 使用错误的字段名 `market_value_base` (应为 `market_value`)
- **修复**: 将所有 `cls.market_value_base` 改为 `cls.market_value`
- **影响**: 总资产计算、资产类别汇总、场所明细
- **文件**: `frontend/public/assets/js/dashboard.js` (第27, 64, 71行)
- **提交**: 2026-02-14 23:18

#### 4. 负债结构饼图空白
- **问题**: 代码从 `by_asset_class[].by_venue` 读取负债数据,但该字段不存在
- **修复**: 从 `summary.children[]` 数组筛选 `asset_class === 'LIABILITY'` 的项目并按账户名分组
- **文件**: `frontend/public/assets/js/dashboard.js` (第121-147行)
- **提交**: 2026-02-14 23:20

#### 5. IP地址无法访问Dashboard
- **问题**: Caddyfile 仅配置 `http://pams.local` 虚拟主机,IP访问返回空响应
- **修复**: 添加 `:80` 默认站点处理IP地址请求
- **文件**: `gateway/Caddyfile`
- **提交**: 2026-02-14 (早期修复)

### 📝 配置文件变更

#### gateway/Caddyfile
```diff
+ # Default site for IP access (192.168.0.143)
+ :80 {
+   # API routes -> FastAPI backend
+   handle_path /api/* {
+     reverse_proxy pams-api:8000
+   }
+
+   # Frontend static files -> Nginx
+   handle {
+     reverse_proxy pams-web:80
+   }
+ }
```

#### config/docker/compose.yaml
```diff
  pams-web:
    build:
-     context: ./web
+     context: ../../frontend

  pams-api:
    build:
-     context: ./api
+     context: ../../backend/api

  caddy:
    volumes:
-     - ./Caddyfile:/etc/caddy/Caddyfile:ro
+     - ../../gateway/Caddyfile:/etc/caddy/Caddyfile:ro
```

### 🗄️ 数据库

#### 使用的数据表
- `pams_holdings_snapshot`: 持仓快照数据 (30条记录)
- `pams_asset_class_map`: 资产类别映射规则
- `pams_risk_events`: 风险事件记录
- `pams_nav_daily`: 日净资产值记录

#### 数据导入
- 从 `import/balance_sheet_export.csv` 导入32行资产负债数据
- 包含9种资产类别,3种负债账户
- 总资产: ¥677,302.64 CNY
- 总负债: ¥32,174.73 CNY
- 净资产: ¥645,127.91 CNY

### 📊 数据统计

**资产分布**:
- CN_PRIVATE (华为内部持股): ¥318,000 (47%)
- RECEIVABLE (应收款): ¥83,683.13
- HPF (公积金): ¥57,414.63
- CN_FUND (A股基金): ¥53,683.32
- CN_EQUITY (A股权益): ¥51,451
- 其他: ¥113,070.56

**负债分布**:
- 建行-0453信用卡: ¥25,313.26 (79%)
- 广发-9711信用卡: ¥6,672.77 (21%)
- 广发-7443信用卡: ¥188.70 (0.6%)

### 🔒 已知限制

1. **NAV时间序列数据不足**: 仅有2个数据点 (2026-02-11, 2026-02-13),最大回撤计算暂不可用
2. **现金流数据为空**: `/api/cashflow/series` 返回空数组,收支图表无数据显示
3. **风险告警**: 检测到单一标的集中度过高 (华为内部持股占52.94%)

### 🚀 部署信息

- **访问地址**: http://192.168.0.143/ 或 http://pams.local (需配置DNS)
- **容器运行**: pams-web, pams-api, pams-caddy, pams-postgres
- **构建时间**: 2026-02-14 23:20:26 CST
- **镜像版本**:
  - nginx:1.27-alpine
  - postgres:16-alpine
  - caddy:2

### 📚 文档

- 主文档: `README.md`
- Dashboard规范: `DASHBOARD_SPEC.md`
- API端点文档: `api/endpoints_overview.py`
- 架构重构计划: `.claude/plans/fizzy-roaming-sedgewick.md`

---

## 版本规则说明

- **主版本号 (MAJOR)**: 架构级重大变更,不兼容的API修改
- **次版本号 (MINOR)**: 新增功能,向后兼容
- **修订号 (PATCH)**: Bug修复,向后兼容

---

## 版本路线图

### ⚡ 近期开发优先级调整 (2026-02-14)

**核心策略**: 前端现代化优先,UI/UX全面重构

原计划的 V1.0.1 (Patch) 和 V1.1.0 (Minor) **暂缓**,优先推进:
1. ✨ **Vue 3 前端框架化** (架构级升级)
2. 🎨 **UI 设计系统重构** (参考成熟设计)

---

### V1.5.0 (Minor - 前端现代化) 🔥 **当前优先**
**预计发布**: 2026-03-30

#### 🎯 核心目标: Vue 3 框架化 + UI 重构

**阶段 1: Vue 3 技术栈迁移 (Week 1-2)**

**1. 项目初始化**
- [ ] 使用 Vite 创建 Vue 3 项目
  - 选择 TypeScript + Vue 3 + Pinia
  - ESLint + Prettier 代码规范
  - 集成 Vue Router 4

- [ ] 目录结构规划
  ```
  frontend/
  ├── src/
  │   ├── assets/          # 静态资源
  │   ├── components/      # 通用组件
  │   │   ├── common/      # 基础组件 (按钮/卡片/表格)
  │   │   ├── charts/      # 图表组件
  │   │   └── layout/      # 布局组件
  │   ├── views/           # 页面视图
  │   │   ├── Dashboard.vue
  │   │   ├── Holdings.vue
  │   │   ├── Cashflow.vue
  │   │   ├── Strategy.vue
  │   │   └── Watchlist.vue
  │   ├── stores/          # Pinia状态管理
  │   ├── composables/     # 组合式API
  │   ├── api/             # API调用层
  │   ├── types/           # TypeScript类型定义
  │   └── router/          # 路由配置
  ```

**2. UI 组件库选型**
- [ ] **方案A: Element Plus** (推荐)
  - 完整的组件生态
  - 深色主题支持
  - 中文文档友好

- [ ] **方案B: Ant Design Vue**
  - 企业级UI设计
  - 丰富的图表集成

- [ ] **方案C: Naive UI**
  - 轻量级,TypeScript原生
  - 深色主题优秀

**选择**: Element Plus + 自定义深色主题

**阶段 2: UI 设计系统 (Week 2-3)**

**1. 参考设计灵感**
- [ ] 参考 **Tailwind UI Dashboard** 布局
- [ ] 参考 **Stripe Dashboard** 配色方案
- [ ] 参考 **Linear** 极简设计理念
- [ ] 参考 **Notion** 卡片式布局

**2. 设计 Token 体系**
```typescript
// Design Tokens
export const designTokens = {
  colors: {
    primary: '#6aa6ff',      // 主题蓝
    success: '#43d19e',      // 成功绿
    warning: '#ffd166',      // 警告黄
    danger: '#ff6b6b',       // 危险红
    background: {
      base: '#0b0f14',       // 背景基色
      elevated: '#161b22',   // 抬升背景
      card: '#1c2128',       // 卡片背景
    },
    text: {
      primary: '#cfe0ff',    // 主文本
      secondary: '#8ea0c7',  // 次要文本
      muted: '#6e7681',      // 弱化文本
    },
  },
  spacing: {
    xs: '4px',
    sm: '8px',
    md: '12px',
    lg: '16px',
    xl: '24px',
    xxl: '32px',
  },
  borderRadius: {
    sm: '6px',
    md: '10px',
    lg: '16px',
    full: '9999px',
  },
  shadows: {
    sm: '0 2px 4px rgba(0,0,0,0.1)',
    md: '0 4px 12px rgba(0,0,0,0.15)',
    lg: '0 8px 24px rgba(0,0,0,0.2)',
  },
}
```

**3. 核心组件开发**
- [ ] **KPICard 组件**
  - 支持趋势指示 (up/down/neutral)
  - 数字动画效果
  - 响应式卡片

- [ ] **DataTable 组件**
  - 可排序/筛选/搜索
  - 虚拟滚动 (大数据量)
  - 自定义列

- [ ] **ChartCard 组件**
  - Chart.js 封装
  - 饼图/折线图/柱状图
  - 深色主题适配

- [ ] **AlertBanner 组件**
  - 风险等级样式 (info/warn/crit)
  - 可展开详情
  - 动画过渡

**阶段 3: 页面迁移 (Week 3-4)**

**1. Dashboard 页面重构**
- [ ] 使用 Grid 布局系统
- [ ] KPI 卡片组件化
- [ ] 图表懒加载优化
- [ ] 骨架屏加载状态

**2. Holdings 页面重构**
- [ ] 数据表格组件化
- [ ] 高级筛选面板
- [ ] 详情抽屉 (Drawer)

**3. 其他页面迁移**
- [ ] Cashflow 页面
- [ ] Strategy 页面
- [ ] Watchlist 页面

**阶段 4: 状态管理与性能优化 (Week 4-5)**

**1. Pinia Store 设计**
```typescript
// stores/asset.ts
export const useAssetStore = defineStore('asset', {
  state: () => ({
    summary: null,
    holdings: [],
    loading: false,
    error: null,
  }),

  actions: {
    async fetchSummary(baseCurrency = 'CNY') {
      this.loading = true
      try {
        const data = await api.getSummary(baseCurrency)
        this.summary = data
      } catch (err) {
        this.error = err
      } finally {
        this.loading = false
      }
    }
  },

  getters: {
    totalAssets: (state) => state.summary?.totals.market_value || 0,
    netWorth: (state) => /* 计算逻辑 */,
  }
})
```

**2. 性能优化**
- [ ] 组件懒加载 (defineAsyncComponent)
- [ ] 图表按需渲染
- [ ] API 响应缓存
- [ ] 防抖/节流处理

**技术栈总结**:
```json
{
  "framework": "Vue 3.4",
  "build": "Vite 5",
  "language": "TypeScript 5",
  "ui": "Element Plus",
  "state": "Pinia",
  "router": "Vue Router 4",
  "charts": "Chart.js",
  "http": "Axios",
  "utils": "VueUse"
}
```

---

### V1.1.0 (Minor - 数据完整性) **推迟至 Vue 3 完成后**
**预计发布**: 2026-04-15

#### 🎯 核心目标: 完善数据采集

- [ ] **NAV时间序列数据采集**
  - 实现定时任务自动生成NAV快照
  - 补全历史数据 (至少30天)
  - 启用最大回撤(MDD)计算

- [ ] **Firefly III现金流集成**
  - 读取Firefly API获取交易记录
  - 按日汇总收入/支出/净额
  - 在Dashboard收支图表中展示真实数据

- [ ] **后端API模块化重构**
  - 拆分 main.py (897行) 为模块化结构
  - endpoints/ 端点层
  - services/ 业务逻辑层
  - models/ 数据模型层

---

### V1.2.0 (Minor - 自动化采集)
**预计发布**: 2026-04-30

#### 🎯 核心目标: 减少手动数据导入

**1. 数据源自动采集**
- [ ] **Binance API集成**
  - 自动获取加密货币持仓
  - 定时更新市价
  - 支持多币种转换

- [ ] **IBKR API集成** (Flex Query)
  - 自动拉取美股持仓快照
  - 支持历史数据回填

- [ ] **银行/券商数据爬虫** (可选)
  - 建行/理财通数据自动采集
  - 需要账号授权

**2. 定时任务调度**
- [ ] 实现后台Scheduler
  - 每日凌晨自动采集数据
  - 生成NAV快照
  - 触发风险检测

**3. 数据导入界面**
- [ ] Web端CSV上传
  - 在线上传资产负债表
  - 自动解析和验证
  - 显示导入结果

---

### V2.0.0 (Major - 架构升级)
**预计发布**: 2026-Q3

#### 🎯 核心目标: 现代化技术栈

**前端重构**:
- [ ] 引入 Vue 3 框架
  - 组件化开发
  - 状态管理 (Pinia)
  - TypeScript支持

**后端重构**:
- [ ] FastAPI模块化
  - endpoints/ 端点层
  - services/ 业务层
  - models/ 数据模型层

- [ ] 引入缓存层
  - Redis缓存常用查询
  - API响应缓存

**实时功能**:
- [ ] WebSocket推送
  - 实时持仓更新
  - 风险告警推送

**移动端**:
- [ ] PWA支持
  - 离线缓存
  - 添加到主屏幕
  - 推送通知

---

### 功能愿望清单 (Backlog)

**分析功能**:
- [ ] 收益归因分析
- [ ] 资产相关性矩阵
- [ ] 风险指标 (Sharpe/Sortino)
- [ ] 回测功能

**策略功能**:
- [ ] 自动选股引擎
- [ ] 订单自动生成
- [ ] 再平衡建议

**数据管理**:
- [ ] 数据备份/恢复
- [ ] 导出PDF月度报告
- [ ] Excel批量导出

**用户体验**:
- [ ] 多账户/多用户支持
- [ ] 自定义仪表板布局
- [ ] 主题切换 (浅色/深色)

---

_最后更新: 2026-02-14 by Claude Sonnet 4.5_
