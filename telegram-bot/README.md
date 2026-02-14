# PAMS Telegram Bot - 收支记账助手

轻量级 Telegram 记账机器人，与 PAMS 后端 API 集成。

## 功能特性

### 1. 快速记账（推荐）⚡
直接发送消息即可记账，格式：`金额 备注`

**示例：**
- `-50 午餐` → 支出50元，自动分类为"餐饮"
- `-100 打车回家` → 支出100元，自动分类为"交通"
- `+5000 工资` → 收入5000元，自动分类为"工资"

**智能分类：**
机器人会根据备注关键词自动识别分类：
- 餐饮：午餐、晚餐、咖啡、奶茶等
- 交通：打车、地铁、公交等
- 购物：超市、淘宝、京东等
- 娱乐：电影、游戏、KTV等
- 居住：房租、水电、物业等

### 2. 详细记账 📋
使用 `/record` 命令，按步骤选择：
1. 选择收入/支出
2. 输入金额
3. 选择分类（按钮菜单）
4. 输入备注（可跳过）

### 3. 查看统计 📊
- `/stats` - 查看本月收支统计
  - 总收入
  - 总支出
  - 结余

### 4. 查看记录 📝
- `/list` - 查看最近10条记录

### 5. 其他命令
- `/start` - 开始使用
- `/help` - 帮助信息
- `/cancel` - 取消当前操作

## 快速开始

### 1. 创建 Telegram Bot

1. 在 Telegram 中找到 [@BotFather](https://t.me/BotFather)
2. 发送 `/newbot` 创建新机器人
3. 按提示设置名称和用户名
4. 获得 Bot Token（形如 `123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11`）

### 2. 安装依赖

```bash
cd c:/Users/hzfzzzi/openclaw/pams/telegram-bot
pip install -r requirements.txt
```

### 3. 配置环境变量

复制 `.env.example` 为 `.env`：

```bash
cp .env.example .env
```

编辑 `.env` 文件：

```bash
# 填入你的 Bot Token
TELEGRAM_BOT_TOKEN=123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11

# 后端 API 地址
PAMS_API_BASE=http://pams.local/api
```

### 4. 启动机器人

```bash
python bot.py
```

看到以下输出表示启动成功：
```
🤖 PAMS Telegram Bot 启动中...
```

### 5. 开始使用

1. 在 Telegram 中搜索你的机器人（用户名）
2. 发送 `/start` 开始使用
3. 试试发送 `-25 咖啡` 快速记账

## 使用示例

### 快速记账

```
你: -50 午餐
Bot: ✅ 记账成功！

💸 支出：¥50.00
分类：餐饮
备注：午餐
```

```
你: +5000 工资
Bot: ✅ 记账成功！

💰 收入：¥5000.00
分类：工资
备注：工资
```

### 查看统计

```
你: /stats
Bot: 📊 2026-02 月度统计

💰 总收入：¥5000.00
💸 总支出：¥150.00
💵 结余：¥4850.00

📈 收大于支
```

### 查看记录

```
你: /list
Bot: 📋 最近记录

💸 -¥50.00 - 餐饮
   午餐 (2026-02-15)

💸 -¥25.00 - 餐饮
   咖啡 (2026-02-15)

💰 +¥5000.00 - 工资
   工资 (2026-02-15)
```

## Docker 部署（推荐）

### 创建 Dockerfile

```dockerfile
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

CMD ["python", "-u", "bot.py"]
```

### 添加到 Docker Compose

在 `compose.yaml` 中添加：

```yaml
services:
  # ... 其他服务

  pams-bot:
    build:
      context: ./telegram-bot
      dockerfile: Dockerfile
    container_name: pams-telegram-bot
    restart: unless-stopped
    environment:
      - TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}
      - PAMS_API_BASE=http://pams-api:8000/api
    networks:
      - pams-network
    depends_on:
      - pams-api
```

### 启动

```bash
docker compose up -d pams-bot
```

## 后台运行（Linux/Mac）

使用 `screen` 或 `tmux`：

```bash
# 使用 screen
screen -S pams-bot
python bot.py
# Ctrl+A, D 退出但保持运行

# 重新连接
screen -r pams-bot
```

或使用 systemd 服务（推荐）：

```ini
# /etc/systemd/system/pams-bot.service
[Unit]
Description=PAMS Telegram Bot
After=network.target

[Service]
Type=simple
User=your_user
WorkingDirectory=/path/to/pams/telegram-bot
ExecStart=/usr/bin/python3 bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

启动服务：
```bash
sudo systemctl daemon-reload
sudo systemctl enable pams-bot
sudo systemctl start pams-bot
```

## 后台运行（Windows）

### 方法1: 使用 `pythonw`

```bash
pythonw bot.py
```

### 方法2: 创建 Windows 服务

使用 [NSSM](https://nssm.cc/)：

```bash
nssm install PAMSBot "C:\Python311\python.exe" "C:\path\to\bot.py"
nssm start PAMSBot
```

## 自动分类规则

机器人内置智能分类识别，支持以下关键词：

| 分类 | 关键词 |
|-----|-------|
| 🍜 餐饮 | 午餐、晚餐、早餐、咖啡、奶茶、餐、饭、吃 |
| 🚗 交通 | 打车、地铁、公交、出租、滴滴、交通 |
| 🛍️ 购物 | 超市、淘宝、京东、购物、买 |
| 🎮 娱乐 | 电影、游戏、娱乐、KTV |
| 🏠 居住 | 房租、水电、物业、居住 |
| 💊 医疗 | 药、医院、医疗、看病 |
| 📚 教育 | 书、课程、培训、教育 |
| 📱 通讯 | 话费、流量、宽带、通讯 |
| 💰 工资 | 工资、薪水 |
| 💼 奖金 | 奖金、年终奖 |
| 🎁 红包 | 红包、转账 |
| 📈 投资 | 理财、投资、股票、基金 |

如需自定义，编辑 [bot.py](bot.py) 中的 `auto_categorize()` 函数。

## 依赖的后端 API

确保 PAMS 后端已实现以下接口：

| 接口 | 方法 | 说明 |
|-----|------|------|
| `/transactions` | POST | 创建交易记录 |
| `/transactions/recent` | GET | 获取最近记录 |
| `/transactions/monthly/{month}` | GET | 获取月度统计 |

## 安全建议

1. **保护 Bot Token**：不要泄露 Token，否则他人可以控制你的机器人
2. **限制访问**：可在代码中添加用户白名单，仅允许特定用户使用
3. **HTTPS API**：生产环境建议后端使用 HTTPS
4. **环境变量**：不要将 `.env` 文件提交到 Git

### 添加用户白名单

在 `bot.py` 中添加：

```python
ALLOWED_USERS = [123456789, 987654321]  # 替换为你的 Telegram User ID

async def check_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """检查用户权限"""
    user_id = update.effective_user.id
    if user_id not in ALLOWED_USERS:
        await update.message.reply_text("❌ 无权限使用此机器人")
        return False
    return True

# 在每个命令处理器中调用
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_user(update, context):
        return
    # ... 原有逻辑
```

## 常见问题

### Q1: 如何获取我的 Telegram User ID？

A: 发送 `/start` 给 [@userinfobot](https://t.me/userinfobot)

### Q2: Bot 不响应消息？

A: 检查：
1. Bot 是否正在运行（`python bot.py`）
2. `TELEGRAM_BOT_TOKEN` 是否正确
3. 网络连接是否正常

### Q3: 提示"记账失败"？

A: 检查：
1. 后端 API 是否运行（`http://pams.local/api`）
2. `PAMS_API_BASE` 配置是否正确
3. 后端是否已实现 `/transactions` 接口

### Q4: 如何部署到服务器？

A: 推荐使用 Docker Compose，或使用 systemd 服务（见上文）

### Q5: 能否支持多用户？

A: 可以，但需要为每个用户分配独立账户。可修改代码添加用户映射。

## 待实现功能

- [ ] 编辑已记账的记录
- [ ] 删除记录
- [ ] 导出月度账单（PDF/Excel）
- [ ] 预算管理和提醒
- [ ] 图表统计（分类占比、趋势）
- [ ] 多账户支持（切换账户）
- [ ] 定时提醒记账
- [ ] 语音记账

## 技术栈

- **Python 3.11+**
- **python-telegram-bot 20.8** - Telegram Bot API 库
- **requests** - HTTP 请求
- **python-dotenv** - 环境变量管理

## 更新日志

### v1.0.0 (2026-02-15)
- ✨ 初始版本发布
- 🚀 快速记账（解析消息）
- 📋 详细记账（对话流程）
- 📊 月度统计
- 📝 记录列表
- 🤖 智能分类识别

---

**License**: MIT
**Author**: PAMS Project
**Last Updated**: 2026-02-15
