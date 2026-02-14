# PAMS 系统访问指南

## 🎯 稳定入口地址

### ⭐ 推荐访问方式

**通过端口直接访问**（最稳定）：

| 服务 | 地址 | 说明 |
|-----|------|------|
| **总览页面** | http://localhost:8080/ | 资产总览、KPI、图表 |
| **收支分析** | http://localhost:8080/cashflow.html | 收支记录、添加交易、趋势图 |
| **持仓明细** | http://localhost:8080/holdings.html | 详细持仓列表 |
| **投资策略** | http://localhost:8080/strategy.html | IPS策略、风险监控 |
| **观察池** | http://localhost:8080/watchlist.html | 候选投资标的 |

### 🔧 API 端点

| 端点 | 地址 | 说明 |
|-----|------|------|
| 后端API | http://localhost:8090/api/ | FastAPI服务 |
| API文档 | http://localhost:8090/docs | Swagger UI |

### 🌐 局域网访问

如果已配置 AdGuard DNS 和 Caddy：

| 服务 | 地址 | 说明 |
|-----|------|------|
| PAMS主页 | http://pams.local/ | 通过Caddy代理 |
| Firefly III | http://firefly.pams.local/ | 记账系统 |
| AdGuard | http://adguard.pams.local/ | DNS管理 |

---

## 🚀 快速开始

### 1. 启动所有服务

```bash
cd /c/Users/hzfzzzi/openclaw/pams
docker compose -f config/docker/compose.yaml up -d
```

### 2. 验证服务状态

```bash
docker ps --filter name=pams
```

应该看到以下容器运行中：
- ✅ pams-web
- ✅ pams-api
- ✅ pams-postgres
- ✅ pams-caddy
- ✅ pams-firefly
- ✅ pams-adguard

### 3. 访问页面

直接在浏览器打开：
```
http://localhost:8080/
```

---

## 📊 功能页面导航

### 总览页面 (/)
- 5个KPI卡片：总资产、总负债、净资产、最大回撤、持仓数
- 资产结构饼图
- 负债结构饼图
- 净资产曲线
- 近期收支图

### 收支分析 (/cashflow.html)
- 📅 月份选择器
- 💰 月度KPI：总收入、总支出、结余
- 📈 **按月开支统计折线图**（近6个月趋势）
- 🥧 **支出分类占比饼图**
- ➕ **添加交易表单**
  - 支持选择币种：MXN / CNY / USD
  - 支持选择账户：11个标准账户
  - 默认：MXN + Santander
- 📝 最近交易记录表格

### 持仓明细 (/holdings.html)
- 完整持仓列表
- 市值、盈亏统计
- 按类型筛选

### 投资策略 (/strategy.html)
- IPS策略参数
- 风险事件列表
- NAV/回撤曲线

### 观察池 (/watchlist.html)
- 候选投资标的
- 投资论点记录

---

## 🔧 常用操作

### 查看日志

```bash
# 前端日志
docker logs pams-web --tail 50

# 后端日志
docker logs pams-api --tail 50

# 数据库日志
docker logs pams-postgres --tail 50
```

### 重启服务

```bash
cd /c/Users/hzfzzzi/openclaw/pams

# 重启前端
docker compose -f config/docker/compose.yaml restart pams-web

# 重启后端
docker compose -f config/docker/compose.yaml restart pams-api

# 重启全部
docker compose -f config/docker/compose.yaml restart
```

### 重新构建

```bash
cd /c/Users/hzfzzzi/openclaw/pams

# 重新构建前端
docker compose -f config/docker/compose.yaml build pams-web

# 重新构建后端
docker compose -f config/docker/compose.yaml build pams-api

# 重新构建并启动
docker compose -f config/docker/compose.yaml up -d --build
```

### 完全重启（环境变量更新后）

```bash
cd /c/Users/hzfzzzi/openclaw/pams

# 1. 确保.env文件在正确位置
cp .env config/docker/.env

# 2. 强制重新创建容器
docker compose -f config/docker/compose.yaml up -d --force-recreate
```

---

## 🐛 故障排查

### 问题1：页面打不开

**症状**：访问 http://localhost:8080/ 无响应

**排查步骤**：
```bash
# 1. 检查容器是否运行
docker ps | grep pams-web

# 2. 如果没有运行，启动它
docker compose -f config/docker/compose.yaml up -d pams-web

# 3. 检查日志
docker logs pams-web --tail 30
```

### 问题2：API错误 500

**症状**：收支页面加载失败，控制台显示 500 错误

**原因**：后端无法连接数据库（密码问题）

**解决方案**：
```bash
# 1. 检查环境变量
docker exec pams-api env | grep PG

# 2. 如果PGPASSWORD为空，重新创建容器
cd /c/Users/hzfzzzi/openclaw/pams
cp .env config/docker/.env
docker compose -f config/docker/compose.yaml up -d --force-recreate pams-api

# 3. 验证
curl http://localhost:8090/api/transactions/monthly/2026-02
```

### 问题3：数据库连接失败

**症状**：后端日志显示 "password authentication failed"

**解决方案**：
```bash
# 1. 确保.env文件存在
cat /c/Users/hzfzzzi/openclaw/pams/.env

# 2. 复制到docker目录
cp /c/Users/hzfzzzi/openclaw/pams/.env /c/Users/hzfzzzi/openclaw/pams/config/docker/

# 3. 重新创建API和数据库容器
cd /c/Users/hzfzzzi/openclaw/pams
docker compose -f config/docker/compose.yaml up -d --force-recreate pams-api postgres
```

### 问题4：Caddy无法访问 (pams.local)

**症状**：http://pams.local/ 无法访问

**原因**：DNS未配置或Caddy未运行

**解决方案**：
```bash
# 1. 检查Caddy是否运行
docker ps | grep pams-caddy

# 2. 如果未运行，启动它
docker compose -f config/docker/compose.yaml up -d caddy

# 3. 检查AdGuard DNS是否配置了 pams.local 指向 192.168.0.143
# 访问 http://localhost:8081/ 进入AdGuard管理界面
```

---

## 📱 移动设备访问

### iOS / Android

**前提条件**：
- 设备与电脑在同一局域网
- 已配置 AdGuard Home DNS

**访问步骤**：

1. **配置设备DNS**
   - iOS: 设置 → Wi-Fi → 点击网络 → 配置DNS → 手动
   - 添加DNS服务器：`192.168.0.143`

2. **访问页面**
   - 在移动浏览器打开：http://pams.local/
   - 或使用IP：http://192.168.0.143:8080/

---

## 🔐 环境变量说明

关键环境变量（在 `.env` 文件中）：

```bash
# PostgreSQL密码（必需）
POSTGRES_PASSWORD=IKWeyQYUDMBHO6-BwUKfMbN43--g8EBk

# Firefly III配置
FIREFLY_APP_KEY=base64:FDByPetCdIUZLT5U1OZ6ROryx8l3I+BQIAlq1DpWWHc=
FIREFLY_DB_DATABASE=firefly
FIREFLY_DB_USERNAME=firefly
FIREFLY_DB_PASSWORD=${POSTGRES_PASSWORD}

# Firefly III初始用户
FIREFLY_SITE_OWNER=shareholder
FIREFLY_SITE_OWNER_EMAIL=shareholder@example.com
FIREFLY_SITE_OWNER_PASSWORD=ChangeMe_12345
```

**重要提示**：
- `.env` 文件必须同时存在于以下位置：
  - `/c/Users/hzfzzzi/openclaw/pams/.env` （原始文件）
  - `/c/Users/hzfzzzi/openclaw/pams/config/docker/.env` （Docker读取）

---

## 📞 快速联系方式

### 获取帮助

如遇到问题：
1. 查看本文档的故障排查部分
2. 检查容器日志
3. 访问 http://localhost:8090/docs 查看API文档

---

**最后更新**: 2026-02-15
**版本**: v1.1
**维护者**: Claude Code + hzfzzzi

---

## ✅ 访问清单

部署成功后，请验证以下页面：

- [ ] http://localhost:8080/ - 总览页面
- [ ] http://localhost:8080/cashflow.html - 收支页面
- [ ] http://localhost:8080/holdings.html - 持仓页面
- [ ] http://localhost:8080/strategy.html - 策略页面
- [ ] http://localhost:8080/watchlist.html - 观察池
- [ ] http://localhost:8090/docs - API文档
- [ ] http://localhost:8090/api/transactions/monthly/2026-02 - API测试

**所有链接都应该能正常访问！** ✨
