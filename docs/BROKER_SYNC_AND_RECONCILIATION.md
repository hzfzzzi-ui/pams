# 券商同步与对账

本主题区分生产 Flex 同步、独立日结证据发布、事实处理、只读对账和独立账户审计。各链路成功互不替代；诊断结果不写金额、不解除原阻断。资金事实门禁见 [导入与事实写入](IMPORT_AND_FACTS.md)，财务公式见 [数据与收益](DATA_AND_RETURNS.md)。

## 报告选择与业务日期

ibkr_report_state.select_report 以已提交 IBKR success 审计、实际报告日期及审计 ID 选源，返回有效原件、业务日期、SHA256、质量状态；不按 mtime 扫描任意文件、不调用 endpoint 或金额引擎。

新记录核对原文字节哈希，损坏/缺失时尝试上一有效记录并提示；数据库不可用不能绕过发布状态。已验证新发布优先于旧 success，只有新发布原文全部不可用才回看历史兼容源。旧记录无哈希时检查现存原件并标 legacy_unverified，不追认其不可变。沿用旧值、缺报告、过期与历史待核准分别返回。

collector 接受原凭据、XML 和可选验证参考日，凭据不进入元数据。候选 XML 先唯一临时文件、flush/fsync、原子改名；文件存在不代表发布。校验 BASE_SUMMARY 现金、报告与持仓日期、关键数字和单一来源账户，取得事务锁后重读发布日期高水位；拒绝倒退、过期、未收市/未来日期，坏文件不降低水位。

业务写入、事实补录和 success 发布审计同事务；失败回滚并独立记 failed，政策拦截记 blocked 且零业务写入。summary_json.publication 保留 version、state、report_date、expected_date、SHA256、写入模式/去重指纹和本地账户代码。相同字节且写入模式相同仅 unchanged，不移动指针；普通 Flex 原文生成时间改变仍可能重写，不承诺语义去重。

报告日期是来源事实。as_of / align_to_latest_snapshot 字段仍可接受，但目标日期必须与实际报告日相同，异日请求在写业务前拒绝；有效同日行为保持，拒绝时保留有效发布与事实，可另有失败审计。当前投影和历史记录不能共享可变字典，写入器验证每行日期且不修改调用者输入。

此日期规则来自现有 [collector](../backend/api/collectors/ibkr_flex.py) 的 _target_as_of 与 [日期契约回归](../tests/brokers/ibkr/test_ibkr_snapshot_dates.py)。异日导入不得改写来源业务日。

## 日历与同步确认

expected_statement_reference 由 API 按交易日规范化并在写入前检查，expected_statement_as_of 用于后置核对，不能用 as_of 重标来源日期。使用 exchange-calendars==4.13.2 的离线 XNYS，下限默认纽约 16:30，包含夏令时及半日市；日历越界失败、不猜日期。全球账户在美国休市但当日截止已过时可有有效报告，不能一概拒绝，也不能提前确认尚未结束的日期。

performance/风控共用选源，原解析与旧字段保留，质量追加 report_quality。缺有效报告时 performance 返回 404，风控保持缺失数据核准，不补零。同步中心被拦截的 HTTP 200 不能显示已刷新；审计非 ok 时展示审计状态，但保留旧报告过期/缺失提示。

同日报告修订仍可能导入，不代表已解决既有 fact 订正、跨年 YTD、归属或空账户零余额的所有限制。完整历史核准另行处理。

## 独立日结发布与自动任务

ibkr_daily_close 解析并证明，ibkr_daily_close_read 读取明确发布的补充证据，performance、portfolio_state、valuation_nav 复用同一证明。scripts/ibkr_daily_close.py 默认校验指定 XML；显式 --publish-dir 仅写不可变原文及独立 manifest，不连接数据库、不导入交易或入金。可显式下载独立审计查询并复用 Token，不修改生产查询，审计原文不能送进普通交易导入器。

manifest 限定用途、账户映射、源账号、日期、SHA256 和同目录原文；消费者仅在配置 PAMS_IBKR_DAILY_CLOSE_MANIFEST 后启用，逐次验证字节，缓存按内容身份受限。损坏、跨账户、范围不符保留未确认，不静默覆盖。

| 开关或接口 | 契约 |
| --- | --- |
| PAMS_IBKR_DAILY_CLOSE_QUERY_ID | 独立数字查询 ID，必须不同于生产查询，默认关闭 |
| PAMS_IBKR_DAILY_CLOSE_SYNC_ENABLED | scheduler 默认 false，获准部署后启用 |
| GET /api/ibkr/daily-close/status | 只读，不下载或写事实 |
| POST /api/ibkr/daily-close/sync | 获取并发布证据，不接受重标日期或对齐估值 |

日结自动发布复用 30 分钟调度、OS 互斥、持久意图、退避、重试后再读确认和失联响应恢复，使用独立 ibkr_daily_close 状态，不清除其他市场历史缺口。这里的证据发布器保持 facts_written=0，逐笔现金由独立链路处理。

同日已确认返回 unchanged，不因生成时间替换原文。新日须同账户、完整日结、期间桥接、生产现金/持仓组成吻合且源字节未变，并覆盖已发布历史而不改写它。失败保留前一指针；修订触及历史时阻断，不借自动化越过修账审批。旧前端兼容须在启用前核准，避免展示重算覆盖共享后端结果。

实际查询范围和 Token 见私有配置。缺 CashTransaction 的报告不能靠新解析分支证明资金闭环；独立日结查询包含事件也不表示事实已处理。

## 只读对账

GET /api/ibkr/reconciliation 比较官方原文与现有入账。输入是同一选源/哈希/质量、原 parse_flex_report、pams_ibkr_trades、canonical pams_investment_trade_fact、报告日持仓历史和快照。

- 来源账户 + trade_id 优先，其次 transaction_id；缺执行号的哈希回退待核准，重复身份不静默去重。
- fact 以 ibkr:<source_trade_key> 定位，再核物理 source_id。ID 被复用时不比较同 ID 的另一笔交易；稳定键可比仍保留来源错配。
- 同账户、日期、标的、币种才比较。fact 成交额/费用沿用绝对值，netCash 保留官方符号，不按买卖反推。
- 份额只比报告日历史；日期不符跳过，盘中价不与官方收盘比。现金、价格、市值、成本和归属不进入份额核对。
- 缺字段/节、无效数字、legacy、过期、缺表和不唯一来源待核准；未被报告覆盖的入账不自动判错。
- 原币金额/份额绝对容差 0.00000001，delta=actual−expected，以十进制字符串返回；这不是人民币会计阈值或 FX 规则。

输出保留原五组检查、比较数、完整原因计数和最多 30 条样本，truncated 明示截断。ok 仅限可比范围；review 表示证据或关联待核；error 表示确定差异、漏/重复或读取失败。同步 success 不等于对账 ok。

只读适配器首个 SQL 前强制 PostgreSQL REPEATABLE READ READ ONLY，单条 SQL 5 秒超时，读取后再次验证哈希并关闭连接。不 ensure/DDL、刷新、回填、修复、插审计或改原件；异常仅安全类型，不输出 DSN/凭据/原错误。

## 独立账户证据审计

domain.brokers.ibkr.ibkr_account_audit.audit_ibkr_account 是纯函数，无数据库、文件、网络、时钟、collector、refresh、schema、修复或交易调用，也不导入被审金额函数。调用者负责选择已提交原件、原字节哈希及未归一换行的解码；函数只能核传入哈希，不能认证券商签发或发布真实性，publication_verified=false。

参数包括 xml_text、expected_sha256、官方 account_id、精确 as_of 关闭日期、官方 base_currency、qualified internal_state，可选 period_start、history_dates 和 quality。账户别名 ibkr 不等于官方身份。规范化内部状态必须有来源证明，不能仅给旧响应贴标签以使其可比：

| 输入字段 | 必须表达的语义 |
| --- | --- |
| account_id / as_of / base_currency | 官方账户、精确收盘日、AccountInformation 本位币 |
| ownership_basis / valuation_basis | full_account / ibkr_statement_close，未乘个人归属、非实时混日估值 |
| cash_base / 可选 cash_by_currency | cash_basis=ending_cash，交易日现金，不是 settled cash |
| position_value_base / 可选 positions | position_basis=signed_market_value，有符号净持仓市值，不是 gross 敞口 |
| net_liquidation_base | nav_basis=official_nav_total，包含应计等全部 NAV |
| fx_rates_to_base | fx_basis=statement_date_to_base，同日官方汇率 |
| 可选 period | basis=nav_change_less_external_flows，精确 from_date/to_date、opening_nav_base、net_external_flow_base、closing_nav_base、pnl_base |

每个内部持仓有字符串 conid、currency、quantity、原币 position_value；持仓与 period 继承共同账户/日期/归属/估值范围，冲突覆盖拒绝。缺标签或语义不支持返回 review 并跳过受影响比较。

### 账户证据门禁

1. 精确选择一个账户与关闭日的 FlexStatement。所有消费节点（含 ConversionRate）、节、statement、自定义 wrapper 的整条祖先链均核账户与 model；匹配叶子不能覆盖父层冲突，空 OpenPositions 也先过范围门禁。
2. AccountInformation 显式给本位币，不默认 USD。原币、FX 两侧及内部 currency-map 键必须严格 ASCII [A-Z]{3}，不 trim、大写、查注册表或填默认；BASE_SUMMARY 只是现金汇总标记，不能当原币。无效配对阻断受影响换算；独立官方本位汇总不依赖无效细节重建。
3. 现金取有符号 CashReportCurrency.endingCash 的唯一有效 BASE_SUMMARY，或全部原币行按同日官方 FX 换算；已经存在但无效/含糊的汇总不能退回原币凑结果。原币与本位汇总不相加，负现金已含融资。原币回退须覆盖所选 statement 中观察到的每个币种，旧/未来/缺日币种保留原因；只有同币已有充分当前证据才可忽略旧观察。币种覆盖与 FX 完整性分开。
4. 持仓必须有 OpenPositions 与稳定 contract/currency 身份、明确数量/金额的 SUMMARY。LOT 不与 SUMMARY 相加，只有 LOT 的标的阻断汇总。显式空节可证明零，缺节未知。有符号 positionValue 直接使用，不重构期权乘数或空头符号；完整数据匹配详情，不拿截断样本核全部。
5. FX 只用显式 fxRateToBase 或精确同日直接 ConversionRate，不倒数、不前填、不取当前，基币自身严格 1。缺失、矛盾或现金/证券跨节汇率冲突使依赖值 null、可用映射去除该率；独立 BASE_SUMMARY/NAV 仍可比真实差额。内部率必须正、基币自身为 1 后再判容差；语义非法不是小额成功。独立本位汇总不由无效内部明细键推断。
6. NAV 必须为同账户/日 EquitySummaryByReportDateInBase.total，现金＋证券不证明含应计、衍生品和其他成分的完整 NAV。
7. 期间须精确 ChangeInNAV，显式 startingValue、endingValue，以及 depositsWithdrawals、internalCashTransfers、assetTransfers、linkingAdjustments、debitCardActivity、billPay（含明确零）。未知转账/链接变体拒绝，流量保留符号；独立关闭 NAV 冲突阻断期间比较。YTD、股票净值、已实现成交盈亏、期初现金均不可替代。不认证百分比、TWR/MWR 或成立以来收益。
8. history_coverage 永远 unverified：分别报告观察到及缺失的请求日。每天有 statement 也不证明整条收益曲线，外层 from/to 不能当逐日覆盖。

### 账户审计输出与适配

金融值、差额、率均有限 Decimal 字符串或 null，容差 0.00000001。全部输入均检查，只有 issue/detail/date 展示最多 30 条并报完整计数/截断。XML 上限 32 MiB，拒绝 DTD/entities，支持 namespace；数字表示有资源界限而非业务金额白名单，独立 Decimal context 精度 160、指数 -512..512，不继承调用者舍入/traps。

ok 仅为指定可比项通过，缺详情仍 detail_comparison=not_supplied；review 包含 missing_basis、semantic_mismatch、missing_historical_fx、period_evidence_incomplete 等；error 包含有限可比金额差异或 XML/哈希完整性错误。单项 review 不能抹掉别项 error。始终 read_only=true、executable=false，因历史未认证总状态保持 review，除非出现 error。

GET /api/ibkr/account-audit 可带 ISO period_start；在同一个服务器强制只读重复读事务选原件、subject 解析前后核原字节哈希、读取历史日期。一个文档可有多个日 statement，但唯一精确关闭范围，兄弟节/祖先外当前行不能混入。原 performance 解析器是审计对象；include_display_fx 默认 True 保留旧响应，适配器用 False 避免无关当前 CNY 请求。其旧金融函数体保持回归指纹，仅 display opt-out 是允许差异。

subject 现金/有符号持仓可比较，subject NAV 明确 cash_plus_positions_not_official_nav_total；YTD 不贴成受审期间。缺独立 NAV、历史 FX、期间或历史仍 review。独立证据不替代现有响应，也不清除费用和来源阻断。

## 费用的三个证据层次

### 来源单位与持久字段

review_fees 在同一只读事务输入上追加 fee_review，保留旧五组、计数、状态、差额和阻断。缺报告/缺表提前返回可没有子对象。其范围是跨币种、缺币种、无效原佣金；明确零与缺失分开，完整汇总和最多 100 条样本分别返回。

原文佣金保留符号，fact 按既有费用绝对值核对，另核净现金和数量。现金滚动已读 net_cash_cny/net_cash，不再减 fee_amount；股票/ETF 重构受既有市场筛选，CASH 换汇不进入该重构。官方账户表现取报告现金和证券，不从 fact 再扣费，individual_fee_effect_on_nav_or_pnl=null。

source_unit_verified 要求官方、来源、不可变内嵌的账户/执行身份、单位、数值、稳定键/源哈希、方向及全部内嵌金融字段一致；缺失/重复/冲突/非有限保持 unverified，零也需证据。read_model 仅返回原费用字符串及可证币种，来源 official_and_immutable_embedded_raw，不换汇或改 fact。

071_investment_trade_fee_currency.sql 仅增加 nullable varchar(3)，无默认、历史 UPDATE 或触发器。ibkr_fee_currency 只在原始 IBKR JSON 有唯一显式 ibCommissionCurrency/ibCommission、有限佣金绝对值与原 fee_amount 精确相同时保存；源单位格式允许 trim/大写 ASCII 三字母，与上面账户审计的严格输入门禁不同。大小写重复属性、非 ASCII、缺失/无效佣金、金额冲突保持 NULL；不从交易/账户币种、symbol 或 FX 猜单位，不声称完整币种注册表核准。

明确零可有明确单位，旧解析缺佣金默认 0 不算证据。手动和 TradingView 保持 NULL，不扩展表单；符号、净现金及原费用算法不变。

| schema / 字段状态 | 输出语义 |
| --- | --- |
| 无 fee_currency 列 | 插入前一次只读能力检查，沿用旧 SQL，raw 保留证据；column_unavailable、fact_fee_currency_persisted=false、currency_model=not_modeled |
| 列存在但 NULL | missing、nullable_fact_currency_not_recorded |
| 非规范持久值 / 来源不足 / 冲突 | invalid / unverified / mismatch，各自独立展示 |
| 完整来源与持久币种一致 | matched，仅核该字段；explicit_fact_currency 不等于结算已证 |

新列存在仅让新且有证据的 IBKR fact 带单位。来源重复仍 ON CONFLICT DO NOTHING，不 UPDATE 原 fact、不补历史币种或时间戳；旧代码/非 IBKR 缺字段仍可插。071 不依赖 070，生产 schema、代码发布及历史补充各自批准，回退旧代码不要求删列或清元数据。

unit_review.fact_currency_check、summary.fact_fee_currency_matched_rows 表达持久值检查，fact_fee_currency_persisted 只指非 NULL。旧 fee_unit_evidence 枚举保留兼容，名称不再用于推断 schema；source_unit_verified 与持久值冲突可同时存在。匹配最多消除对应模型缺口，不能消除现金结算和来源警告。

### 日汇总现金证据

同日全部 Trade 佣金按明确本位币及官方 fxRateToBase，与唯一日频 BASE_SUMMARY.commissions 比较；不是只看费用列示币种行，也不用当前 FX。缺字段、第三币种无换算依据、重复或非日频现金表不能确认。日汇总相等仍 individual_allocation_verified=false，不能分到单笔或认证全历史/NAV。FX netCash=0 本身不证明漏费。

### 单笔结算证据

ibkr_fee_settlement.review_fee_settlement 只读官方 XML、纯函数且不修改输入。review_fees 仅追加 settlement_evidence；异常成为安全、有限的 unavailable 子结果，不替代旧检查。按 source_account_id + execution_id（trade_id:...，其次 transaction_id:...）关联，不能按列表位置或 symbol。它不证明已存 trades/facts 与 XML 一致。

调用者负责已发布原字节哈希及质量；report_sha256 只绑定传入 UTF-8 文本，去 BOM/归一化后不必等于原字节哈希。funds_line_indices 是全文 StatementOfFundsLine 的零起序号；trade_evidence_period/funds_evidence_periods 保留各祖先期间交集与行号，未知期间 null，不能拿外层报表跨度替代。

- 要求当前质量、应有/实际日期一致，using_previous 拒绝；每个 statement 显式有序 from/to，同一账户多 statement 期间不能重叠，报表 as_of 为最大明确结束日。
- 消费行及整条父链核账户/别名/model，含空容器和 AccountInformation。容器期间只能缩小，Trade.tradeDate 和资金行 date 在自己的有效期间内；兄弟 statement、gap、reportDate 不能填补业务日。
- 行级过期/无效/期间空档隔离受影响行并保留全部身份参与重复检测；唯一关联坏资金行也阻断该交易，零佣金不例外。账户/model/账户信息矛盾、statement 期间非法或发布失败是全局关闭门禁，total 未知；不能过滤坏行挑方便匹配。
- 每 statement 必须有 Trades：缺节 total=null，全部明确空节才 total=0/explicit_empty_trade_section，非空未知内容不能冒充空。
- 账户+执行号唯一，所有次级身份一致，重复执行/复用次号/资金一行配多笔或一笔多行均冲突，不取首条。缺叶账户保留候选身份但不证明原币结算。
- 必须显式币种、有限有符号 Decimal 费用、执行明细、工具/方向/活动语义、交易/结算日；不采用解析默认、近似日期或 FX。结算日晚于 as_of 或关联资金行自身期间末，不能用较晚空 statement 推进证明。

| classification | 充分证据范围 |
| --- | --- |
| included | 唯一同币种非 FX 资金行；同符号佣金，amount=debit+credit=gross+commission+tax，现金腿符号、官方 proceeds/taxes/netCash 和本金方向一致 |
| separate | 唯一关联原费币佣金行，gross/tax 明确为零，amount 等于有符号佣金，现金检查相同；普通 ADJ 仅此形态可用，OFEE/描述/金额相近不够 |
| explicit_zero | 官方明确零只证明费用值，不证明结算；关联佣金冲突拒绝。双方佣金明确零而现金组成缺失可仅保留零费用充分，cash_evidence_reason 仍记录缺口 |

缺失的非活动 debit/credit 不能默认零。FX/FOREX 即使显示本金零也不是费专用行，跨币种翻译本金及 BaseCurrency 行不证明非零原币结算；只有唯一原币 Currency ADJ 的明确佣金专用形态可证明 separate。日 CashReport 不用于分配单笔证据。精确 Decimal 不用 float/容差或调用者精度，需要超过 4096 位运算的范围保持未核准。

结果记录 sufficient/unverified/conflict、classification、machine reasons、evidence_scope 和独立 individual_cash_settlement_verified；sufficient 仅限该证据范围。report_component_arithmetic 是较窄的金额算术检查，不能代替现金腿。全报告范围充分仍 review，范围缺失/无效为 unverified，执行/扣款标志始终 false。

trade_scope_diagnostics / funds_scope_diagnostics 报完整行/未知行/原因数，最多 30 条 issue 和截断；关联交易另有 funds_scope_issues 全计数/有限详情。无 index 的空容器 issue 不增加未知行数；全局拒绝后的未评估零 issue 不代表可用。total 为官方 Trade 行数，含隔离行，不是已认证结算数。无关坏资金行不拖垮其他充分执行，关联坏行保持阻断。

## 覆盖与验证边界

费用单位、持久字段、日汇总及单笔现金证据互不替代。相关交易链、带本金调整、多现金腿、迟报、模糊字段保持未核准；严格规则可能拒绝业务上看似合理的缺空白腿或迟记账行。原币报表证据不证明银行实际清算、费用入库、全历史、NAV 或盈亏。additional_deduction_proposed=false、executable=false，数值/单位一致不额外扣费。

代码验证覆盖固定输入完整输出、账户/日期/祖先范围、缺节与空节、重复/次身份、单位/符号/零/缺失、尾部样本、精度、原文变化及只读服务器拒写。071 的隔离验证仅排除新增列比较所有原列，不能声称 schema/全行哈希未变；历史样本数不是运行规则。已有净现金缺失推导分支和金融读者不借元数据字段改金额。

正常业务就绪需来源可取、任务自动运行、报告日期推进、官方事件幂等入账、错误在后台状态；全历史财务核准独立。长期无人值守还需连续 7 个美股交易日实测，部署当天不能登记，主机常开及 Token/源方时点是外部依赖。测试、部署和验收分别查对应 GitHub PR；既往结果不能作为当前运行绿灯。

## 官方字段参考

[现金报告](https://www.ibkrguides.com/reportingreference/reportguide/cashreport.htm)、[NAV 成分](https://www.ibkrguides.com/reportingreference/reportguide/netassetvalue_default.htm)、[Change in NAV](https://www.ibkrguides.com/reportingreference/reportguide/changeinnav_fq.htm)、[Trades Flex](https://www.ibkrguides.com/reportingreference/reportguide/tradesfq.htm)、[Cash Report Flex](https://www.ibkrguides.com/reportingreference/reportguide/cash%20reportfq.htm)、[Statement of Funds Flex](https://www.ibkrguides.com/reportingreference/reportguide/statement%20of%20fundsfq.htm)。具体证据以原文字段、账户、日期和单位为准，不用通用说明覆盖原文。本次只整理已有引用，未联网复核。
