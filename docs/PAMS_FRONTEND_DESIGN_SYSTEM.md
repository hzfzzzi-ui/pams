# PAMS 前端设计系统收敛 v4

本文件定义 PAMS 前端后续改动的视觉边界。目标不是重做 UI，而是把已经稳定的移动端 dark app 风格收敛成可维护的组件契约，减少同一类卡片、标签、列表在不同页面重复实现。

## 设计定位

PAMS 是个人资产控制台，不是营销页。界面应优先服务三件事：

1. 快速判断资产状态。
2. 明确数据口径和更新时间。
3. 支持进一步下钻，而不是在首屏堆满解释文字。

视觉关键词：

- 克制、紧凑、高信息密度。
- 深色金融终端底色。
- 绿色 / 红色只表达收益与风险，不做装饰。
- 蓝色只表达导航、数据状态和可交互入口。
- 大卡片只用于核心状态区；普通列表、设置入口和检查项使用紧凑行。

## 样式分层

当前前端样式分三层：

```text
style.css
  旧全局样式、页面历史样式、现有业务模块样式

design-system.css
  新增设计系统覆盖层，只放 token、app-shell、共享组件和密度规则

页面内 style
  仅保留页面特有布局，后续逐步迁移到共享组件
```

`design-system.css` 必须在带版本号的 `style.css?v=...` 后加载。它不承载业务布局，不写接口相关样式，不直接绑定投资收益、风险、收支等业务含义。

正式页面的 `<body>` 必须带 `app-shell` 类。`ui.js` 仍会补充导航增强和预加载，但设计系统基础样式不能依赖 JS 执行后才生效。

## 页面骨架

正式页面只保留三类主入口：

- `investments.html`：投资。
- `ledger.html`：收支。
- `more.html`：更多。

低频页面从“更多”进入：

- `accounts.html`
- `data_sources.html`
- `research.html`

页面结构应保持一致：

```text
底部主导航
  -> 页面内二级导航 / tab
  -> 核心状态区
  -> 主列表 / 图表 / 检查项
  -> 点击后底部浮层或详情
```

不要恢复大 hero、状态栏大按钮、同步大入口条或旧独立风险页。低频工作台页面如果需要页面说明和快捷入口，使用 `.appWorkHeader`；不要恢复或新增 `appPageHero` / `appHero*`。

## 组件契约

后续新增 UI 时优先复用这些现有类或它们的语义：

- 面板：`.card`、`.investmentPanel`、`.portfolioRiskCategory`、`.portfolioRuleNode`
- 列表行：`.ledgerListRow`、`.syncCenterRow`、`.moreRow`
- 状态标签：`.statusBadge`、`.tag`、`.statusChip`、`.flowTag`
- 操作按钮：`.btn`、`.ledgerActionBtn`、`.reviewBtn`
- 工作台头部：`.appWorkHeader`、`.appShortcutGrid`、`.appShortcutCard`
- 底部浮层：`openBottomSheet`

新增页面如果需要新的卡片或列表，应先判断是否能复用上述结构。不能为了单页视觉效果新增一套完整组件。

## 移动端密度

移动端优先规则：

- 一屏先展示状态，不展示说明书。
- 列表行保持可扫读，标题、金额、状态三要素优先。
- 长解释进入详情或折叠区域。
- 热力图单元格优先展示标的名称和核心金额，完整解释放点击浮层。
- 交易、风险、配置页允许高密度，但必须有清晰点击入口。

## 回归要求

涉及前端设计系统的改动至少运行：

```powershell
python -m pytest tests/test_frontend_static_contract.py -q
```

若改动 CSS 或 HTML，运行态需要重建：

```powershell
docker compose up -d --build pams-web
```

如果新增正式页面，必须接入：

```html
<link rel="stylesheet" href="assets/css/style.css?v=20260620-mobile-density-v1" />
<link rel="stylesheet" href="assets/css/design-system.css?v=20260620-visual-cohesion-v1" />
```

并补充静态契约测试，防止页面绕开设计系统。
