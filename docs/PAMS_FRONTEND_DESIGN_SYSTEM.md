# PAMS 前端设计系统收敛 v4

本文件定义 PAMS 前端后续改动的视觉边界。目标不是重做 UI，而是把既有的移动端 dark app 风格（保留边界不代表已验收稳定版）收敛成可维护的组件契约，减少同一类卡片、标签、列表在不同页面重复实现。

统一外壳规则：`app-shell.css` 只负责外层 430px 居中视口与唯一主导航，`page-shell.css` 负责内容文档的边界及统一二级导航。业务页不再各自创建底栏；投资与收支二级导航共用尺寸、对齐和选中态。旧全局业务样式仍保留，其他冗余按模块另行整理，不能把本轮外壳收敛描述为全部样式已完成重构。详见 [外壳与内容页](PAMS_PAGE_BOUNDARY.md#统一外壳与内容页)。

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
python -m pytest tests/frontend/test_frontend_static_contract.py -q
```

仅在获准部署受影响 web 且没有可复用的已验证候选时，才执行相应构建/切换。以下为示例，不是文档修改的必跑项：

```powershell
docker compose up -d --build pams-web
```

如果新增正式页面，必须接入：

```html
<link rel="stylesheet" href="assets/css/style.css?v=20260620-mobile-density-v1" />
<link rel="stylesheet" href="assets/css/design-system.css?v=20260620-visual-cohesion-v1" />
```

并补充静态契约测试，防止页面绕开设计系统。


## 前端保留与最小改动规则

### 前端保留边界（不代表版本已验收）

投资页、收支页、更多页、风控页已经是移动端 dark app 风格。不要因为数据问题重做 UI。

硬规则：

- 底部移动端 nav 只能有：`投资`、`收支`、`更多`。
- 不要恢复旧的顶部 `statusbar` 到收支页和风控页。
- 不要恢复旧的多按钮入口条，例如同步、口径、风控一排大按钮。
- 交易页手动录入和筛选是折叠按钮入口，不要恢复成常驻大表单。
- 交易明细默认显示前 10 单，通过“展开更多”显示全部，不要显示“最近10单”冗余标签。
- 前端组件只展示 API 结果，不能在多个 JS 组件里重复实现行业、主题、地区、币种、资产类型分类。

相关回归测试优先看：

- `tests/frontend/test_frontend_static_contract.py`
- `frontend/public/investments.html`
- `frontend/public/assets/js/transactions.js`
- `frontend/public/assets/js/ui.js`
- `frontend/public/assets/css/style.css`

### 前端改动最小化

除非用户明确要求 UI 改造，否则：

- 不新增大段 CSS。
- 不改颜色、圆角、阴影、页面密度。
- 不重排页面主结构。
- 只做字段绑定、文案修正、数据质量提示、缺失列表或必要交互修复。

若确实需要改 HTML/JS：

1. 先确认当前页面已有结构。
2. 只改对应模块。
3. 加静态契约测试。
4. bump JS/CSS 版本号。
5. 验证候选构建；生产 `pams-web` 的重建/切换须另获部署授权。
6. 按 [工作流程第 0.2 节](DEVELOPMENT_WORKFLOW.md#02-测试验证分层抽样与证据复用) 选择页面范围，在候选环境及获准部署后进行对应可见验证。

## 构建生成的手机样式

投资页和更多页使用构建生成的 style-phone.css、design-system-phone.css；源文件与转换工具位于 frontend/public/assets/css/ 和 frontend/tools/mobile_styles.py。文档中的基础样式示例不要求把当前页面改回旧引用。页面职责以 [页面边界](PAMS_PAGE_BOUNDARY.md) 为准；实际样式使用须核对 HTML、Dockerfile 和生成工具。

更多页沿用投资页的字体、背景、主文本和手机留白变量；使用紧凑列表，高级工具默认折叠，待处理入口按数据状态显示。更多页局部样式放在 more.css，限制于页面作用域，不在导航组件计算财务数据。验证应覆盖手机和宽屏、正常与异常状态、折叠交互、键盘焦点和横向溢出。


## 更多页的视觉与验证细节

更多页继承投资页的 `pams-font-sans`、背景和文本变量，不另加字体、CDN 或独立主题。一级菜单沿用持仓名称的 16px、900 字重和 1.28 行高；标题使用 18px，二级工具使用 14px，辅助信息和分组标题保持较弱层级。使用平铺列表和细分隔线，不把资产数值大字号、等宽数字或收益红绿移植到普通菜单。

手机内容宽度及水平留白与投资页一致；高级工具默认折叠，展开只改变相关控件状态，不改变其他菜单的文本色。更多页局部样式限制在 `body.page-more`，不借局部修正改动全局折叠、投资或收支页面样式。

涉及该页面布局或共享样式时，按工作流程选择手机及宽屏覆盖；需要完整布局验证时覆盖 320、390 和 1280 视口、长标题、正常与异常状态、展开/收起、键盘 Tab/Shift+Tab 焦点与横向溢出。固定 DTO、禁网替身或模拟浏览器结果只证明对应候选，不代替真实设备、运行版本及财务核验。
