# PAMS v1.0 Release Notes

🎉 **PAMS v1.0 正式发布！**

这是PAMS项目的第一个正式发布版本，包含完整的收支分析模块和全新的项目架构。

---

## ✨ 主要功能

### 收支分析模块 (Cashflow) ⭐ 新增
- 📅 **月度统计**：自动汇总收入、支出和净额
- 🤖 **智能分类**：基于关键词的自动分类引擎（内置18条规则）
- 📊 **可视化分析**：支出分类饼图、交易记录表格
- 💳 **多账户支持**：微信、支付宝、信用卡等

### 资产管理系统
- 📊 **5大KPI监控**：总资产、总负债、净资产、最大回撤、持仓数
- 🎯 **多维度可视化**：资产结构饼图、NAV曲线、收支趋势
- 💼 **持仓管理**：完整持仓表格、盈亏追踪
- 📈 **投资策略**：IPS策略管理、风险监控
- 👀 **观察池**：候选标的追踪

### 支持平台
- ✅ 国信证券（A股）
- ✅ 盈透证券 IBKR（美股）
- ✅ 腾讯理财通（基金/理财）
- ✅ 建设银行（银行账户）
- ✅ 华为ESOP（员工持股）
- ⏳ 币安 Binance（加密货币 - API集成中）

---

## 🏗️ 项目重构

### 架构清理
- 🗂️ **代码归档**：1.4MB旧版代码归档到 `archive/`
- 📁 **目录优化**：从22个目录精简到14个（精简36%）
- 📚 **文档完善**：4个架构文档 + MIT许可证

### 技术栈
- **后端**：Python 3.11 + FastAPI + PostgreSQL 16
- **前端**：HTML5 + ES6 Modules + Chart.js 4.4.1
- **基础设施**：Docker Compose + Caddy + Nginx

---

## 🐛 Bug修复
- 修复总览页API路径404错误
- 修复前端资源引用路径问题
- 统一API端点路由规则

---

## 📊 数据统计
- **代码量**：17,769行
- **文件数**：202个
- **提交数**：4个
- **归档代码**：1.4MB

---

## 🚀 快速开始

### 1. 克隆项目
```bash
git clone https://github.com/hzfzzzi-ui/pams.git
cd pams
```

### 2. 配置环境
```bash
cp config/.env.example .env
# 编辑.env填写数据库密码等配置
```

### 3. 启动服务
```bash
# 构建镜像
docker compose -f config/docker/compose.yaml build

# 启动服务
docker compose -f config/docker/compose.yaml up -d
```

### 4. 访问系统
```
http://192.168.0.143:8080/
或
http://pams.local/ (需配置DNS)
```

---

## 📚 文档

- [README.md](README.md) - 完整的项目说明和安装指南
- [CURRENT_ARCHITECTURE.md](CURRENT_ARCHITECTURE.md) - 系统架构详解
- [CHANGELOG.md](CHANGELOG.md) - 完整变更日志
- [API文档](backend/api/endpoints_overview.py) - API端点总览

---

## 🙏 致谢

感谢以下开源项目：
- [FastAPI](https://fastapi.tiangolo.com/)
- [Chart.js](https://www.chartjs.org/)
- [Firefly III](https://www.firefly-iii.org/)
- [AdGuard Home](https://adguard.com/en/adguard-home/overview.html)
- [Caddy](https://caddyserver.com/)

---

## 📝 下一步计划

- [ ] Telegram Bot完整测试
- [ ] Binance API集成
- [ ] IBKR自动数据同步
- [ ] 收支预算管理
- [ ] Vue3前端迁移

---

**完整更新日志**: [CHANGELOG.md](CHANGELOG.md)

**下载地址**: [v1.0 Release](https://github.com/hzfzzzi-ui/pams/releases/tag/v1.0)

---

Made with ❤️ by hzfzzzi-ui
