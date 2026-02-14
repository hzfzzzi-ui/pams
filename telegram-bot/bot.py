#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PAMS Telegram Bot - 收支记账助手

支持功能：
- /start - 开始使用
- /record - 快速记账
- /stats - 查看统计
- /list - 查看记录
- 直接发送金额进行记账（如：-50 午餐）
"""

import os
import re
import logging
from datetime import datetime, date
from typing import Optional
import requests

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# 配置日志
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# API 配置
API_BASE = os.getenv('PAMS_API_BASE', 'http://localhost:8090/api')
BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')

# 对话状态
WAITING_AMOUNT, WAITING_CATEGORY, WAITING_NOTE = range(3)

# 分类列表
CATEGORIES = {
    'expense': [
        '🍜 餐饮', '🚗 交通', '🛍️ 购物', '🎮 娱乐',
        '🏠 居住', '💊 医疗', '📚 教育', '📱 通讯', '📦 其他'
    ],
    'income': [
        '💰 工资', '💼 奖金', '🎁 红包', '📈 投资', '💵 其他'
    ]
}


class TransactionContext:
    """交易上下文，存储待提交的交易信息"""
    def __init__(self):
        self.amount: float = 0
        self.type: str = 'expense'  # expense | income
        self.category: str = ''
        self.note: str = ''
        self.date: str = date.today().isoformat()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /start 命令"""
    user = update.effective_user
    welcome_text = f"""
👋 你好 {user.mention_html()}！

我是 PAMS 记账助手，可以帮你快速记录收支。

🚀 <b>快速记账</b>
直接发送金额和备注，如：
• <code>-50 午餐</code> （支出50元）
• <code>+500 工资</code> （收入500元）

📋 <b>命令列表</b>
/record - 详细记账
/stats - 查看统计
/list - 查看记录
/help - 帮助信息

试试直接发送 <code>-25 咖啡</code> 开始记账吧！
    """

    await update.message.reply_html(welcome_text)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /help 命令"""
    help_text = """
📖 <b>使用帮助</b>

<b>1. 快速记账（推荐）</b>
直接发送消息，格式：<code>金额 备注</code>

示例：
• <code>-50 午餐</code> → 支出50元，分类：餐饮
• <code>-100 打车回家</code> → 支出100元，分类：交通
• <code>+5000 工资</code> → 收入5000元

<b>2. 详细记账</b>
使用 /record 命令，按步骤选择：
• 收入/支出
• 分类
• 金额
• 备注

<b>3. 查看统计</b>
/stats - 查看本月收支统计

<b>4. 查看记录</b>
/list - 查看最近记录

<b>提示</b>
• 支出用负数（-50）或不带符号（50）
• 收入用正数（+500）
• 系统会自动识别分类
    """

    await update.message.reply_html(help_text)


async def record_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """开始详细记账流程"""
    keyboard = [
        [
            InlineKeyboardButton("💸 支出", callback_data="type_expense"),
            InlineKeyboardButton("💰 收入", callback_data="type_income"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # 初始化交易上下文
    context.user_data['transaction'] = TransactionContext()

    await update.message.reply_text(
        "请选择记账类型：",
        reply_markup=reply_markup
    )

    return WAITING_AMOUNT


async def select_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """选择收入/支出类型"""
    query = update.callback_query
    await query.answer()

    tx_type = query.data.split('_')[1]  # type_expense -> expense
    context.user_data['transaction'].type = tx_type

    await query.edit_message_text(
        f"{'💸 支出' if tx_type == 'expense' else '💰 收入'} - 请输入金额（仅数字）："
    )

    return WAITING_CATEGORY


async def input_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """输入金额"""
    text = update.message.text.strip()

    # 验证金额
    try:
        amount = float(text)
        if amount <= 0:
            await update.message.reply_text("❌ 金额必须大于0，请重新输入：")
            return WAITING_CATEGORY

        tx: TransactionContext = context.user_data['transaction']
        tx.amount = amount

        # 显示分类选择
        categories = CATEGORIES[tx.type]
        keyboard = []
        for i in range(0, len(categories), 2):
            row = [
                InlineKeyboardButton(categories[i], callback_data=f"cat_{i}")
            ]
            if i + 1 < len(categories):
                row.append(
                    InlineKeyboardButton(categories[i+1], callback_data=f"cat_{i+1}")
                )
            keyboard.append(row)

        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            f"金额：¥{amount:.2f}\n\n请选择分类：",
            reply_markup=reply_markup
        )

        return WAITING_NOTE

    except ValueError:
        await update.message.reply_text("❌ 金额格式错误，请输入数字：")
        return WAITING_CATEGORY


async def select_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """选择分类"""
    query = update.callback_query
    await query.answer()

    tx: TransactionContext = context.user_data['transaction']
    cat_index = int(query.data.split('_')[1])
    category_full = CATEGORIES[tx.type][cat_index]

    # 提取分类名称（去掉emoji）
    category = category_full.split(' ')[1]
    tx.category = category

    await query.edit_message_text(
        f"金额：¥{tx.amount:.2f}\n"
        f"分类：{category_full}\n\n"
        f"请输入备注（或发送 /skip 跳过）："
    )

    return ConversationHandler.END


async def input_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """输入备注并提交"""
    tx: TransactionContext = context.user_data['transaction']
    tx.note = update.message.text.strip()

    # 提交交易
    result = await submit_transaction(tx)

    if result['success']:
        await update.message.reply_text(
            f"✅ 记账成功！\n\n"
            f"{'💸 支出' if tx.type == 'expense' else '💰 收入'}：¥{tx.amount:.2f}\n"
            f"分类：{tx.category}\n"
            f"备注：{tx.note or '无'}"
        )
    else:
        await update.message.reply_text(
            f"❌ 记账失败：{result['error']}"
        )

    # 清理上下文
    context.user_data.clear()

    return ConversationHandler.END


async def skip_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """跳过备注，直接提交"""
    tx: TransactionContext = context.user_data['transaction']

    # 提交交易
    result = await submit_transaction(tx)

    if result['success']:
        await update.message.reply_text(
            f"✅ 记账成功！\n\n"
            f"{'💸 支出' if tx.type == 'expense' else '💰 收入'}：¥{tx.amount:.2f}\n"
            f"分类：{tx.category}"
        )
    else:
        await update.message.reply_text(
            f"❌ 记账失败：{result['error']}"
        )

    # 清理上下文
    context.user_data.clear()

    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """取消记账"""
    await update.message.reply_text("❌ 已取消记账")
    context.user_data.clear()
    return ConversationHandler.END


async def quick_record(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """快速记账：解析消息并记账"""
    text = update.message.text.strip()

    # 解析格式：-50 午餐 或 +500 工资
    match = re.match(r'^([+-]?\d+(?:\.\d+)?)\s*(.*)$', text)

    if not match:
        return  # 不是记账格式，忽略

    amount_str, note = match.groups()
    amount = float(amount_str)

    # 判断收入/支出
    if amount > 0:
        tx_type = 'income'
    else:
        tx_type = 'expense'
        amount = abs(amount)

    # 自动识别分类
    category = auto_categorize(note, tx_type)

    # 构造交易
    tx = TransactionContext()
    tx.amount = amount
    tx.type = tx_type
    tx.category = category
    tx.note = note

    # 提交
    result = await submit_transaction(tx)

    if result['success']:
        await update.message.reply_text(
            f"✅ 记账成功！\n\n"
            f"{'💸 支出' if tx_type == 'expense' else '💰 收入'}：¥{amount:.2f}\n"
            f"分类：{category}\n"
            f"备注：{note or '无'}"
        )
    else:
        await update.message.reply_text(
            f"❌ 记账失败：{result['error']}"
        )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """查看统计"""
    month = date.today().strftime('%Y-%m')

    try:
        response = requests.get(f"{API_BASE}/transactions/monthly/{month}", timeout=5)
        response.raise_for_status()
        data = response.json()

        total_income = data.get('total_income', 0)
        total_expense = data.get('total_expense', 0)
        net = data.get('net', 0)

        stats_text = f"""
📊 <b>{month} 月度统计</b>

💰 总收入：¥{total_income:.2f}
💸 总支出：¥{total_expense:.2f}
💵 结余：¥{net:.2f}

{'📈 收大于支' if net > 0 else '📉 支大于收' if net < 0 else '⚖️ 收支平衡'}
        """

        await update.message.reply_html(stats_text)

    except Exception as e:
        logger.error(f"获取统计失败: {e}")
        await update.message.reply_text("❌ 获取统计失败，请检查后端API是否运行")


async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """查看最近记录"""
    try:
        response = requests.get(f"{API_BASE}/transactions/recent?limit=10", timeout=5)
        response.raise_for_status()
        records = response.json()

        if not records:
            await update.message.reply_text("📝 暂无记录")
            return

        list_text = "<b>📋 最近记录</b>\n\n"

        for record in records:
            amount = record['amount']
            symbol = '💰' if amount > 0 else '💸'
            sign = '+' if amount > 0 else ''

            list_text += (
                f"{symbol} {sign}¥{amount:.2f} - {record['category']}\n"
                f"   {record.get('description', '无备注')} ({record['date']})\n\n"
            )

        await update.message.reply_html(list_text)

    except Exception as e:
        logger.error(f"获取记录失败: {e}")
        await update.message.reply_text("❌ 获取记录失败，请检查后端API是否运行")


async def submit_transaction(tx: TransactionContext) -> dict:
    """提交交易到后端API"""
    try:
        # 构造请求数据
        data = {
            'amount': tx.amount if tx.type == 'income' else -tx.amount,
            'category': tx.category,
            'description': tx.note or None,
            'date': tx.date,
            'currency': 'CNY',
            'account': 'telegram'
        }

        response = requests.post(
            f"{API_BASE}/transactions",
            json=data,
            timeout=5
        )
        response.raise_for_status()

        return {'success': True}

    except Exception as e:
        logger.error(f"提交交易失败: {e}")
        return {'success': False, 'error': str(e)}


def auto_categorize(note: str, tx_type: str) -> str:
    """根据备注自动识别分类"""
    note_lower = note.lower()

    if tx_type == 'expense':
        if any(kw in note_lower for kw in ['午餐', '晚餐', '早餐', '餐', '饭', '吃', '咖啡', '奶茶']):
            return '餐饮'
        elif any(kw in note_lower for kw in ['打车', '地铁', '公交', '出租', '滴滴', '交通']):
            return '交通'
        elif any(kw in note_lower for kw in ['超市', '淘宝', '京东', '购物', '买']):
            return '购物'
        elif any(kw in note_lower for kw in ['电影', '游戏', '娱乐', 'ktv']):
            return '娱乐'
        elif any(kw in note_lower for kw in ['房租', '水电', '物业', '居住']):
            return '居住'
        elif any(kw in note_lower for kw in ['药', '医院', '医疗', '看病']):
            return '医疗'
        elif any(kw in note_lower for kw in ['书', '课程', '培训', '教育']):
            return '教育'
        elif any(kw in note_lower for kw in ['话费', '流量', '宽带', '通讯']):
            return '通讯'
        else:
            return '其他'
    else:  # income
        if any(kw in note_lower for kw in ['工资', '薪水']):
            return '工资'
        elif any(kw in note_lower for kw in ['奖金', '年终奖']):
            return '奖金'
        elif any(kw in note_lower for kw in ['红包', '转账']):
            return '红包'
        elif any(kw in note_lower for kw in ['理财', '投资', '股票', '基金']):
            return '投资'
        else:
            return '其他'


def main():
    """主函数"""
    if not BOT_TOKEN:
        logger.error("未设置 TELEGRAM_BOT_TOKEN 环境变量")
        return

    # 创建应用
    application = Application.builder().token(BOT_TOKEN).build()

    # 详细记账对话处理器
    record_conv_handler = ConversationHandler(
        entry_points=[CommandHandler('record', record_start)],
        states={
            WAITING_AMOUNT: [CallbackQueryHandler(select_type, pattern=r'^type_')],
            WAITING_CATEGORY: [MessageHandler(filters.TEXT & ~filters.COMMAND, input_amount)],
            WAITING_NOTE: [
                CallbackQueryHandler(select_category, pattern=r'^cat_'),
                MessageHandler(filters.TEXT & ~filters.COMMAND, input_note),
                CommandHandler('skip', skip_note),
            ],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )

    # 注册处理器
    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('help', help_command))
    application.add_handler(record_conv_handler)
    application.add_handler(CommandHandler('stats', stats_command))
    application.add_handler(CommandHandler('list', list_command))

    # 快速记账（处理所有文本消息）
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, quick_record)
    )

    # 启动
    logger.info("🤖 PAMS Telegram Bot 启动中...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
