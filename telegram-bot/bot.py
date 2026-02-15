#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PAMS Telegram Bot V3.0

流程A - 快速记账：直接发送 "-50 餐饮"
  → 默认 Santander / MXN，自动提交

流程B - 详细记账：/record
  → 输入金额备注 → 选收入/支出 → 选账户 → 选币种 → 选分类 → 完成

流程C - 交易识别：发送截图或 "买入 贵州茅台 100股 @1800"
  → OCR/解析 → 确认 → UPSERT 到持仓

每日推送：15:00 UTC (9:00 UTC-6) 推送持仓日报
通知轮询：每60秒检查待发送通知
"""

import json
import os
import re
import logging
from datetime import date, time as dt_time, timezone
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
OWNER_CHAT_ID = int(os.getenv('OWNER_CHAT_ID', '0'))

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

# 文字交易 → 账户推断映射
TRADE_ACCOUNT_HINTS = {
    'A股': 'tonghuashun',
    'a股': 'tonghuashun',
    '同花顺': 'tonghuashun',
    '广发': 'guangfa',
    'IBKR': 'ibkr',
    '盈透': 'ibkr',
    '美股': 'ibkr',
    '币安': 'binance',
    'binance': 'binance',
    'BTC': 'binance',
    'ETH': 'binance',
}

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

def submit_trade(trade_data):
    """提交持仓交易到API"""
    try:
        resp = requests.post(f"{API_BASE}/holdings/trade", json=trade_data, timeout=10)
        if resp.status_code == 200:
            return resp.json()
        return {'success': False, 'message': resp.json().get('detail', '未知错误')}
    except Exception as e:
        logger.error(f"交易提交失败: {e}")
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

def fmt_price(value, currency='CNY'):
    """格式化价格"""
    if value is None:
        return '—'
    prefix = {'CNY': '¥', 'USD': '$', 'MXN': '$'}.get(currency, '')
    return f"{prefix}{value:,.2f}"

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

# ==================== 流程C：交易识别 ====================

def parse_trade_text(text):
    """
    解析文字交易命令，返回 dict 或 None。

    支持格式：
      买入 贵州茅台 100股 @1800
      买入 贵州茅台 100 @1800
      卖出 AAPL 50 @195.5
      buy BTC 0.5 @97000
      sell ETH 2@3500
    """
    m = re.match(
        r'(?i)^\s*(买入|卖出|buy|sell)\s+'
        r'(.+?)\s+'
        r'(\d+(?:\.\d+)?)\s*(?:股|个|份)?\s*'
        r'(?:@|＠)\s*(\d+(?:\.\d+)?)',
        text.strip()
    )
    if not m:
        return None

    action = m.group(1).lower()
    name = m.group(2).strip()
    quantity = float(m.group(3))
    price = float(m.group(4))

    is_buy = action in ('买入', 'buy')

    # 推断账户
    account_code = None
    for hint, acc in TRADE_ACCOUNT_HINTS.items():
        if hint.lower() in name.lower() or hint.lower() in text.lower():
            account_code = acc
            break

    return {
        'action': '买入' if is_buy else '卖出',
        'name': name,
        'quantity': quantity,
        'price': price,
        'account_code': account_code,
    }


def format_trade_confirm(trade_data):
    """格式化交易确认消息"""
    acc_name = ACCOUNT_MAP.get(trade_data.get('account_code', ''), (trade_data.get('account_code', '未知'), ''))[0]
    currency = trade_data.get('currency', 'CNY')
    mv = trade_data.get('market_value')
    mv_str = fmt_price(mv, currency) if mv else '—'

    lines = ["📊 <b>交易确认</b>\n"]
    lines.append(f"🏦 账户：{acc_name}")
    lines.append(f"📈 名称：{trade_data.get('name', '—')}")
    if trade_data.get('symbol'):
        lines.append(f"📊 代码：{trade_data['symbol']}")
    if trade_data.get('major_category'):
        cat = trade_data['major_category']
        if trade_data.get('minor_category'):
            cat += f" > {trade_data['minor_category']}"
        lines.append(f"📂 类型：{cat}")
    lines.append(f"📦 数量：{trade_data.get('quantity', '—')}")
    if trade_data.get('buy_price') is not None:
        lines.append(f"💰 买入价：{fmt_price(trade_data['buy_price'], currency)}")
    if trade_data.get('current_price') is not None:
        lines.append(f"💵 现价：{fmt_price(trade_data['current_price'], currency)}")
    lines.append(f"💎 市值：{mv_str}")
    lines.append("\n确认入账？")

    return '\n'.join(lines)


async def handle_trade_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """收到图片 → OCR → 识别 → 显示确认"""
    try:
        from ocr import recognize_trade
    except ImportError:
        await update.message.reply_text("❌ OCR 模块未安装，请检查 Tesseract 是否已配置")
        return

    # 下载图片（取最大尺寸）
    photo = update.message.photo[-1]
    file = await photo.get_file()
    image_bytes = await file.download_as_bytearray()

    await update.message.reply_text("🔍 正在识别截图...")

    trade = recognize_trade(bytes(image_bytes))

    if not trade:
        await update.message.reply_text(
            "❌ 无法识别截图内容\n\n"
            "请尝试：\n"
            "• 确保截图清晰完整\n"
            "• 使用文字格式：<code>买入 贵州茅台 100股 @1800</code>",
            parse_mode='HTML'
        )
        return

    # 构建交易数据
    trade_dict = {
        'account_code': trade.account_code,
        'symbol': trade.symbol,
        'name': trade.name,
        'quantity': trade.quantity,
        'buy_price': trade.buy_price,
        'current_price': trade.current_price,
        'market_value': trade.market_value,
        'currency': trade.currency,
        'major_category': trade.major_category,
        'minor_category': trade.minor_category,
    }

    # 存储到 user_data 供确认回调使用
    context.user_data['pending_trade'] = trade_dict

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ 确认入账", callback_data="trade_yes"),
            InlineKeyboardButton("❌ 取消", callback_data="trade_no"),
        ]
    ])

    await update.message.reply_text(
        format_trade_confirm(trade_dict),
        reply_markup=keyboard,
        parse_mode='HTML'
    )


async def handle_trade_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """收到交易文字 → 解析 → 显示确认"""
    parsed = parse_trade_text(update.message.text)
    if not parsed:
        await update.message.reply_text(
            "❌ 格式不正确\n\n"
            "支持格式：\n"
            "<code>买入 贵州茅台 100股 @1800</code>\n"
            "<code>卖出 AAPL 50 @195.5</code>\n"
            "<code>buy BTC 0.5 @97000</code>",
            parse_mode='HTML'
        )
        return

    # 如果没推断出账户，让用户选择
    account_code = parsed['account_code']
    if not account_code:
        # 默认用同花顺（A股）
        account_code = 'tonghuashun'

    currency = ACCOUNT_MAP.get(account_code, ('', 'CNY'))[1]
    market_value = parsed['quantity'] * parsed['price']

    trade_dict = {
        'account_code': account_code,
        'symbol': None,
        'name': parsed['name'],
        'quantity': parsed['quantity'],
        'buy_price': parsed['price'] if parsed['action'] == '买入' else None,
        'current_price': parsed['price'],
        'market_value': market_value,
        'currency': currency,
        'major_category': None,
        'minor_category': None,
    }

    context.user_data['pending_trade'] = trade_dict

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ 确认入账", callback_data="trade_yes"),
            InlineKeyboardButton("❌ 取消", callback_data="trade_no"),
        ]
    ])

    await update.message.reply_text(
        format_trade_confirm(trade_dict),
        reply_markup=keyboard,
        parse_mode='HTML'
    )


async def on_trade_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """交易确认/取消回调"""
    query = update.callback_query
    await query.answer()

    if query.data == 'trade_no':
        await query.edit_message_text("❌ 已取消")
        context.user_data.pop('pending_trade', None)
        return

    trade_dict = context.user_data.pop('pending_trade', None)
    if not trade_dict:
        await query.edit_message_text("❌ 交易数据已过期，请重新操作")
        return

    result = submit_trade(trade_dict)

    if result.get('success'):
        holding = result.get('holding', {})
        pnl = holding.get('holding_pnl')
        pnl_str = ''
        if pnl is not None:
            pnl_str = f"\n📊 浮动盈亏：{fmt_price(pnl, trade_dict.get('currency', 'CNY'))}"

        await query.edit_message_text(
            f"✅ 持仓已更新！\n\n"
            f"📈 {trade_dict['name']}\n"
            f"📦 数量：{trade_dict['quantity']}\n"
            f"💎 市值：{fmt_price(holding.get('market_value'), trade_dict.get('currency', 'CNY'))}"
            f"{pnl_str}\n"
            f"📅 日期：{date.today().isoformat()}"
        )
    else:
        await query.edit_message_text(f"❌ 更新失败：{result.get('message')}")

# ==================== 查询命令 ====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(
        f"👋 你好 {update.effective_user.mention_html()}！\n\n"
        f"<b>快速记账</b>：直接发送金额\n"
        f"  <code>-50 餐饮</code> → Santander/MXN\n"
        f"  <code>+5000 工资</code> → Santander/MXN收入\n\n"
        f"<b>详细记账</b>：/record\n"
        f"  选账户 → 选币种 → 选分类\n\n"
        f"<b>交易识别</b>：发送截图或文字\n"
        f"  📸 发送券商持仓截图\n"
        f"  <code>买入 贵州茅台 100股 @1800</code>\n"
        f"  <code>卖出 AAPL 50 @195.5</code>\n\n"
        f"<b>其他</b>\n"
        f"  /stats - 本月统计\n"
        f"  /list - 最近记录\n"
        f"  /portfolio - 持仓概览"
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


async def portfolio_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """手动触发持仓概览"""
    try:
        resp = requests.get(f"{API_BASE}/holdings/summary?base_currency=CNY", timeout=15)
        if resp.status_code == 200:
            msg = format_portfolio_summary(resp.json())
            await update.message.reply_html(msg)
        else:
            await update.message.reply_text("❌ 获取持仓失败")
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("已取消")
    return ConversationHandler.END

# ==================== 每日推送 ====================

def format_portfolio_summary(data):
    """格式化持仓日报"""
    today_str = date.today().isoformat()
    lines = [f"📊 <b>每日持仓报告</b> ({today_str})\n"]

    # 汇总数据
    categories = data.get('categories', [])
    grand_value = sum(c.get('total_value', 0) for c in categories)
    grand_pnl = sum(c.get('total_pnl', 0) for c in categories)
    grand_daily = sum(c.get('daily_pnl', 0) for c in categories)
    grand_cost = grand_value - grand_pnl
    pnl_pct = (grand_pnl / grand_cost * 100) if grand_cost > 0 else 0

    lines.append(f"💰 总资产：¥{grand_value:,.0f}")

    pnl_sign = '+' if grand_pnl >= 0 else ''
    lines.append(f"📈 持仓盈亏：{pnl_sign}¥{grand_pnl:,.0f} ({pnl_sign}{pnl_pct:.2f}%)")

    daily_sign = '+' if grand_daily >= 0 else ''
    lines.append(f"📊 昨日变动：{daily_sign}¥{grand_daily:,.0f}")

    if categories:
        lines.append("\n📂 <b>分类汇总</b>")
        for c in categories:
            cv = c.get('total_value', 0)
            pct = (cv / grand_value * 100) if grand_value > 0 else 0
            lines.append(f"  • {c.get('major_category', '?')}：¥{cv:,.0f} ({pct:.1f}%)")

    return '\n'.join(lines)


async def daily_portfolio_push(context: ContextTypes.DEFAULT_TYPE):
    """15:00 UTC = 9:00 UTC-6：推送持仓日报"""
    if not OWNER_CHAT_ID:
        logger.warning("OWNER_CHAT_ID not set, skipping daily push")
        return

    try:
        resp = requests.get(f"{API_BASE}/holdings/summary?base_currency=CNY", timeout=15)
        if resp.status_code != 200:
            logger.error("Daily push: failed to fetch holdings summary: %s", resp.status_code)
            return

        msg = format_portfolio_summary(resp.json())
        await context.bot.send_message(chat_id=OWNER_CHAT_ID, text=msg, parse_mode='HTML')
        logger.info("Daily portfolio push sent to %s", OWNER_CHAT_ID)
    except Exception as e:
        logger.error("Daily push failed: %s", e)


async def check_pending_notifications(context: ContextTypes.DEFAULT_TYPE):
    """每60秒轮询待发送通知"""
    try:
        resp = requests.get(f"{API_BASE}/notifications/pending?limit=10", timeout=10)
        if resp.status_code != 200:
            return

        notifications = resp.json().get('notifications', [])
        for notif in notifications:
            notif_id = notif['id']
            chat_id = notif['chat_id']
            title = notif.get('title', '')
            body = notif.get('body', '')
            source = notif.get('source', 'system')

            msg = ''
            if title:
                msg = f"🔔 <b>{title}</b>\n\n{body}"
            else:
                msg = f"🔔 {body}"

            if source != 'system':
                msg += f"\n\n<i>来源：{source}</i>"

            try:
                await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode='HTML')
                # 标记已发送
                requests.patch(f"{API_BASE}/notifications/{notif_id}/sent", timeout=5)
                logger.info("Notification %d sent to %s", notif_id, chat_id)
            except Exception as e:
                logger.error("Failed to send notification %d: %s", notif_id, e)

    except Exception as e:
        # 静默处理轮询错误，避免日志刷屏
        if 'Connection' not in str(e):
            logger.error("Notification poll error: %s", e)

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

    # 命令
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('stats', stats))
    app.add_handler(CommandHandler('list', list_recent))
    app.add_handler(CommandHandler('portfolio', portfolio_command))
    app.add_handler(record_handler)

    # 交易图片（PHOTO）
    app.add_handler(MessageHandler(filters.PHOTO, handle_trade_image))

    # 交易文字（买入/卖出开头，必须在 quick_record 之前注册）
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.Regex(r'(?i)^\s*(买入|卖出|buy|sell)\s'),
        handle_trade_text
    ))

    # 交易确认回调
    app.add_handler(CallbackQueryHandler(on_trade_confirm, pattern=r'^trade_(yes|no)$'))

    # 快速记账：匹配以数字或+-开头的消息
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.Regex(r'^\s*[+-]?\d'),
        quick_record
    ))

    # 定时任务（需要 python-telegram-bot[job-queue]）
    if app.job_queue:
        # 每日 15:00 UTC (9:00 UTC-6) 推送持仓日报
        app.job_queue.run_daily(
            daily_portfolio_push,
            time=dt_time(hour=15, minute=0, tzinfo=timezone.utc),
            name='daily_portfolio_push'
        )
        # 每 60 秒轮询待发送通知
        app.job_queue.run_repeating(
            check_pending_notifications,
            interval=60,
            first=30,
            name='notification_poll'
        )
        logger.info("Job queue configured: daily push + notification poll")
    else:
        logger.warning("Job queue not available - install python-telegram-bot[job-queue]")

    logger.info("Bot V3.0 started (trade recognition + daily push)")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
