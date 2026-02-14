# PAMS 账户标准化说明

## 概述

根据"资产负债表.xlsx"中的账户列，统一了所有页面的账户和币种数据结构。

---

## 标准账户列表

以下是系统支持的所有账户（与资产负债表保持一致）：

| 账户名称 | 说明 | 币种 |
|---------|------|------|
| **Santander** | Santander银行账户（默认） | MXN |
| 腾讯理财通 | 腾讯理财通平台 | CNY |
| 建行 | 中国建设银行 | CNY |
| 华为账户 | 华为员工持股计划 | CNY |
| 公积金账户 | 住房公积金 | CNY |
| 币安 | Binance加密货币交易所 | USD/CNY |
| IBKR | 盈透证券 | USD/MXN |
| 广发 | 广发银行 | CNY/USD |
| 同花顺 | 同花顺证券账户 | CNY |
| 借款 | 他人借款 | CNY |
| 微信 | 微信钱包/零钱通 | CNY |

---

## 支持的币种

系统支持以下三种币种：

- **MXN** - 墨西哥比索（默认）
- **CNY** - 人民币
- **USD** - 美元

所有币种会自动换算为CNY（人民币）用于统计和分析。

---

## 默认值设置

根据用户主要使用场景，系统默认值为：
- **默认币种**: MXN（墨西哥比索）
- **默认账户**: Santander

---

## 修改内容

### 1. 数据库层 (PostgreSQL)

#### 新增文件
- `database/postgres/schema/006_update_accounts.sql` - 账户标准化SQL脚本

#### 主要变更
- 修改 `pams_transactions` 表默认值：
  - `currency` 默认值: `CNY` → **`MXN`**
  - `account` 默认值: `manual` → **`Santander`**

- 添加账户CHECK约束（验证账户名是否在标准列表中）
- 添加币种CHECK约束（限制为 CNY/USD/MXN）
- 创建 `v_transactions_cny` 视图，自动将所有币种换算为CNY

#### 汇率转换规则
```sql
-- MXN → CNY: 查询 pams_fx_rates_daily 表，默认汇率 0.35
-- USD → CNY: 查询 pams_fx_rates_daily 表，默认汇率 7.2
-- CNY → CNY: 1:1
```

### 2. 后端层 (FastAPI)

#### 修改文件
- `backend/api/endpoints/transactions.py`

#### 主要变更
```python
# TransactionCreate 模型默认值
account: Optional[str] = Field("Santander", description="账户")  # 原: "manual"
currency: str = Field("MXN", description="币种")                # 原: "CNY"
```

### 3. 前端层 (HTML + JavaScript)

#### 修改文件
- `frontend/public/cashflow.html`
- `frontend/public/assets/cashflow.js`

#### 新增功能

**1. 添加交易表单**
- 金额输入框
- 类型选择器（支出/收入）
- **币种下拉框**（MXN/CNY/USD，默认MXN）
- **账户下拉框**（11个标准账户，默认Santander）
- 分类输入框
- 备注输入框
- 添加按钮

**2. 交易记录表格增强**
- 新增"币种"列
- 账户列显示中文名称
- 支持回车键快速提交

**3. JavaScript 新增函数**
```javascript
addTransaction()          // 添加交易记录
showAddTxMessage()        // 显示添加结果消息
```

---

## 数据库迁移步骤

### 方法1：Docker exec（推荐）

```bash
# 1. 进入PostgreSQL容器
docker exec -it pams-postgres psql -U pams -d pams

# 2. 执行迁移脚本
\i /docker-entrypoint-initdb.d/006_update_accounts.sql

# 3. 验证约束
\d pams_transactions

# 4. 退出
\q
```

### 方法2：直接运行SQL文件

```bash
docker exec -i pams-postgres psql -U pams -d pams < database/postgres/schema/006_update_accounts.sql
```

### 方法3：手动执行（如有已存在数据）

```sql
-- 1. 先更新现有数据（如果有的话）
UPDATE pams_transactions SET account = 'Santander' WHERE account = 'manual';
UPDATE pams_transactions SET account = 'Santander' WHERE account = 'santander';
UPDATE pams_transactions SET account = '微信' WHERE account = 'wechat';

-- 2. 修改默认值
ALTER TABLE pams_transactions ALTER COLUMN currency SET DEFAULT 'MXN';
ALTER TABLE pams_transactions ALTER COLUMN account SET DEFAULT 'Santander';

-- 3. 添加约束（可选，如果数据已规范）
ALTER TABLE pams_transactions ADD CONSTRAINT chk_account_valid
  CHECK (account IN (
    '腾讯理财通', '建行', '华为账户', '公积金账户', '币安',
    'IBKR', '广发', 'Santander', '同花顺', '借款', '微信'
  ));

ALTER TABLE pams_transactions ADD CONSTRAINT chk_currency_valid
  CHECK (currency IN ('CNY', 'USD', 'MXN'));
```

---

## 使用示例

### 添加交易（前端）

1. 访问收支页面：http://localhost:8080/cashflow.html
2. 在"添加交易"卡片中填写：
   - 金额：100
   - 类型：支出
   - **币种：MXN**（默认已选）
   - **账户：Santander**（默认已选）
   - 分类：餐饮
   - 备注：午餐
3. 点击"添加交易"按钮
4. 页面自动刷新，显示新增的交易记录

### 添加交易（API）

```bash
curl -X POST http://localhost:8090/api/transactions \
  -H "Content-Type: application/json" \
  -d '{
    "amount": -100,
    "currency": "MXN",
    "account": "Santander",
    "category": "餐饮",
    "description": "午餐"
  }'
```

### 查询月度统计（自动换算为CNY）

```bash
curl http://localhost:8090/api/transactions/monthly/2026-02
```

返回示例：
```json
{
  "month": "2026-02",
  "total_income": 5000.0,
  "total_expense": 2569.5,
  "net": 2430.5,
  "by_category": [
    {"category": "餐饮", "amount": 1500.0, "count": 25},
    {"category": "交通", "amount": 500.0, "count": 15}
  ]
}
```

---

## 汇率数据管理

### 查询汇率表

```sql
SELECT * FROM pams_fx_rates_daily
WHERE pair IN ('MXN/CNY', 'USD/CNY')
ORDER BY as_of DESC
LIMIT 10;
```

### 手动插入汇率（如果自动获取失败）

```sql
INSERT INTO pams_fx_rates_daily (as_of, pair, rate, source)
VALUES
  ('2026-02-15', 'MXN/CNY', 0.42, 'manual'),
  ('2026-02-15', 'USD/CNY', 7.25, 'manual')
ON CONFLICT (as_of, pair) DO UPDATE SET rate = EXCLUDED.rate;
```

---

## 验证清单

部署完成后，请验证以下项目：

- [ ] 前端收支页面正常加载
- [ ] "添加交易"表单显示币种和账户下拉框
- [ ] 币种默认值为 MXN
- [ ] 账户默认值为 Santander
- [ ] 可以成功添加交易记录
- [ ] 交易记录表格显示币种列
- [ ] 月度统计数据正常显示
- [ ] 按月趋势图正常渲染
- [ ] API 返回数据包含 currency 和 account 字段

---

## 故障排查

### 问题1：添加交易失败，提示账户无效

**原因**：数据库添加了CHECK约束，但账户名不在标准列表中

**解决**：
1. 检查前端下拉框选项是否正确
2. 确认后端默认值为 "Santander" 而非 "manual"
3. 如需临时禁用约束：
   ```sql
   ALTER TABLE pams_transactions DROP CONSTRAINT chk_account_valid;
   ```

### 问题2：汇率换算结果为0

**原因**：`pams_fx_rates_daily` 表中没有对应日期的汇率数据

**解决**：
1. 插入默认汇率（见上文）
2. 或者配置自动汇率获取任务

### 问题3：前端页面显示错误

**原因**：Docker容器未重启，旧代码仍在运行

**解决**：
```bash
docker compose -f config/docker/compose.yaml restart pams-web pams-api
```

---

## 未来扩展

### 1. 账户管理页面
- 查看所有账户及余额
- 添加/编辑/删除账户
- 账户转账记录

### 2. 汇率自动更新
- 定时任务从 Frankfurter API 获取汇率
- 支持更多币种（EUR, GBP, JPY等）
- 汇率变动预警

### 3. 预算管理
- 按账户设置预算
- 按币种分别统计
- 多币种预算转换

### 4. 数据导入
- 从资产负债表.xlsx批量导入
- 银行账单CSV导入
- 微信/支付宝账单解析

---

**更新日期**: 2026-02-15
**版本**: v1.1
**作者**: Claude Code + hzfzzzi
