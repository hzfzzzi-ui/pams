# PAMS 账户、平台、类型映射表

## 概述

此文档定义了数据库中的账户ID、平台代码、资产类型如何映射到前端显示的中文名称。

**映射文件位置**: `frontend/public/assets/js/api.js`

---

## 📊 账户映射 (ACCOUNT_LABELS)

### 标准账户（基于资产负债表.xlsx）

| 数据库值 | 显示名称 | 说明 |
|---------|---------|------|
| `tencent_licaitong` | 腾讯理财通 | 腾讯理财通平台 |
| `licaitong` | 腾讯理财通 | 同上 |
| `main` | 腾讯理财通 | 主账户（默认映射） |
| | | |
| `ccb` | 建行 | 中国建设银行 |
| `ccb_wm47` | 建行 | 建行理财账户 |
| `ccb_gold` | 建行 | 建行黄金账户 |
| `ccb_7332` | 建行 | 建行现金账户 |
| | | |
| `huawei` | 华为账户 | 华为员工持股计划 |
| `huawei_emp` | 华为账户 | 同上 |
| `huawei_esop` | 华为账户 | 同上 |
| | | |
| `cn_hpf` | 公积金账户 | 住房公积金 |
| `hpf` | 公积金账户 | 同上 |
| | | |
| `binance` | 币安 | Binance加密货币交易所 |
| | | |
| `ibkr` | IBKR | 盈透证券 |
| `us_main` | IBKR | 美股主账户（映射到IBKR） |
| | | |
| `gf` | 广发 | 广发银行 |
| `guangfa` | 广发 | 同上 |
| `gf_7443` | 广发 | 广发信用卡 |
| `gf_9711` | 广发 | 广发信用卡 |
| | | |
| `santander` | Santander | Santander银行 |
| `santander4209` | Santander | 同上 |
| `santander4209_cash` | Santander | Santander现金账户 |
| | | |
| `tonghuashun` | 同花顺 | 同花顺证券 |
| `ths` | 同花顺 | 同上 |
| `cn_a` | 同花顺 | A股账户（映射到同花顺） |
| `cn_a_rmb` | 同花顺 | A股人民币账户 |
| | | |
| `recv_loan` | 借款 | 他人借款 |
| `loan` | 借款 | 同上 |
| | | |
| `wechat` | 微信 | 微信钱包/零钱通 |
| `weixin` | 微信 | 同上 |
| | | |
| `recv_reimburse` | 报销应收款 | 报销款项 |
| `manual` | 手动录入 | 手动输入的数据 |
| `unknown` | 未知 | 未分类账户 |

---

## 🏢 平台映射 (VENUE_LABELS)

### 交易平台/金融机构

| 数据库值 | 显示名称 | 类型 |
|---------|---------|------|
| `guosen` | 国信证券 | 券商 |
| `国信证券` | 国信证券 | - |
| | | |
| `tencent_licaitong` | 腾讯理财通 | 理财平台 |
| `licaitong` | 腾讯理财通 | - |
| `腾讯理财通` | 腾讯理财通 | - |
| | | |
| `ibkr` | 盈透证券 | 美股券商 |
| `IBKR` | 盈透证券 | - |
| `盈透证券` | 盈透证券 | - |
| | | |
| `binance` | 币安 | 加密货币交易所 |
| `Binance` | 币安 | - |
| `币安` | 币安 | - |
| | | |
| `ccb` | 建设银行 | 银行 |
| `CCB` | 建设银行 | - |
| `建行` | 建设银行 | - |
| | | |
| `santander` | Santander | 银行 |
| `Santander` | Santander | - |
| | | |
| `tonghuashun` | 同花顺 | 券商 |
| `ths` | 同花顺 | - |
| `同花顺` | 同花顺 | - |
| | | |
| `guangfa` | 广发银行 | 银行 |
| `gf` | 广发银行 | - |
| `广发` | 广发银行 | - |
| | | |
| `wechat` | 微信 | 支付平台 |
| `weixin` | 微信 | - |
| `微信` | 微信 | - |
| | | |
| `manual` | 手动录入 | - |
| `手动录入` | 手动录入 | - |
| | | |
| `Unknown` | 未知 | - |
| `unknown` | 未知 | - |
| `未知` | 未知 | - |

---

## 📁 资产类型映射 (TYPE_LABELS)

### 资产分类

| 数据库值 | 显示名称 | 说明 |
|---------|---------|------|
| `cash` | 现金 | 活期存款、现金 |
| `现金` | 现金 | - |
| | | |
| `fund` | 基金 | 公募基金、ETF |
| `基金` | 基金 | - |
| | | |
| `stock` | A股 | A股股票 |
| `stock_cn` | A股 | - |
| `A股` | A股 | - |
| | | |
| `stock_us` | 美股 | 美股股票 |
| `美股` | 美股 | - |
| | | |
| `wealth` | 理财 | 理财产品 |
| `理财` | 理财 | - |
| | | |
| `gold` | 黄金 | 黄金、贵金属 |
| `黄金` | 黄金 | - |
| `asset_gold` | 黄金 | - |
| | | |
| `crypto` | 加密 | 加密货币 |
| `加密` | 加密 | - |
| | | |
| `equity_cn` | A股权益 | A股权益类 |
| `equity_us` | 美股权益 | 美股权益类 |
| | | |
| `esop` | 员工持股 | 员工股权计划 |
| `equity_emp` | 员工持股 | - |
| | | |
| `asset_hpf` | 公积金 | 住房公积金 |
| `hpf` | 公积金 | - |
| | | |
| `receivable` | 应收款 | 应收账款 |
| `recv` | 应收款 | - |
| | | |
| `liability` | 负债 | 负债项 |
| `负债` | 负债 | - |
| | | |
| `loan` | 借款 | 借款 |
| `借款` | 借款 | - |

---

## 🔄 映射逻辑

### 前端显示流程

```javascript
// 1. 从API获取数据
const holdings = await jget('/holdings/latest');

// 2. 遍历每一行
for (const row of holdings.rows) {
  // 3. 应用映射函数
  const venueName = venueLabel(row.venue);     // 平台映射
  const accountName = accountLabel(row.account); // 账户映射
  const typeName = typeLabel(row.type);         // 类型映射

  // 4. 显示在表格中
  // ...
}
```

### 映射函数定义

```javascript
// api.js 中的映射函数

export function venueLabel(v) {
  return VENUE_LABELS[v] || v || '';
}

export function accountLabel(a) {
  return ACCOUNT_LABELS[a] || a || '';
}

export function typeLabel(t) {
  return TYPE_LABELS[t] || t || '';
}
```

**逻辑**：
1. 首先在映射表中查找键
2. 如果找到，返回对应的中文名称
3. 如果未找到，返回原值
4. 如果原值为空，返回空字符串

---

## 📝 维护指南

### 添加新的账户映射

当数据库中出现新的账户值时：

1. **编辑文件**: `frontend/public/assets/js/api.js`

2. **在 ACCOUNT_LABELS 中添加映射**:
   ```javascript
   export const ACCOUNT_LABELS = {
     // ... 现有映射 ...

     'new_account_id': '新账户名称',
   };
   ```

3. **重新构建前端**:
   ```bash
   cd /c/Users/hzfzzzi/openclaw/pams
   docker compose -f config/docker/compose.yaml build pams-web
   docker compose -f config/docker/compose.yaml up -d pams-web
   ```

4. **验证**:
   - 访问 http://localhost:8080/holdings.html
   - 检查新账户是否显示中文名称

### 添加新的平台映射

步骤同上，但修改 `VENUE_LABELS`。

### 添加新的类型映射

步骤同上，但修改 `TYPE_LABELS`。

---

## 🐛 常见问题

### Q1: 持仓页面仍显示英文代码

**可能原因**:
1. 前端容器未重启
2. 浏览器缓存未清除
3. 映射表中缺少该值

**解决方案**:
```bash
# 1. 重新构建并重启前端
docker compose -f config/docker/compose.yaml up -d --force-recreate pams-web

# 2. 清除浏览器缓存（Ctrl+Shift+R 强制刷新）

# 3. 检查映射表是否包含该值
curl http://localhost:8080/assets/js/api.js | grep "账户值"
```

### Q2: 如何查看数据库中的原始账户值？

```bash
# 方法1: 查看API返回的原始数据
curl http://localhost:8090/holdings/latest | jq '.rows[] | {venue, account, type}'

# 方法2: 直接查询数据库
docker exec -it pams-postgres psql -U pams -d pams -c \
  "SELECT DISTINCT venue, account, type FROM pams_holdings_snapshot LIMIT 20;"
```

### Q3: 某些账户显示为"未知"或原始值

这表示该账户值未在映射表中定义。请按照"添加新的账户映射"步骤添加。

---

## 📊 映射统计

当前映射覆盖情况：

- **账户映射**: 30+ 个账户ID
- **平台映射**: 15+ 个平台代码
- **类型映射**: 20+ 个资产类型

**覆盖率**: ~95%（基于资产负债表.xlsx）

---

## 🔄 同步建议

建议每次更新"资产负债表.xlsx"时：

1. 检查是否有新的账户、平台或类型
2. 更新 `api.js` 中的映射表
3. 重新构建前端容器
4. 验证持仓页面显示正确

---

**最后更新**: 2026-02-15
**维护者**: Claude Code + hzfzzzi
