# 持仓页面更新说明

## 📋 更新内容

### 1. 页面布局优化

**删除列**：
- ❌ "平台" 列（已删除）
- ❌ "代码" 列（已删除）

**保留列**：
- ✅ 账户
- ✅ 类型
- ✅ 名称
- ✅ 币种
- ✅ 市值
- ✅ 持仓收益
- ✅ 昨日收益

### 2. 账户标准化

**标准账户列表**（与资产负债表.xlsx完全一致）：

1. 腾讯理财通
2. 建行
3. 华为账户
4. 公积金账户
5. 币安
6. IBKR
7. 广发
8. Santander
9. 同花顺
10. 借款
11. 微信

**映射逻辑**：
- 所有数据库中的账户ID自动映射到上述11个标准账户
- 无法映射的账户显示为"其他"

---

## 🔧 技术实现

### 后端层

#### 新增文件
- `backend/api/account_mapper.py` - 账户标准化映射模块

**功能**：
```python
def standardize_account(account_id: str) -> str:
    """
    将数据库账户ID映射到11个标准账户之一

    示例：
    'huawei_emp' → '华为账户'
    'santander4209_cash' → 'Santander'
    'cn_a_rmb' → '同花顺'
    """
```

#### 修改文件
- `backend/api/main.py`

**修改点**：
1. 导入账户映射模块
2. 在 `holdings_latest` 端点应用账户标准化
3. 在 `holdings_as_of` 端点应用账户标准化

**代码示例**：
```python
from account_mapper import standardize_account

# 在返回数据前标准化账户
for r in rows:
    row_dict = {k: _norm(v) for k, v in dict(r).items()}
    if 'account' in row_dict:
        row_dict['account'] = standardize_account(row_dict['account'])
    out_rows.append(row_dict)
```

### 前端层

#### 修改文件
- `frontend/public/holdings.html` - 删除"平台"和"代码"列
- `frontend/public/assets/js/holdings.js` - 更新表格渲染逻辑

**HTML 变更**：
```html
<!-- 之前：9列 -->
<th>平台</th>
<th>账户</th>
<th>类型</th>
<th>代码</th>
<th>名称</th>
...

<!-- 现在：7列 -->
<th>账户</th>
<th>类型</th>
<th>名称</th>
...
```

**JavaScript 变更**：
```javascript
// 之前：显示所有字段
tr.innerHTML = `
  <td>${venueLabel(r.venue)}</td>
  <td>${accountLabel(r.account)}</td>
  <td>${typeLabel(r.type)||''}</td>
  <td>${r.symbol||''}</td>
  ...
`;

// 现在：只显示必要字段
tr.innerHTML = `
  <td>${accountLabel(r.account)}</td>
  <td>${typeLabel(r.type)||''}</td>
  <td>${r.name||''}</td>
  ...
`;
```

---

## 📊 账户映射表

### 完整映射关系

| 数据库账户ID | → | 标准账户 |
|-------------|---|---------|
| `tencent_licaitong` | → | 腾讯理财通 |
| `licaitong` | → | 腾讯理财通 |
| `main` | → | 腾讯理财通 |
| `主账户` | → | 腾讯理财通 |
| | | |
| `ccb` | → | 建行 |
| `ccb_wm47` | → | 建行 |
| `ccb_gold` | → | 建行 |
| `ccb_7332` | → | 建行 |
| | | |
| `huawei` | → | 华为账户 |
| `huawei_emp` | → | 华为账户 |
| `huawei_esop` | → | 华为账户 |
| | | |
| `cn_hpf` | → | 公积金账户 |
| `hpf` | → | 公积金账户 |
| | | |
| `binance` | → | 币安 |
| | | |
| `ibkr` | → | IBKR |
| `us_main` | → | IBKR |
| `美股主账户` | → | IBKR |
| | | |
| `gf` | → | 广发 |
| `guangfa` | → | 广发 |
| `gf_7443` | → | 广发 |
| `gf_9711` | → | 广发 |
| | | |
| `santander` | → | Santander |
| `santander4209` | → | Santander |
| `santander4209_cash` | → | Santander |
| | | |
| `tonghuashun` | → | 同花顺 |
| `ths` | → | 同花顺 |
| `cn_a` | → | 同花顺 |
| `cn_a_rmb` | → | 同花顺 |
| `A股人民币账户` | → | 同花顺 |
| | | |
| `recv_loan` | → | 借款 |
| `loan` | → | 借款 |
| | | |
| `wechat` | → | 微信 |
| `weixin` | → | 微信 |
| | | |
| `recv_reimburse` | → | 其他 |
| `manual` | → | 其他 |
| `unknown` | → | 其他 |

---

## 🔄 数据同步

### 与收支页面同步

**问题**：持仓数据（`pams_holdings_snapshot`）和交易数据（`pams_transactions`）是两个独立的表。

**解决方案**：
1. **持仓数据**：来自资产负债表导入，反映实际持有的资产
2. **交易数据**：来自日常收支记录，反映现金流动

**刷新机制**：
- 持仓页面每次访问时从数据库读取最新快照
- 账户名称在后端API层统一标准化
- 前端显示始终使用标准账户名称

**未来改进**（可选）：
- 将交易数据汇总为持仓变化
- 定期同步两个数据源
- 建立账户余额视图

---

## ✅ 验证步骤

### 1. 访问持仓页面
```
http://localhost:8080/holdings.html
```

### 2. 检查页面布局
- [ ] 只显示7列（无"平台"和"代码"）
- [ ] 账户列显示标准中文名称
- [ ] 类型列显示中文类型
- [ ] 表格数据正常加载

### 3. 检查账户名称
所有账户应该只显示11个标准账户之一：
- [ ] 腾讯理财通
- [ ] 建行
- [ ] 华为账户
- [ ] 公积金账户
- [ ] 币安
- [ ] IBKR
- [ ] 广发
- [ ] Santander
- [ ] 同花顺
- [ ] 借款
- [ ] 微信

如有其他账户，应显示为"其他"。

### 4. 测试API端点
```bash
# 测试持仓API
curl http://localhost:8090/holdings/latest | jq '.rows[0:3]'

# 验证账户字段已标准化
curl http://localhost:8090/holdings/latest | jq '.rows[].account' | sort | uniq
```

---

## 🐛 故障排查

### 问题1：账户仍显示原始ID

**症状**：持仓页面显示 `huawei_emp` 而非"华为账户"

**原因**：后端容器未重启或映射失败

**解决**：
```bash
cd /c/Users/hzfzzzi/openclaw/pams
docker compose -f config/docker/compose.yaml restart pams-api
docker logs pams-api --tail 20
```

### 问题2：页面布局错乱

**症状**：表格列数不对，显示空白列

**原因**：前端容器未更新

**解决**：
```bash
docker compose -f config/docker/compose.yaml build pams-web
docker compose -f config/docker/compose.yaml up -d pams-web
```

### 问题3：某些账户显示为"其他"

**症状**：正常账户被映射为"其他"

**原因**：账户ID未在映射表中定义

**解决**：
1. 编辑 `backend/api/account_mapper.py`
2. 在 `ACCOUNT_MAPPING` 中添加缺失的映射
3. 重新构建并重启 pams-api 容器

---

## 📝 维护指南

### 添加新的账户映射

当数据库中出现新的账户ID时：

1. **编辑映射文件**
   ```bash
   nano backend/api/account_mapper.py
   ```

2. **添加映射关系**
   ```python
   ACCOUNT_MAPPING = {
       # ... 现有映射 ...
       'new_account_id': '标准账户名称',
   }
   ```

3. **重新部署**
   ```bash
   docker compose -f config/docker/compose.yaml build pams-api
   docker compose -f config/docker/compose.yaml up -d pams-api
   ```

4. **验证**
   ```bash
   curl http://localhost:8090/holdings/latest | jq '.rows[].account' | grep "新账户"
   ```

---

## 🎯 页面对比

### 更新前

| 平台 | 账户 | 类型 | 代码 | 名称 | 币种 | 市值 | ... |
|-----|-----|-----|-----|-----|-----|-----|-----|
| 手动录入 | huawei_emp | equity_emp | - | 华为员工内部持股 | CNY | ... | ... |
| Santander | santander4209_cash | 现金 | - | Santander4209 活期 | MXN | ... | ... |

### 更新后

| 账户 | 类型 | 名称 | 币种 | 市值 | ... |
|-----|-----|-----|-----|-----|-----|
| 华为账户 | 员工持股 | 华为员工内部持股 | CNY | ... | ... |
| Santander | 现金 | Santander4209 活期 | MXN | ... | ... |

---

**更新日期**: 2026-02-15
**版本**: v1.2
**作者**: Claude Code + hzfzzzi
