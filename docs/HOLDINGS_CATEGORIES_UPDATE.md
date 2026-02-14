# 持仓页面大类分类与子页面功能

## 📋 更新内容

### 1. 类型映射对齐资产负债表

持仓页面的类型分类现已与`资产负债表.xlsx`的**大类**和**小类**完全一致。

**大类分类**（4个）：
1. **A股** - 包含A股股票、A股基金、员工持股等
2. **美股** - 包含美股股票、美股基金等
3. **加密** - 包含BTC、ETH等加密货币
4. **其他** - 包含贵金属、理财、现金、公积金、借款、欠款等

### 2. 四个子页面

在主持仓页面新增了4个子页面入口，每个子页面包含：
- **持仓明细表** - 显示该大类下所有持仓
- **市值走势图** - 折线图展示历史市值变化

| 子页面 | 数据范围 | 图表频率 | 访问路径 |
|--------|----------|----------|----------|
| A股持仓 | A股+分红类 | 每日 | [holdings_a_stock.html](http://pams.local/holdings_a_stock.html) |
| 美股持仓 | 美股类 | 每日 | [holdings_us_stock.html](http://pams.local/holdings_us_stock.html) |
| 加密持仓 | 加密货币 | 每日 | [holdings_crypto.html](http://pams.local/holdings_crypto.html) |
| 其他持仓 | 现金、理财、贵金属等 | **按周** | [holdings_other.html](http://pams.local/holdings_other.html) |

---

## 🔧 技术实现

### 后端层

#### 新增文件
- `backend/api/type_mapper.py` - 类型映射模块

**功能**：
```python
def get_major_category(type_value: str) -> str:
    """
    将数据库type字段映射到大类

    示例：
    'stock_cn' → 'A股'
    'equity_private' → '分红' → (归入'A股'页面)
    'crypto' → '加密'
    'gold' → '贵金属' → (归入'其他'页面)
    """

def get_minor_category(type_value: str) -> str:
    """
    将数据库type字段映射到小类

    示例：
    'fund' → 'A股基金'
    'cash' → '现金/活期'
    'liability_cc' → '信用卡'
    """
```

#### 修改文件
- `backend/api/main.py`

**新增端点**：

1. **GET /holdings/by-category/{category}**
   - 获取指定大类的最新持仓
   - 参数: `category` (A股/美股/加密/其他), `limit` (默认500)
   - 返回: 包含 `major_category` 和 `minor_category` 字段的持仓列表

2. **GET /holdings/history/{category}**
   - 获取指定大类的历史市值序列
   - 参数:
     - `category` (A股/美股/加密/其他)
     - `days` (默认90天)
     - `interval` (daily/weekly，默认daily)
   - 返回: 日期-市值时间序列数据

**修改端点**：
- `/holdings/latest` - 新增 `major_category` 和 `minor_category` 字段
- `/holdings/as-of` - 新增 `major_category` 和 `minor_category` 字段

### 前端层

#### 新增文件

**HTML页面**（4个）：
- `frontend/public/holdings_a_stock.html` - A股子页面
- `frontend/public/holdings_us_stock.html` - 美股子页面
- `frontend/public/holdings_crypto.html` - 加密子页面
- `frontend/public/holdings_other.html` - 其他子页面

**JavaScript模块**（4个）：
- `frontend/public/assets/js/holdings_a_stock.js`
- `frontend/public/assets/js/holdings_us_stock.js`
- `frontend/public/assets/js/holdings_crypto.js`
- `frontend/public/assets/js/holdings_other.js`

**功能**：
- 调用 `/holdings/by-category/{category}` 获取持仓数据
- 调用 `/holdings/history/{category}` 获取历史市值
- 使用Chart.js渲染折线图
- 其他页面使用 `weekly` 频率以减少数据点

#### 修改文件
- `frontend/public/holdings.html` - 添加4个子页面导航按钮

**变更**：
```html
<!-- 之前：类型筛选器 -->
<select id="typeFilter" class="btn">
  <option value="">全部</option>
</select>

<!-- 现在：大类导航按钮 -->
<a class="pill" href="holdings_a_stock.html">A股</a>
<a class="pill" href="holdings_us_stock.html">美股</a>
<a class="pill" href="holdings_crypto.html">加密</a>
<a class="pill" href="holdings_other.html">其他</a>
```

---

## 📊 类型映射表

### 数据库type字段 → 大类映射

| 数据库type | 小类显示 | 大类 | 页面 |
|-----------|---------|------|------|
| `stock` | A股 | A股 | A股页面 |
| `stock_cn` | A股 | A股 | A股页面 |
| `fund` | A股基金 | A股 | A股页面 |
| `equity_cn` | A股权益 | A股 | A股页面 |
| `esop` | 股票 | 分红 | A股页面 |
| `equity_emp` | 股票 | 分红 | A股页面 |
| `equity_private` | 股票 | 分红 | A股页面 |
| | | | |
| `stock_us` | 美股 | 美股 | 美股页面 |
| `equity_us` | 美股权益 | 美股 | 美股页面 |
| | | | |
| `crypto` | ETH | 加密 | 加密页面 |
| | | | |
| `gold` | 黄金 | 贵金属 | 其他页面 |
| `asset_gold` | 黄金 | 贵金属 | 其他页面 |
| `wealth` | 固收理财 | 理财 | 其他页面 |
| `cash` | 现金/活期 | 现金 | 其他页面 |
| `hpf` | 现金/活期 | 公积金 | 其他页面 |
| `asset_hpf` | 现金/活期 | 公积金 | 其他页面 |
| `loan` | 借款 | 借款 | 其他页面 |
| `recv_loan` | 借款 | 借款 | 其他页面 |
| `receivable_loan` | 借款 | 借款 | 其他页面 |
| `receivable` | 应收款 | 应收款 | 其他页面 |
| `recv` | 应收款 | 应收款 | 其他页面 |
| `recv_reimburse` | 应收款 | 应收款 | 其他页面 |
| `receivable_reimburse` | 应收款 | 应收款 | 其他页面 |
| `liability` | 信用卡 | 欠款 | 其他页面 |
| `liability_cc` | 信用卡 | 欠款 | 其他页面 |

---

## ✅ 验证步骤

### 1. 访问主持仓页面
```
http://pams.local/holdings.html
```

检查：
- [ ] 页面顶部有4个导航按钮（A股、美股、加密、其他）
- [ ] 点击按钮可跳转到对应子页面

### 2. 访问A股子页面
```
http://pams.local/holdings_a_stock.html
```

检查：
- [ ] 折线图显示近90天A股市值走势
- [ ] 表格显示所有A股类持仓（包括股票和基金）
- [ ] 小类列显示"A股"、"A股基金"、"股票"等
- [ ] 总计数量正确

### 3. 访问美股子页面
```
http://pams.local/holdings_us_stock.html
```

检查：
- [ ] 折线图显示近90天美股市值走势
- [ ] 表格显示所有美股类持仓

### 4. 访问加密子页面
```
http://pams.local/holdings_crypto.html
```

检查：
- [ ] 折线图显示近90天加密资产市值走势
- [ ] 表格显示所有加密货币持仓

### 5. 访问其他子页面
```
http://pams.local/holdings_other.html
```

检查：
- [ ] 折线图显示近90天**按周**市值走势（数据点较少）
- [ ] 表格显示贵金属、理财、现金、公积金、借款、欠款等

### 6. 测试API端点

```bash
# 测试获取A股持仓
curl http://localhost:8090/holdings/by-category/A股?limit=5

# 测试获取美股历史
curl "http://localhost:8090/holdings/history/美股?days=30&interval=daily"

# 测试获取其他类别（按周）
curl "http://localhost:8090/holdings/history/其他?days=90&interval=weekly"

# 验证主端点包含分类字段
curl http://localhost:8090/holdings/latest?limit=3 | grep major_category
```

---

## 📝 维护指南

### 添加新的类型映射

当数据库中出现新的type值时：

1. **编辑映射文件**
   ```bash
   nano backend/api/type_mapper.py
   ```

2. **添加到TYPE_TO_MAJOR字典**
   ```python
   TYPE_TO_MAJOR = {
       # ... 现有映射 ...
       'new_type': '大类名称',  # A股/美股/加密/贵金属/理财/现金/公积金/借款/应收款/欠款
   }
   ```

3. **添加到TYPE_TO_MINOR字典**
   ```python
   TYPE_TO_MINOR = {
       # ... 现有映射 ...
       'new_type': '小类显示名称',
   }
   ```

4. **重新部署**
   ```bash
   cd c:/Users/hzfzzzi/openclaw/pams
   docker compose -f config/docker/compose.yaml build pams-api
   docker compose -f config/docker/compose.yaml up -d pams-api
   ```

### 修改图表时间范围

编辑对应的JavaScript文件，修改`days`参数：

```javascript
// holdings_a_stock.js
const history = await jget('/holdings/history/A股?days=180&interval=daily');
//                                                         ^^^ 改为180天
```

---

## 🎯 页面对比

### 更新前（主持仓页面）

| 账户 | 类型 | 名称 | 币种 | 市值 | ... |
|-----|------|------|-----|------|-----|
| 华为账户 | equity_private | 华为员工内部持股 | CNY | ... | ... |
| 腾讯理财通 | fund | 华夏人工智能ETF链接A | CNY | ... | ... |

- 类型显示数据库原始值
- 无大类分类概念
- 无子页面导航

### 更新后（主持仓页面）

| 账户 | 类型 | 名称 | 币种 | 市值 | ... |
|-----|------|------|-----|------|-----|
| 华为账户 | 股票 | 华为员工内部持股 | CNY | ... | ... |
| 腾讯理财通 | A股基金 | 华夏人工智能ETF链接A | CNY | ... | ... |

- 类型显示小类中文名称
- 包含major_category字段（A股/美股/加密/其他）
- 顶部有4个子页面导航按钮

### A股子页面（新增）

**折线图区域**：
- 标题：A股市值走势（近90天）
- X轴：日期
- Y轴：市值（CNY）
- 数据：每日A股+分红类总市值

**持仓表格**：
- 只显示major_category='A股'的持仓
- 小类列显示具体分类（A股、A股基金、股票）
- 按市值降序排列

---

## 🐛 故障排查

### 问题1：子页面显示空数据

**症状**：折线图为空，表格无数据

**可能原因**：
1. 数据库中该大类无持仓数据
2. type映射未配置

**解决**：
```bash
# 检查数据库中的type值
docker exec pams-postgres psql -U pams -d pams -c \
  "SELECT DISTINCT type, COUNT(*) FROM pams_holdings_snapshot GROUP BY type;"

# 检查是否有未映射的type
curl http://localhost:8090/holdings/latest | grep '"major_category":"其他"'
```

### 问题2：折线图报错

**症状**：页面加载时控制台显示API错误

**可能原因**：
1. 历史数据不足90天
2. API端点参数错误

**解决**：
```bash
# 检查API返回
curl "http://localhost:8090/holdings/history/A股?days=30&interval=daily"

# 减少天数参数（修改JS文件）
const history = await jget('/holdings/history/A股?days=30&interval=daily');
```

### 问题3：某些type显示为原始值

**症状**：小类列显示 `stock_us` 而非"美股"

**原因**：TYPE_TO_MINOR中缺少该映射

**解决**：
1. 编辑 `backend/api/type_mapper.py`
2. 在 `TYPE_TO_MINOR` 中添加映射
3. 重新部署pams-api容器

---

## 📌 注意事项

1. **其他页面使用weekly频率**
   - 因为现金、理财等资产变动频率较低
   - 减少数据点，提升图表可读性

2. **大类归属逻辑**
   - "分红"类（如员工持股）归入"A股"页面
   - 所有未明确分类的归入"其他"页面

3. **历史数据依赖**
   - 折线图需要`pams_holdings_snapshot`表有足够历史数据
   - 建议至少有30天数据

4. **URL编码**
   - 中文分类（如"A股"）在URL中会被自动编码
   - JavaScript `jget()` 函数会正确处理

---

**更新日期**: 2026-02-15
**版本**: v2.0
**作者**: Claude Code + hzfzzzi
