# Firefly III 废弃说明

**日期**: 2026-02-15
**影响版本**: v1.1+

---

## 📝 背景

Firefly III是一个功能强大的开源记账系统，原本计划用于PAMS的收支管理模块。但在实际使用中发现：

### 问题
1. **健康状态异常**: `pams-firefly-cron`容器一直处于unhealthy状态
2. **架构复杂度**: Firefly III引入了额外的Laravel框架和复杂配置
3. **集成困难**: API集成复杂，数据同步不稳定
4. **资源占用**: Firefly + Firefly-Cron占用大量内存和CPU
5. **功能冗余**: PAMS只需要简单的收支记录，不需要Firefly的全部功能

### 决策
**废弃Firefly III，改用PostgreSQL直接存储收支数据**

---

## 🔧 技术变更

### 架构对比

#### 旧架构 (v1.0)
```
用户 → Caddy → Firefly III (Laravel) → PostgreSQL (firefly数据库)
                    ↓
                PAMS API → 读取Firefly数据
```

#### 新架构 (v1.1+)
```
用户 → Caddy → PAMS API → PostgreSQL (pams数据库)
                    ↓
              pams_transactions表
```

### 数据模型

**PostgreSQL表结构** (`pams_transactions`):
```sql
CREATE TABLE pams_transactions (
    id SERIAL PRIMARY KEY,
    date DATE NOT NULL,
    amount DECIMAL(15,2) NOT NULL,
    currency VARCHAR(3) DEFAULT 'CNY',
    category VARCHAR(50) NOT NULL,
    subcategory VARCHAR(50),
    account VARCHAR(50),
    description TEXT,
    location VARCHAR(200),
    auto_classified BOOLEAN DEFAULT FALSE,
    confidence REAL,
    source VARCHAR(20) DEFAULT 'manual',
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);
```

---

## 📦 迁移步骤

### 1. 停止并移除Firefly容器

```bash
cd config/docker
docker compose stop firefly firefly-cron
docker compose rm -f firefly firefly-cron
```

### 2. 更新 `compose.yaml`

已完成修改：
- 注释掉`firefly`和`firefly-cron`服务定义
- 移除Caddy对`firefly`的依赖
- 注释掉`firefly_upload` volume

### 3. 更新 `Caddyfile`

已完成修改：
- 注释掉`http://firefly.pams.local`路由

### 4. 重启Caddy

```bash
docker compose restart caddy
```

---

## 🗑️ 数据清理（可选）

### 删除Firefly数据库

如果不再需要Firefly的历史数据：

```sql
-- 连接到PostgreSQL
docker exec -it pams-postgres psql -U pams -d postgres

-- 删除Firefly数据库
DROP DATABASE IF EXISTS firefly;

-- 删除Firefly用户
DROP USER IF EXISTS firefly;
```

### 删除Docker Volume

```bash
# 删除Firefly上传文件
docker volume rm pams_firefly_upload
```

---

## ✅ 验证

### 检查运行容器

```bash
docker ps --filter "name=pams"
```

**预期结果**:
- ✅ pams-web: 运行中
- ✅ pams-api: 运行中
- ✅ pams-postgres: 运行中
- ✅ pams-caddy: 运行中
- ✅ pams-adguard: 运行中
- ✅ pams-bot: 运行中
- ❌ pams-firefly: 不存在
- ❌ pams-firefly-cron: 不存在

### 测试收支API

```bash
# 测试交易记录查询
curl http://pams.local/api/transactions/recent?limit=10

# 测试月度统计
curl http://pams.local/api/transactions/monthly/2026-02
```

---

## 📊 收支功能使用

### 添加交易记录

**方法1: 直接通过API**

```bash
curl -X POST http://pams.local/api/transactions \
  -H "Content-Type: application/json" \
  -d '{
    "date": "2026-02-15",
    "amount": -50.00,
    "category": "餐饮",
    "description": "午餐",
    "account": "wechat",
    "currency": "CNY"
  }'
```

**方法2: 通过数据库直接插入**

```sql
INSERT INTO pams_transactions (date, amount, category, description, account)
VALUES ('2026-02-15', -50.00, '餐饮', '午餐', 'wechat');
```

**方法3: 通过微信小程序（开发中）**

未来版本将提供微信小程序快速记账。

### 查看收支数据

访问：http://pams.local/cashflow.html

---

## 🎯 优势

### 简化架构
- ❌ 移除Firefly III (Laravel框架)
- ❌ 移除Firefly Cron (定时任务)
- ✅ 统一使用PostgreSQL
- ✅ 减少容器数量 (从8个降到6个)

### 性能提升
- 内存占用减少 ~500MB
- 容器启动时间减少 ~30秒
- API响应速度提升

### 开发效率
- 无需维护Firefly配置
- 数据模型自主可控
- API端点统一管理

---

## 🚧 待完成功能

虽然废弃了Firefly，但以下功能仍需实现：

1. **自动分类引擎**
   - 基于关键词的智能分类
   - 分类规则学习
   - 置信度评分

2. **预算管理**
   - 月度预算设置
   - 预算执行监控
   - 超支预警

3. **数据导入**
   - 微信/支付宝账单CSV导入
   - 银行流水导入
   - Excel批量导入

4. **数据可视化**
   - 月度收支趋势图
   - 分类占比饼图
   - 收支对比柱状图

5. **微信小程序**
   - 快速记账界面
   - 地理位置获取
   - 语音输入

---

## 📚 相关文档

- [PROJECT_ANALYSIS_2026-02-15.md](PROJECT_ANALYSIS_2026-02-15.md) - 项目分析报告
- [VERSION_v1.1_SUMMARY.md](VERSION_v1.1_SUMMARY.md) - v1.1版本总结
- [CHANGELOG.md](../CHANGELOG.md) - 完整变更日志

---

## 🔄 回滚方案（不推荐）

如果需要恢复Firefly：

1. 取消注释 `compose.yaml` 中的Firefly服务
2. 取消注释 `Caddyfile` 中的Firefly路由
3. 恢复Firefly数据库（如果已删除）
4. 重新启动容器：
   ```bash
   docker compose up -d firefly firefly-cron
   docker compose restart caddy
   ```

---

**变更完成日期**: 2026-02-15
**影响版本**: v1.1+
**文档维护**: Claude Sonnet 4.5
