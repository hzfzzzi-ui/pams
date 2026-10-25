# IBKR 手续费币种显式契约

## 范围与责任

本次只给现有 `fee_amount` 增加可空的 `fee_currency` 元数据，不重算金额。
生产只读，本轮不迁移、不回填、不修账、不发布；历史 10 笔只在隔离副本验证。
不改变金额、份额、成本、现金、收益率、NAV、来源关联、原始 JSON 或页面。

| 模块 | 输入 | 输出与边界 |
| --- | --- | --- |
| 原 Flex 解析/来源表 | 已发布原文 | 保持原解析、源键、佣金及原始属性不变，不采用解析器的默认交易币种作为费币 |
| `ibkr_fee_currency` | 来源类型、原始 `ibCommissionCurrency`/`ibCommission`、现有费用数值 | 只验证显式单位；不查行情/汇率，不计算现金或收益 |
| `investment_trade_fact` | 原来源行 | 附加规范化费币；插入时检查字段是否存在，仍 `ON CONFLICT DO NOTHING` |
| `ibkr_fee_review` | 官方/来源/历史内嵌证据及现有 fact | 分别报告来源单位、fact 持久值、单笔现金结算；只读、不额外扣费 |

## 字段规则

- 迁移 `071_investment_trade_fee_currency.sql` 仅新增 nullable `varchar(3)`，无默认币种、历史 UPDATE 或触发器。
- 只有 IBKR 原始 JSON 明确提供唯一费用币种属性、明确有限佣金且绝对数值与现有 `fee_amount` 精确相同，才保存单位；不从交易币种、账户本位币、symbol、当前 FX 或外部默认值猜测。
- 币种是源方明确的三位 ASCII 字母代码，去空白并大写；非 ASCII、重复大小写属性、无效/缺失佣金、金额冲突均保持 NULL。这里只做格式和证据验证，不冒充完整外部币种注册表核准。
- 明确零费用可以有明确币种；缺费用不是明确零，不能因旧解析默认 0 而补单位。手续费正负、金额绝对值及净现金沿用原规范，不借本字段纠正旧金额算法。
- 手动交易及 TradingView 暂不推断费币，保持 NULL；本 PR 不扩展其录入表单或分类契约。

## 新旧数据库兼容

1. 不存在字段时，批量插入只读检查一次能力，继续原 SQL 字段集，并记录不含凭据/金额的迁移提示；币种证据仍在 raw JSON。没有运行时 DDL。
2. 字段存在时，新 IBKR fact 才保存可证实的单位，未知仍 NULL。字段未提供的旧代码/非 IBKR 写入仍可插入。
3. 重复来源仍跳过整个 fact，**不 UPDATE 已有行**，不补币种或改 `updated_at`。迁移后再次同步也不会补录历史未知值。
4. 071 是独立迁移，不要求、也不能顺带执行来源修复审计表的 070。本轮两者均不在生产执行。
5. 若以后获准发布，须分别批准生产 schema、代码发布与历史补充；回退旧代码不需删除列或清空已确认元数据，任何数据恢复另批。

## 审计输出

既有 `GET /api/ibkr/reconciliation` 的费用子结果追加
`records[].unit_review.fact_currency_check` 和 `summary.fact_fee_currency_matched_rows`。
原五组对账、来源预检、金额/数值比较、每日佣金汇总及接口金融结果不变。
仍沿用原跨币种/缺单位/无效佣金的诊断范围，不声称审计了全账户所有费用或新增一套核算口径。

| 状态 | 含义 |
| --- | --- |
| `column_unavailable` | fact 没有该字段，旧 schema 未建模 |
| `missing` | 字段存在但值为 NULL，未记录 |
| `invalid` | 持久值不是规范化格式 |
| `unverified` | 有持久值，但完整来源单位证据不足 |
| `mismatch` | 完整来源单位证据与持久值冲突 |
| `matched` | 持久币种与完整来源单位证据匹配，仅限该字段 |

- `fact_fee_currency_persisted` 只表示存在非 NULL 持久值，不表示值正确；必须结合上述检查。
- `currency_model` 区分 `not_modeled`、`nullable_fact_currency_not_recorded`、`explicit_fact_currency`。
- `source_unit_verified` 仍只核准原文/来源/内嵌单位证据；即使 fact 单位冲突，也须独立展示冲突，不能混为同一个状态。
- 为兼容已发布来源规划，保留旧 `fee_unit_evidence` 字符串枚举。其名称不再作为 schema 存在与否的依据；应读取新的字段检查和持久模型状态。
- 匹配时只把费用模型缺口替换为单笔结算仍待核准的提示；`individual_cash_settlement=unverified`、`additional_deduction_proposed=false`、`executable=false` 保持。原来源错配/费用币种保护不自动解除。

## 不变量与验收

- 固定原文和数据库输入，在旧库、新列全部 NULL、隔离补充 10 笔三种状态运行候选 API；九组金融结果与原版本比较，不用动态市场值拼接。
- 新增列本身是允许的 schema 差异，不能声称新增列后整行字节哈希完全不变；剔除**仅该新增列**后，所有原始列、非 IBKR 原有数据和其他八张核心表必须相同。
- 历史 10 笔仅在隔离库按完整来源单位证据验证，补充首轮 10/重复 0，中断回滚；除该列外连时间戳、raw JSON、source_id/source_hash 都不改。没有生产历史补充工具或自动任务。
- 来源 ID 修复工具/预检的保守保护不在本轮放宽；费用单位匹配不等于现金结算、历史覆盖、全账 NAV/收益或用户验收完成。
- 原净现金缺失时的旧推导分支、交易页费用展示和收益/NAV 读者不消费新字段；跨币种缺净现金等问题仍需单独审计，不借本 PR 调整金额或额外扣费。

验证沿用 pytest 原生报告、Playwright 原生浏览器证据和 GitHub CI。
生产九表/九接口只读对照与隔离实验分开记录；前端文件不改，字体波动按用户决定暂缓处理。
