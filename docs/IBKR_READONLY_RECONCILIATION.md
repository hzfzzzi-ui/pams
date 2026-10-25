# IBKR 只读对账契约

本模块回答“已发布官方原文与现有入账记录是否一致”，不产生新的资产或收益事实。

## 输入与责任

- `ibkr_report_state.select_report`：唯一发布选源、原文 SHA256、业务日和 legacy/过期状态。审计不得扫描文件 mtime 另选来源。
- 原 `parse_flex_report`：沿用既有交易/持仓解析，不修改算法；审计额外检查原文必需字段，防止缺值被解析默认值掩盖。
- `pams_ibkr_trades`：执行编号、源键、原币成交额/手续费/净现金及方向。
- `pams_investment_trade_fact`：相同执行的 canonical fact；金额形态沿用原口径（成交额/费用取绝对值，净现金保留官方符号）。
- `pams_ibkr_position_history`、`pams_holdings_snapshot`：报告日证券份额与当前快照。现金行、价格、市值、成本和归属比例不参与份额对账。

## 比较边界

1. 交易优先用来源账户和 `trade_id`，其次 `transaction_id`；缺执行编号的哈希回退明确需核准。原文或入账身份重复不静默去重。
2. fact 以 `ibkr:<source_trade_key>` 定位同一执行，再核对物理 `source_id`。历史导入若重写物理主键，不拿同 ID 的另一笔交易比较金额；源键可比时仍可查金额，但来源关联问题保留，不修复。
3. 只比较同账户、日期、标的与币种；手续费声明不同币种时仅费用不可比，不能直接相减或自行换汇。净现金方向以原文 netCash 为准，不用买卖方向反推。
4. 份额只比较报告日持仓历史；当前快照日期不一致则跳过。盘中价格不与官方收盘值比较，不重算 NAV、FX、收益或持仓成本。
5. 缺字段/无效数值不补零；来源不唯一、legacy、过期、缺表、缺交易节明确待核准。只核对原文列出的执行，未覆盖的入账不自动认定为错单。
6. 原币金额及份额容差为 `0.00000001`，用于既有 float 解析与 NUMERIC(28,10) 存储间的数值对照，不是人民币会计阈值或币种换算规则。`delta = actual - expected`，原值与容差以十进制字符串输出。

## 输出

- `GET /api/ibkr/reconciliation`：只读结果、发布证据/哈希、范围、5 组检查、状态和差额。
- 每组包含比较数、总提示数、完整原因计数和最多 30 条证据样本；截断时 `truncated=true`，总数不受截断影响。
- `ok` 仅表示当前可比范围一致；`review` 表示资料、时点或来源关联待核准；`error` 表示可比字段差异、确定缺漏/重复或读取失败。不能把同步 success 当成已对账一致。
- 现有同步中心仅追加摘要与结构化结果；有效报告但审计非 ok 时显示审计状态，原过期/缺报告状态不被覆盖。没有新页面、布局或前端财务计算。

## 无写入保证

独立连接在首个查询前设置 PostgreSQL `REPEATABLE READ READ ONLY`；有 5 秒单条 SQL 超时并关闭连接。不调用 ensure/DDL、刷新、回填、修账、审计入库或原文写入。读取后再次核验内容哈希；失败只返回安全错误类型，不输出 DSN、凭据或错误正文。

验证使用独立 PostgreSQL：服务器拒绝注入的 UPDATE、所有业务/审计表与原文前后相同；覆盖稳定源键与物理 ID 错配、重复/漏单、费用和净现金、跨币种/日期、缺值、现金空仓及 legacy 场景。生产资产修正须另行确认，不能借本模块自动执行。

## 跨币种费用核准子结果

成功读源后，同一只读事务取得的输入供 `ibkr_fee_review.review_fees` 使用，原 GET 仅追加 `fee_review` 子对象。旧 5 组检查、计数、状态与差额不变，来源关联/费用币种阻断不解除；同步中心和前端不新增消费或计算逻辑。缺报告/缺表等提前返回时该子对象可以不存在。

- 输入：同一已发布 XML、质量状态、原始交易及 canonical fact。只按来源账户/执行编号和稳定键匹配，不能用可复用的物理 ID 错配费用。
- 范围：币种不同、币种缺失或原始费用无效的记录；明确零额与缺失值分开。`total`、完整汇总与最多 100 条样本分开，`truncated` 明示样本截断。
- 数值：原文佣金保留正负，fact 费用按既有绝对值口径核对；同时检查净现金和数量。数值一致不等于 fee_amount 的币种已独立建模，币种仅存于原始/内嵌 JSON 时仍 `review`，不能把告警清零作为目标。
- 现金：以原文明确的账户本位币及官方 `fxRateToBase`，核对同日全部 Trade 佣金与唯一日频 `BASE_SUMMARY.commissions`。不是只查费用列示币种的现金行，不使用当前汇率；缺字段/第三币种缺换算依据/重复或非日频现金表均不能确认。
- 上述现金证据只到每日汇总，`individual_allocation_verified=false`；不能据此分配到单笔结算、判定所有历史交易完整或核准全账户 NAV。FX `netCash=0` 本身不是漏扣费用证据。
- 路径追踪：现金滚动读取 net_cash_cny/net_cash，不再单独减 fee_amount；IBKR 股票/ETF 重构受既有市场筛选约束，CASH 换汇记录不进入该重构。报告列出实际记录 flags 与市场，不把排除路径说成所有成本/收益均已核准。
- 官方账户表现读取报告现金余额和证券市值，并非再次从 fact 扣佣金；`individual_fee_effect_on_nav_or_pnl=null`，不伪造每笔费用对当前 NAV 的增减。
- 新服务是纯输入/输出函数，不持有连接、无 ensure/DDL、写入、外部行情或修复功能。异常仅输出安全类型；旧字段不能因新增诊断失败而更改。

官方字段参考：[Trades Flex](https://www.ibkrguides.com/reportingreference/reportguide/tradesfq.htm)、[Cash Report Flex](https://www.ibkrguides.com/reportingreference/reportguide/cash%20reportfq.htm)。按原文字段及账户/日期核对，不用通用说明覆盖原文实际的费用币种或现金扣收币种。

### 费用单位的只读取证结果

`fee_review.records[].unit_review` 与 `summary.source_unit_verified_rows` 为向后兼容追加字段，旧检查/状态/计数/警告不改变，不被收益或 NAV 服务消费。

- `source_unit_verified` 仅表示官方原始费币、当前来源及不可变内嵌证据的身份/费币/数值一致，fact 稳定键/来源哈希、原始方向及全部内嵌金融字段均可核对；缺值、重复、冲突或非有限值保持 `unverified`，不补零。显式零费用也须通过相同证据检查。
- `read_model` 仅提供现存 fact 费用数值的原字符串和有据可查的原币种，来源为 `official_and_immutable_embedded_raw`；不换汇、不改写 fact、不推导当前汇率。证据不足时该对象为 null，不能从交易币种猜测费币。
- 未迁移旧库仍返回 `fact_fee_currency_persisted=false` / `currency_model=not_modeled`；可空字段存在后，另区分未记录与显式持久值。`unit_review.fact_currency_check` 核对存储值，`summary.fact_fee_currency_matched_rows` 统计匹配数；有非 NULL 值不代表正确。详见 [手续费币种契约](IBKR_FEE_CURRENCY.md)。
- `individual_cash_settlement=unverified` 独立于单位证据、持久币种和日级汇总是否相等。数值/单位匹配不代表费用已在某条现金腿扣收；旧 schema 的原费用结果除明确新增字段外保持相同，不清除来源保护。
- `additional_deduction_proposed=false` / `executable=false`，不新增现金扣款、成本调整或生产修账入口；原 `fee_currency_not_modeled` 问题保留。与来源关联投影的历史解释一起用于审阅，不是全账可信或实际修复验收。
