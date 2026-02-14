# PAMS Telegram Bot - 快速开始指南

## 5分钟快速启动 🚀

### 第1步：创建 Telegram Bot（2分钟）

1. 打开 Telegram，搜索 `@BotFather`
2. 发送 `/newbot` 命令
3. 按提示设置：
   - Bot 名称：`PAMS 记账助手`
   - Bot 用户名：`your_pams_bot`（必须以 `bot` 结尾）
4. **复制 Bot Token**（形如 `123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11`）

### 第2步：配置环境变量（1分钟）

在 `telegram-bot` 目录创建 `.env` 文件：

```bash
cd c:/Users/hzfzzzi/openclaw/pams/telegram-bot
cp .env.example .env
```

编辑 `.env`，填入你的 Bot Token：

```bash
TELEGRAM_BOT_TOKEN=你的Bot Token
PAMS_API_BASE=http://localhost:8090/api
```

### 第3步：安装依赖（1分钟）

```bash
pip install -r requirements.txt
```

### 第4步：启动机器人（1分钟）

```bash
python bot.py
```

看到以下输出表示成功：
```
🤖 PAMS Telegram Bot 启动中...
```

### 第5步：开始使用！

1. 在 Telegram 搜索你的机器人用户名
2. 发送 `/start` 开始
3. 试试发送 `-25 咖啡` 快速记账

## 使用示例

### 快速记账（最常用）

直接发送消息，格式：`金额 备注`

```
-50 午餐          → 支出50元，自动分类"餐饮"
-100 打车         → 支出100元，自动分类"交通"
+5000 工资        → 收入5000元，自动分类"工资"
-25.5 咖啡        → 支出25.5元（支持小数）
```

### 详细记账（可选）

使用 `/record` 命令，按钮选择分类：

```
你: /record
Bot: 请选择记账类型： [💸 支出] [💰 收入]
你: 点击 [💸 支出]
Bot: 💸 支出 - 请输入金额（仅数字）：
你: 50
Bot: 金额：¥50.00 - 请选择分类： [🍜 餐饮] [🚗 交通] ...
你: 点击 [🍜 餐饮]
Bot: 请输入备注（或发送 /skip 跳过）：
你: 午餐
Bot: ✅ 记账成功！
```

### 查看统计

```
你: /stats
Bot: 📊 2026-02 月度统计
     💰 总收入：¥5000.00
     💸 总支出：¥150.00
     💵 结余：¥4850.00
```

### 查看记录

```
你: /list
Bot: 📋 最近记录
     💸 -¥50.00 - 餐饮 (午餐)
     💸 -¥25.00 - 餐饮 (咖啡)
     ...
```

## 常用命令速查

| 命令 | 说明 |
|-----|------|
| `/start` | 开始使用 |
| `/help` | 帮助信息 |
| `/record` | 详细记账 |
| `/stats` | 月度统计 |
| `/list` | 最近记录 |

## 智能分类关键词

机器人会根据备注自动分类：

- 🍜 **餐饮**：午餐、晚餐、咖啡、奶茶
- 🚗 **交通**：打车、地铁、公交、滴滴
- 🛍️ **购物**：超市、淘宝、京东、买
- 🎮 **娱乐**：电影、游戏、KTV
- 🏠 **居住**：房租、水电、物业
- 💊 **医疗**：药、医院、看病
- 📚 **教育**：书、课程、培训
- 📱 **通讯**：话费、流量、宽带

## 故障排查

### 1. Bot 不响应？

检查：
- [ ] `python bot.py` 是否在运行
- [ ] Bot Token 是否正确
- [ ] 网络连接是否正常

### 2. 提示"记账失败"？

检查：
- [ ] PAMS 后端 API 是否运行
- [ ] `.env` 中的 `PAMS_API_BASE` 是否正确
- [ ] 访问 `http://localhost:8090/api/health` 测试后端

### 3. 如何后台运行？

**Windows**:
```bash
pythonw bot.py
```

**Linux/Mac**:
```bash
nohup python bot.py > bot.log 2>&1 &
```

**Docker（推荐）**:
```bash
docker compose up -d pams-bot
```

## 下一步

- ✅ 开始记账
- 📊 查看统计
- 🔧 自定义分类规则（编辑 `bot.py`）
- 🚀 部署到服务器（Docker）
- 🔒 添加用户白名单（见 README）

---

需要帮助？查看完整文档：[README.md](README.md)
