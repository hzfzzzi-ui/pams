#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PAMS Assistant Bot - Telegram ↔ Claude Code Bridge
接收用户消息 → 存入任务队列 → Claude Code处理 → 回传结果
"""

import os
import logging

import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------- Configuration ----------
API_BASE = os.getenv("PAMS_API_BASE", "http://pams-api:8000/api")
BOT_TOKEN = os.getenv("ASSISTANT_BOT_TOKEN", "")
OWNER_CHAT_ID = int(os.getenv("OWNER_CHAT_ID", "0"))
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "3"))
SITE_URL = os.getenv("SITE_URL", "http://pams.local")

# Track last delivered task ID
last_seen_id = 0

PAGES = [
    ("总览", "/"),
    ("策略", "/strategy.html"),
    ("观察池", "/watchlist.html"),
    ("持仓", "/holdings.html"),
    ("收支", "/cashflow.html"),
]


# ---------- Helpers ----------
def is_owner(update: Update) -> bool:
    """检查是否为机器人主人"""
    if OWNER_CHAT_ID == 0:
        return True  # 未设置限制时允许所有人
    return update.effective_chat.id == OWNER_CHAT_ID


def api_post(path, data=None):
    return requests.post(f"{API_BASE}{path}", json=data, timeout=10)


def api_get(path, params=None):
    return requests.get(f"{API_BASE}{path}", params=params, timeout=10)


def api_patch(path, data=None):
    return requests.patch(f"{API_BASE}{path}", json=data, timeout=10)


def api_delete(path):
    return requests.delete(f"{API_BASE}{path}", timeout=10)


# ---------- Command Handlers ----------
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = (
        "🤖 *PAMS Assistant Bot*\n\n"
        "发送任意文本消息，我会将指令传递给 Claude Code。\n"
        "处理完成后，结果会自动回复给你。\n\n"
        "命令列表：\n"
        "/status — 查看队列状态\n"
        "/pages — 查看所有页面链接\n\n"
        f"你的 Chat ID: `{chat_id}`"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await update.message.reply_text("⛔ 无权限")
        return

    try:
        resp = api_get("/tasks/pending")
        tasks = resp.json().get("tasks", [])
        count = len(tasks)

        if count == 0:
            await update.message.reply_text("📭 当前无待处理任务")
        else:
            lines = [f"📋 *{count} 个待处理任务：*\n"]
            for t in tasks[:5]:
                req = t["request"][:60] + ("..." if len(t["request"]) > 60 else "")
                lines.append(f"• #{t['id']} — {req}")
            if count > 5:
                lines.append(f"\n...还有 {count - 5} 个")
            await update.message.reply_text(
                "\n".join(lines), parse_mode="Markdown"
            )
    except Exception as e:
        logger.error("Status query failed: %s", e)
        await update.message.reply_text(f"❌ 查询失败: {e}")


async def cmd_pages(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await update.message.reply_text("⛔ 无权限")
        return

    buttons = [
        [InlineKeyboardButton(name, url=f"{SITE_URL}{path}")]
        for name, path in PAGES
    ]
    await update.message.reply_text(
        "📄 PAMS 页面：",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


# ---------- Message Handler ----------
async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await update.message.reply_text("⛔ 无权限")
        return

    text = (update.message.text or "").strip()
    if not text:
        return

    try:
        resp = api_post("/tasks", {
            "chat_id": update.effective_chat.id,
            "message_id": update.message.message_id,
            "request": text,
        })
        data = resp.json()

        if data.get("success"):
            task_id = data["task_id"]
            await update.message.reply_text(
                f"✅ 指令已入队 (#{task_id})\n等待 Claude Code 处理..."
            )
        else:
            await update.message.reply_text(
                f"❌ 入队失败: {data.get('detail', '未知错误')}"
            )
    except Exception as e:
        logger.error("Task creation failed: %s", e)
        await update.message.reply_text(f"❌ 提交失败: {e}")


# ---------- Background Polling ----------
async def poll_completed(ctx: ContextTypes.DEFAULT_TYPE):
    """轮询已完成任务，发送响应给用户"""
    global last_seen_id

    try:
        resp = api_get("/tasks/completed", {"since_id": last_seen_id})
        if resp.status_code != 200:
            return

        tasks = resp.json().get("tasks", [])
        for task in tasks:
            task_id = task["id"]
            chat_id = task["chat_id"]
            response_text = task.get("response", "（无响应内容）")
            page_links = task.get("page_links", [])
            message_id = task.get("message_id")

            # Build reply text
            msg = f"📨 *Claude Code 回复：*\n\n{response_text}"

            # Truncate if too long for Telegram (4096 limit)
            if len(msg) > 4000:
                msg = msg[:3997] + "..."

            # Build inline keyboard from page links
            keyboard = None
            if page_links:
                buttons = [
                    [InlineKeyboardButton(link["title"], url=link["url"])]
                    for link in page_links
                    if link.get("title") and link.get("url")
                ]
                if buttons:
                    keyboard = InlineKeyboardMarkup(buttons)

            # Send to user
            try:
                await ctx.bot.send_message(
                    chat_id=chat_id,
                    text=msg,
                    parse_mode="Markdown",
                    reply_to_message_id=message_id,
                    reply_markup=keyboard,
                )
            except Exception as send_err:
                # Retry without Markdown if parse fails
                try:
                    await ctx.bot.send_message(
                        chat_id=chat_id,
                        text=msg.replace("*", ""),
                        reply_to_message_id=message_id,
                        reply_markup=keyboard,
                    )
                except Exception as retry_err:
                    logger.error("Send failed for task #%d: %s", task_id, retry_err)

            # Acknowledge delivery
            try:
                api_delete(f"/tasks/{task_id}/ack")
            except Exception:
                pass

            last_seen_id = max(last_seen_id, task_id)

    except Exception as e:
        logger.error("Poll error: %s", e)


# ---------- Main ----------
def main():
    if not BOT_TOKEN:
        logger.error("ASSISTANT_BOT_TOKEN not set")
        return

    if OWNER_CHAT_ID == 0:
        logger.warning("OWNER_CHAT_ID not set — bot accepts messages from anyone")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("pages", cmd_pages))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Poll for completed tasks every POLL_INTERVAL seconds
    app.job_queue.run_repeating(poll_completed, interval=POLL_INTERVAL, first=5)

    logger.info(
        "PAMS Assistant Bot started (owner=%s, poll=%ds)",
        OWNER_CHAT_ID or "ANY",
        POLL_INTERVAL,
    )
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
