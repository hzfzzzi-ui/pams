#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PAMS Telegram Bot V2.1

流程A - 快速记账：直接发送 "-50 餐饮"
  → 默认 Santander / MXN，自动提交

流程B - 详细记账：/record
  → 输入金额备注 → 选收入/支出 → 选账户 → 选币种 → 选分类 → 完成
"""

import os
import re
import logging
from datetime import date
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

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

API_BASE = os.getenv('PAMS_API_BASE', 'http://pams-api:8000/api')
BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')

# 快速记账默认值
DEFAULT_ACCOUNT = 'santander'
DEFAULT_CURRENCY = 'MXN'

# 对话状态
(
    ST_AMOUNT,       # 输入金额备注
    ST_DIRECTION,    # 选收入/支出
    ST_ACCOUNT,      # 选账户
    ST_CURRENCY,     # 选币种
    ST_CATEGORY,     # 选分类
) = range(5)

# 11个标准账户
ACCOUNTS = [
    ('santander',         'Santander',  'MXN'),
    ('ibkr',              'IBKR',       'MXN'),
    ('binance',           '币安',        'USD'),
    ('jianhang',          '建行',        'CNY'),
    ('tencent_licaitong', '腾讯理财通',  'CNY'),
    ('tonghuashun',       '同花顺',      'CNY'),
    ('wechat',            '微信',        'CNY'),
    ('guangfa',           '广发',        'CNY'),
    ('huawei',            '华为账户',    'CNY'),
    ('gongjijin',         '公积金账户',  'CNY'),
    ('jiekuan',           '借款',        'CNY'),
]

ACCOUNT_MAP = {code: (name, cur) for code, name, cur in ACCOUNTS}

CURRENCIES = ['MXN', 'CNY', 'USD']

EXPENSE_CATS = ['餐饮', '交通', '购物', '娱乐', '居住', '医疗', '教育', '通讯', '其他']
INCOME_CATS = ['工资', '奖金', '红包', '投资收益', '其他']

# ==================== 工具函数 ====================

def parse_amount_text(text):
    """解析金额文本，返回 (amount, note) 或 None"""
    text = text.strip()
    m = re.match(r'^([+-]?\d+(?:\.\d+)?)\s*(.*)', text)
    if not m:
        return None
    return float(m.group(1)), m.group(2).strip()

def submit_transaction(tx_data):
    """提交交易到API"""
    try:
        resp = requests.post(f"{API_BASE}/transactions", json=tx_data, timeout=10)
        if resp.status_code == 200:
            return resp.json()
        return {'success': False, 'message': resp.json().get('detail', '未知错误')}
    except Exception as e:
        logger.error(f"提交失败: {e}")
        return {'success': False, 'message': str(e)}

def format_success(amount, currency, account_code, category, note):
    """格式化成功消息"""
    acc_name = ACCOUNT_MAP.get(account_code, (account_code, ''))[0]
    direction = '收入' if amount > 0 else '支出'
    return (
        f"✅ 记账成功！\n\n"
        f"💰 {direction}：{abs(amount)} {currency}\n"
        f"📁 账户：{acc_name}\n"
        f"📂 分类：{category}\n"
        f"📝 备注：{note or '-'}\n"
        f"📅 日期：{date.today().isoformat()}\n\n"
        f"🔄 已同步到资产持仓"
    )

# ==================== 流程A：快速记账 ====================

async def quick_record(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    快速记账：直接发送 "-50 餐饮" 或 "50 午餐"
    默认 Santander / MXN / 支出
    """
    parsed = parse_amount_text(update.message.text)
    if not parsed:
        return  # 不是金额格式，忽略

    amount, note = parsed

    # 负数或无符号都视为支出
    if amount > 0 and not update.message.text.strip().startswith('+'):
        amount = -amount

    # 尝试自动匹配分类
    category = note if note else '其他'
    # 检查note是否恰好是分类名
    all_cats = EXPENSE_CATS + INCOME_CATS
    matched_cat = None
    for cat in all_cats:
        if cat in note:
            matched_cat = cat
            break

    if matched_cat:
        category = matched_cat
        # 如果note就是分类名，备注留空
        if note == matched_cat:
            note = ''

    result = submit_transaction({
        'date': date.today().isoformat(),
        'amount': amount,
        'currency': DEFAULT_CURRENCY,
        'account_code': DEFAULT_ACCOUNT,
        'category': category,
        'description': note or category,
        'source': 'telegram'
    })

    if result.get('success'):
        await update.message.reply_text(
            format_success(amount, DEFAULT_CURRENCY, DEFAULT_ACCOUNT, category, note)
        )
    else:
        await update.message.reply_text(f"❌ 记账失败：{result.get('message')}")

# ==================== 流程B：详细记账 ====================

async def record_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """开始详细记账 - 输入金额和备注"""
    context.user_data['tx'] = {}

    await update.message.reply_text(
        "💰 请输入金额和备注：\n"
        "例如：<code>50 午餐</code> 或 <code>5000 工资</code>\n\n"
        "发送 /cancel 取消",
        parse_mode='HTML'
    )
    return ST_AMOUNT


async def on_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理金额输入 → 选择收入/支出"""
    parsed = parse_amount_text(update.message.text)
    if not parsed:
        await update.message.reply_text("❌ 请输入数字金额，如：<code>50 午餐</code>", parse_mode='HTML')
        return ST_AMOUNT

    amount, note = parsed
    context.user_data['tx']['raw_amount'] = abs(amount)
    context.user_data['tx']['note'] = note

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💸 支出", callback_data="dir_expense"),
            InlineKeyboardButton("💰 收入", callback_data="dir_income"),
        ],
        [InlineKeyboardButton("❌ 取消", callback_data="cancel")],
    ])

    await update.message.reply_text(
        f"金额：{abs(amount)}  备注：{note or '-'}\n\n"
        f"📌 收入还是支出？",
        reply_markup=keyboard
    )
    return ST_DIRECTION


async def on_direction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理收入/支出选择 → 选择账户"""
    query = update.callback_query
    await query.answer()

    if query.data == 'cancel':
        await query.edit_message_text("已取消")
        return ConversationHandler.END

    is_expense = query.data == 'dir_expense'
    raw = context.user_data['tx']['raw_amount']
    context.user_data['tx']['amount'] = -raw if is_expense else raw
    context.user_data['tx']['is_expense'] = is_expense

    # 账户键盘（3列）
    rows = []
    row = []
    for code, name, cur in ACCOUNTS:
        row.append(InlineKeyboardButton(f"{name}", callback_data=f"acc_{code}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("❌ 取消", callback_data="cancel")])

    direction_text = '支出' if is_expense else '收入'
    await query.edit_message_text(
        f"{'💸' if is_expense else '💰'} {direction_text} {raw}\n\n"
        f"📁 请选择账户：",
        reply_markup=InlineKeyboardMarkup(rows)
    )
    return ST_ACCOUNT


async def on_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理账户选择 → 选择币种"""
    query = update.callback_query
    await query.answer()

    if query.data == 'cancel':
        await query.edit_message_text("已取消")
        return ConversationHandler.END

    account_code = query.data.replace('acc_', '')
    context.user_data['tx']['account_code'] = account_code

    acc_name, acc_default_cur = ACCOUNT_MAP[account_code]

    # 币种键盘（默认币种排第一）
    ordered = [acc_default_cur] + [c for c in CURRENCIES if c != acc_default_cur]
    cur_buttons = [
        InlineKeyboardButton(
            f"{'✅ ' if c == acc_default_cur else ''}{c}",
            callback_data=f"cur_{c}"
        )
        for c in ordered
    ]

    await query.edit_message_text(
        f"📁 账户：{acc_name}\n\n"
        f"💱 请选择币种（默认 {acc_default_cur}）：",
        reply_markup=InlineKeyboardMarkup([
            cur_buttons,
            [InlineKeyboardButton("❌ 取消", callback_data="cancel")],
        ])
    )
    return ST_CURRENCY


async def on_currency(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理币种选择 → 选择分类"""
    query = update.callback_query
    await query.answer()

    if query.data == 'cancel':
        await query.edit_message_text("已取消")
        return ConversationHandler.END

    currency = query.data.replace('cur_', '')
    context.user_data['tx']['currency'] = currency

    is_expense = context.user_data['tx']['is_expense']
    cats = EXPENSE_CATS if is_expense else INCOME_CATS

    rows = []
    row = []
    for cat in cats:
        row.append(InlineKeyboardButton(cat, callback_data=f"cat_{cat}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("❌ 取消", callback_data="cancel")])

    tx = context.user_data['tx']
    acc_name = ACCOUNT_MAP[tx['account_code']][0]
    direction = '支出' if is_expense else '收入'

    await query.edit_message_text(
        f"{'💸' if is_expense else '💰'} {direction} {tx['raw_amount']} {currency}\n"
        f"📁 账户：{acc_name}\n\n"
        f"📂 请选择分类：",
        reply_markup=InlineKeyboardMarkup(rows)
    )
    return ST_CATEGORY


async def on_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理分类选择 → 提交"""
    query = update.callback_query
    await query.answer()

    if query.data == 'cancel':
        await query.edit_message_text("已取消")
        return ConversationHandler.END

    category = query.data.replace('cat_', '')
    tx = context.user_data['tx']

    result = submit_transaction({
        'date': date.today().isoformat(),
        'amount': tx['amount'],
        'currency': tx['currency'],
        'account_code': tx['account_code'],
        'category': category,
        'description': tx['note'] or category,
        'source': 'telegram'
    })

    if result.get('success'):
        await query.edit_message_text(
            format_success(tx['amount'], tx['currency'], tx['account_code'], category, tx['note'])
        )
    else:
        await query.edit_message_text(f"❌ 记账失败：{result.get('message')}")

    return ConversationHandler.END

# ==================== 查询命令 ====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(
        f"👋 你好 {update.effective_user.mention_html()}！\n\n"
        f"<b>快速记账</b>：直接发送金额\n"
        f"  <code>-50 餐饮</code> → Santander/MXN\n"
        f"  <code>+5000 工资</code> → Santander/MXN收入\n\n"
        f"<b>详细记账</b>：/record\n"
        f"  选账户 → 选币种 → 选分类\n\n"
        f"<b>其他</b>\n"
        f"  /stats - 本月统计\n"
        f"  /list - 最近记录"
    )


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    current_month = date.today().strftime('%Y-%m')
    try:
        resp = requests.get(f"{API_BASE}/transactions/monthly/{current_month}", timeout=10)
        if resp.status_code == 200:
            d = resp.json()
            text = f"📊 <b>{current_month} 月度统计</b>\n\n"
            text += f"💰 收入：{d['total_income']}\n"
            text += f"💸 支出：{d['total_expense']}\n"
            text += f"💵 结余：{d['net']}\n"
            if d['by_category']:
                text += "\n📂 <b>支出分类</b>\n"
                for c in d['by_category'][:5]:
                    text += f"  • {c['category']}: {c['amount']} ({c['count']}笔)\n"
            await update.message.reply_html(text)
        else:
            await update.message.reply_text("❌ 获取统计失败")
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")


async def list_recent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        resp = requests.get(f"{API_BASE}/transactions/recent?limit=10", timeout=10)
        if resp.status_code == 200:
            txs = resp.json()['transactions']
            if not txs:
                await update.message.reply_text("暂无记录")
                return
            text = "📝 <b>最近10条</b>\n\n"
            for t in txs:
                amt = float(t['amount'])
                emoji = '💰' if amt > 0 else '💸'
                text += f"{emoji} {t['date']} | {abs(amt)} {t['currency']} | {t['category']}\n"
                text += f"   {t.get('account_name', '')} - {t['description'] or '-'}\n\n"
            await update.message.reply_html(text)
        else:
            await update.message.reply_text("❌ 获取失败")
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("已取消")
    return ConversationHandler.END

# ==================== 主函数 ====================

def main():
    if not BOT_TOKEN:
        logger.error("缺少 TELEGRAM_BOT_TOKEN")
        return

    app = Application.builder().token(BOT_TOKEN).build()

    # 详细记账对话
    record_handler = ConversationHandler(
        entry_points=[CommandHandler('record', record_start)],
        states={
            ST_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_amount)],
            ST_DIRECTION: [CallbackQueryHandler(on_direction)],
            ST_ACCOUNT: [CallbackQueryHandler(on_account)],
            ST_CURRENCY: [CallbackQueryHandler(on_currency)],
            ST_CATEGORY: [CallbackQueryHandler(on_category)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
        per_message=False,
    )

    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('stats', stats))
    app.add_handler(CommandHandler('list', list_recent))
    app.add_handler(record_handler)

    # 快速记账：匹配以数字或+-开头的消息
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.Regex(r'^\s*[+-]?\d'),
        quick_record
    ))

    logger.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
