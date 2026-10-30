# IBKR 有效报告发布约定

本轮为 L 级来源治理，不改变 XML 金额解析、收益/NAV/汇率公式，也不是历史资产核准。

## 输入、输出与依赖

- collector 输入：现有 Flex 凭据、XML、可选验证参考日期；凭据不进入发布元数据。
- `ibkr_report_state` 输入：现有同步审计记录、原文目录、离线 XNYS 日历。只选择已提交成功且完整的报告，输出有效文件、实际业务日期、审计 ID 和质量状态；不计算金额，不调用 endpoint。
- collector 输出：原业务写入和 success 审计同事务提交；`summary_json.publication` 为发布标记，含 version、state、report_date、expected_date、SHA256、重复导入指纹和本地账户代码。不增加表或修改 schema。
- performance/风控：共用选源后继续调用原金额解析函数。保留旧字段，新增 `report_quality`；缺有效报告时 performance 返回 404，风控沿用缺失数据核准机制，不补零。
- scheduler：只新增 `expected_statement_reference`，由 API 按交易日规范化并在写库前校验；返回 `expected_statement_as_of` 用于后置检查，不以 `as_of` 覆盖实际报告日期。
- 同步中心：复用现有状态/详情/记录数字段；被拦截的 HTTP 200 不再显示“已刷新”。不改布局、CSS 或导航，不在前端计算财务口径。

## 发布门禁

1. 原始 XML 使用唯一文件名，先完成临时文件写入、flush/fsync，再原子改名；候选文件存在不代表发布成功。
2. 检查 BASE_SUMMARY 现金、报告日期与当前持仓日期、关键数字有效性及单一来源账户。原解析器保持原样，不把缺失字段默认为本轮“已核准”。
3. 取得数据库事务锁，重新读取已提交发布日期。拒绝过期、倒退及未收市/未来日期，坏文件不能降低发布日期高水位。IBKR 为全球账户，不能仅因美国休市拒绝其他市场当日有效报告。
4. 业务写入、交易事实补录与发布审计同事务提交。失败回滚并独立记录 failed；政策拦截记录 blocked，不写业务数据。
5. 字节完全相同且依赖写入模式相同的报告只记 unchanged，不重写业务行，也不移动有效发布指针。原文生成时间变化仍可能导致正常重写，本轮不承诺语义去重。

## 读取与质量

- 仅查现有 IBKR success 审计，按实际报告日期及审计 ID 选源；不扫描任意原文，不按文件 mtime 兜底。
- 新记录校验原文 SHA256；损坏/丢失时尝试上一有效记录并提示。数据库不可用时不擅自绕过发布状态。
- 历史成功记录没有 SHA256：只读兼容、检查现存原文并标 `legacy_unverified`，不能事后追认原文不可变。
- 已有有效的新发布记录优先于旧版 success；旧版自动任务留下的新 success 也不能直接替换已验证来源。所有新发布原文都不可用时才回看历史兼容源并提示。
- 沿用旧值、缺报告、过期和历史待核准分别提示；质量字段不等于官方保证金字段完整或所有资产可信。
- 日历固定为 `exchange-calendars==4.13.2` 的 XNYS，用于应有报告日期下限；默认纽约 16:30 截止，正常交易日的实际收市约束包含夏令时和半日市。美国休市日允许截止时间已过的全球账户当日报告，不能在当日尚未结束时提前确认。日历范围以依赖支持范围为限，越界失败不猜日期；不重写亚洲/基金/其他市场调度规则。

## 兼容边界与发布

- 原 `as_of` / `align_to_latest_snapshot` 手动日期选项保留，仍须通过原文日期门禁；目标快照日期与实际日期不同的导入记 historical，不替代有效报告。此兼容路径仍可写指定日期快照，不应当作自动日终确认。
- 同日报告官方修订仍可导入，但本轮不解决既有交易事实订正、跨年 YTD 入金、归属比例或空账户零余额的解析限制。
- 首次发布前须冻结原文和审计选源，披露金额差异；不得为了“相等”调整公式或回灌事实表。
- 将来部署需要匹配 API、scheduler、web 三组件，单独授权；本轮只建验证镜像和独立 PR，不切生产服务。旧 API 可忽略新增 JSON，但回退会失去新选源保证，不自动恢复数据库。

## 验证

使用专用 `PAMS_TEST_POSTGRES_DSN`；真实事务测试会新建随机测试库，不读取应用 PG* 凭据。禁止将其指向生产。

```powershell
python -m pytest tests/brokers/ibkr/test_ibkr_report_state.py tests/brokers/ibkr/test_ibkr_publication_transaction.py tests/brokers/ibkr/test_ibkr_sync_transaction.py tests/brokers/ibkr/test_ibkr_performance_service.py tests/scheduler/test_refresh_reliability.py -q
python -m pytest tests -q
node --check frontend/public/assets/js/sync_center.js
```

另验证 Ruff、既有 mypy、三镜像构建、断网导入/日历、隔离数据库读取以及 390px/1280px 同步中心状态。财务底稿和原文仅私有保存，不随 PR 上传。
