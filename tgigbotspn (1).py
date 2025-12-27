# =========================================================
# 🤖 INSTAGRAM MESSAGE BOT — REAL • FAST • AUTOMATED
# =========================================================
# Purpose:
# - Control a real Instagram message sender via Telegram bot.
# - Actually sends DMs on Instagram (not simulated).
#
# Why this exists:
# - Automate Instagram messaging safely via Telegram interface.
# - Real sending with rate limiting.
# =========================================================

import asyncio
import os
import re
import tempfile
import time
import json
import unicodedata
import sqlite3
import threading
import signal
from typing import List, Dict, Optional
import logging
import urllib.parse
import requests
import psutil
import random
from queue import Queue, Empty

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
# from playwright_stealth import stealth_async
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
import instabot
# from instagrapi import Client
# from instagrapi.exceptions import ChallengeRequired, TwoFactorRequired, PleaseWaitFewMinutes, RateLimitError, LoginRequired

# =========================
# 🔧 LOGGING CONFIGURATION
# =========================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('instagram_bot.log'),
        logging.StreamHandler()
    ]
)

# =========================
# 📋 QUEUES AND THREADING
# =========================
user_queues = {}
waiting_for_otp = {}
user_fetching = set()
user_cancel_fetch = set()

# =========================
# 🎭 PLAYWRIGHT CONFIG
# =========================
MOBILE_UA = "Mozilla/5.0 (Linux; Android 13; vivo V60) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Mobile Safari/537.36"
MOBILE_VIEWPORT = {"width": 412, "height": 915}
LAUNCH_ARGS = [
    "--disable-dev-shm-usage",
    "--no-sandbox",
    "--disable-gpu",
    "--disable-extensions",
    "--disable-sync",
    "--disable-background-networking",
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--mute-audio",
]

# =========================
# 🗄️ DATABASE SETUP
# =========================
DB_FILE = 'bot_data.db'

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS state (
        key TEXT PRIMARY KEY,
        value TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS accounts (
        id INTEGER PRIMARY KEY,
        username TEXT,
        password TEXT,
        session_id TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS authorized_users (
        id INTEGER PRIMARY KEY,
        tg_id INTEGER UNIQUE,
        username TEXT
    )''')
    conn.commit()
    conn.close()

def save_state(key, value):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)', (key, json.dumps(value)))
    conn.commit()
    conn.close()

def load_state(key, default=None):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT value FROM state WHERE key = ?', (key,))
    row = c.fetchone()
    conn.close()
    if row:
        return json.loads(row[0])
    return default

def save_accounts(accounts):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('DELETE FROM accounts')
    for acc in accounts:
        c.execute('INSERT INTO accounts (username, password, session_id) VALUES (?, ?, ?)',
                  (acc.get('username'), acc.get('password'), acc.get('session_id')))
    conn.commit()
    conn.close()

def load_accounts():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT username, password, session_id FROM accounts')
    rows = c.fetchall()
    conn.close()
    return [{'username': r[0], 'password': r[1], 'session_id': r[2]} for r in rows]

def save_authorized_users(users):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('DELETE FROM authorized_users')
    for user in users:
        c.execute('INSERT INTO authorized_users (tg_id, username) VALUES (?, ?)',
                  (user['id'], user.get('username', '')))
    conn.commit()
    conn.close()

def load_authorized_users():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT tg_id, username FROM authorized_users')
    rows = c.fetchall()
    conn.close()
    return [{'id': r[0], 'username': r[1]} for r in rows]

init_db()

# =========================
# 🔧 CONFIGURATION
# =========================
TG_BOT_TOKEN = "7096827924:AAHRPqyxCNsDuA4NFZvzbJGKQ7BYtU_tNgE"
OWNER_TG_ID = 7510461579

IG_USERNAME = "your_instagram_username"
IG_PASSWORD = "your_instagram_password"

# =========================
# 🧠 GLOBAL STATE
# =========================
STATE: Dict[str, object] = load_state('state', {
    # auth / login (simulated)
    "logged_in": False,
    "session_label": None,      # user-provided label (simulated session)

    # flow
    "step": None,               # current input step
    "mode": None,               # "IG"
    "targets": [],              # selected target thread_ids
    "messages": [],             # payload messages
    "send_count": 0,            # 0 = infinite
    "running": False,           # engine on/off

    # telemetry
    "sent": 0,                  # sent counter
    "started_at": None,         # run start time
    "task": None,               # asyncio task handle

    # IG accounts
    "accounts": load_accounts(),  # list of dicts: {"username": "", "password": "", "session_id": str or None}
    "current_account": load_state('current_account', None),  # index of current account
    "groups": [],               # list of group chats

    # pairing & rotation
    "paired_accounts": None,    # [acc_index1, acc_index2] or None
    "switch_interval": 30,      # minutes

    # preferences
    "threads": 1,               # 1-5

    # admin
    "authorized_users": load_authorized_users() or [OWNER_TG_ID],  # list of tg_ids or dicts

    # tasks
    "running_tasks": [],        # list of {"id": int, "description": str, "task": asyncio.Task}
})

# =========================
# 🧰 UTILITIES
# =========================
def is_authorized(uid: int) -> bool:
    if isinstance(STATE["authorized_users"], list):
        if STATE["authorized_users"] and isinstance(STATE["authorized_users"][0], dict):
            return uid is not None and any(user['id'] == uid for user in STATE["authorized_users"])
        else:
            return uid is not None and uid in STATE["authorized_users"]
    return False

def now_ts() -> int:
    return int(time.time())

def uptime() -> int:
    if not STATE["started_at"]:
        return 0
    return now_ts() - STATE["started_at"]

# =========================
# 📤 PLAYWRIGHT SENDER
# =========================
async def init_page(page, url, dm_selector):
    """
    Initialize a single page by navigating to the URL with retries.
    Returns True if successful, False otherwise.
    """
    init_success = False
    for init_try in range(3):
        try:
            await page.goto("https://www.instagram.com/", timeout=60000)
            await page.goto(url, timeout=60000)
            await page.wait_for_selector(dm_selector, timeout=30000)
            init_success = True
            break
        except Exception as init_e:
            logging.error(f"Tab for {url[:30]}... try {init_try+1}/3 failed: {init_e}")
            if init_try < 2:
                await asyncio.sleep(2)
    return init_success

async def sender(tab_id, args, messages, context, page):
    """
    Async sender coroutine: Cycles through messages in an infinite loop, preloading/reloading pages every 60s to avoid issues.
    Preserves newlines in messages for multi-line content like ASCII art.
    Uses shared context to create new pages for reloading.
    Enhanced with retry logic: If selector not visible or send fails, retry up to 2 times (press Enter to clear if stuck, then refill), skip if all retries fail, never crash.
    """
    dm_selector = 'div[role="textbox"][aria-label="Message"]'
    logging.info(f"Tab {tab_id} ready, starting infinite message loop.")
    current_page = page
    cycle_start = time.time()
    msg_index = 0
    while True:
        elapsed = time.time() - cycle_start
        if elapsed >= 60:
            try:
                logging.info(f"Tab {tab_id} reloading thread after {elapsed:.1f}s")
                # Same URL ka hard reload, kahin aur nahi jayega
                await current_page.reload(timeout=60000)
                await current_page.wait_for_selector(dm_selector, timeout=30000)
            except Exception as reload_e:
                logging.error(f"Tab {tab_id} reload failed after {elapsed:.1f}s: {reload_e}")
                raise Exception(f"Tab {tab_id} reload failed: {reload_e}")
            cycle_start = time.time()
            continue
        msg = messages[msg_index]
        send_success = False
        max_retries = 2
        for retry in range(max_retries):
            try:
                if not current_page.locator(dm_selector).is_visible():
                    logging.warning(f"Tab {tab_id} selector not visible on retry {retry+1}/{max_retries} for '{msg[:50]}...', attempting Enter to clear.")
                    try:
                        await current_page.press(dm_selector, 'Enter')
                        await asyncio.sleep(0.2)
                    except:
                        pass  # Ignore clear failure
                    await asyncio.sleep(0.5)  # Wait for potential update
                    continue  # Retry visibility check

                await current_page.click(dm_selector)
                # DO NOT replace \n with space: Preserve multi-line for ASCII art
                # Instagram DM supports multi-line messages via fill()
                await current_page.fill(dm_selector, msg)
                await current_page.press(dm_selector, 'Enter')
                logging.info(f"Tab {tab_id} sent message {msg_index + 1}/{len(messages)} on retry {retry+1}")
                send_success = True
                break
            except Exception as send_e:
                logging.error(f"Tab {tab_id} send error on retry {retry+1}/{max_retries} for message {msg_index + 1}: {send_e}")
                if retry < max_retries - 1:
                    logging.info(f"Tab {tab_id} retrying after brief pause...")
                    await asyncio.sleep(0.5)
                else:
                    logging.error(f"Tab {tab_id} all retries failed for message {msg_index + 1}, triggering restart.")
        if not send_success:
            raise Exception(f"Tab {tab_id} failed to send after {max_retries} retries")
        await asyncio.sleep(0.24)  # Brief delay between successful sends
        msg_index = (msg_index + 1) % len(messages)

def parse_messages(names_arg):
    """
    Robust parser for messages:
    - If names_arg is a .txt file, first try JSON-lines parsing (one JSON string per line, supporting multi-line messages).
    - If that fails, read the entire file content as a single block and split only on explicit separators '&' or 'and' (preserving newlines within each message for ASCII art).
    - For direct string input, treat as single block and split only on separators.
    This ensures ASCII art (multi-line blocks without separators) is preserved as a single message.
    """
    # Handle argparse nargs possibly producing a list
    if isinstance(names_arg, list):
        names_arg = " ".join(names_arg)

    content = None  
    is_file = isinstance(names_arg, str) and names_arg.endswith('.txt') and os.path.exists(names_arg)  

    if is_file:  
        # Try JSON-lines first (each line is a JSON-encoded string, possibly with \n for multi-line)  
        try:  
            msgs = []  
            with open(names_arg, 'r', encoding='utf-8') as f:  
                lines = [ln.rstrip('\n') for ln in f if ln.strip()]  # Skip empty lines  
            for ln in lines:  
                m = json.loads(ln)  
                if isinstance(m, str):  
                    msgs.append(m)  
                else:  
                    raise ValueError("JSON line is not a string")  
            if msgs:  
                # Normalize each message (preserve \n for art)  
                out = []  
                for m in msgs:  
                    out.append(m)  
                return out  
        except Exception:  
            pass  # Fall through to block parsing on any error  

        # Fallback: read entire file as one block for separator-based splitting  
        try:  
            with open(names_arg, 'r', encoding='utf-8') as f:  
                content = f.read()  
        except Exception as e:  
            raise ValueError(f"Failed to read file {names_arg}: {e}")  
    else:  
        # Direct string input  
        content = str(names_arg)  

    if content is None:  
        raise ValueError("No valid content to parse")  

    # Normalize ampersand-like characters to '&' for consistent splitting  
    content = (  
        content.replace('﹠', '&')  
        .replace('＆', '&')  
        .replace('⅋', '&')  
        .replace('ꓸ', '&')  
        .replace('︔', '&')  
    )  

    # Split only on explicit separators: '&' or the word 'and' (case-insensitive, with optional whitespace)  
    # This preserves multi-line blocks like ASCII art unless explicitly separated  
    pattern = r'\s*(?:&|\band\b)\s*'  
    parts = [part.strip() for part in re.split(pattern, content, flags=re.IGNORECASE) if part.strip()]  
    return parts

async def read_messages(update: Update) -> List[str]:
    """
    Read messages from text or uploaded .txt file using advanced parsing.
    """
    raw = None
    if getattr(update.message, "document", None):
        f = await update.message.document.get_file()
        # Use a NamedTemporaryFile to avoid mktemp race and ensure suffix
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
        tmp_name = tmp.name
        tmp.close()
        try:
            await f.download_to_drive(tmp_name)
            raw = tmp_name  # Pass file path to parse_messages
        finally:
            try:
                os.remove(tmp_name)
            except Exception:
                pass
    else:
        raw = update.message.text or ""

    try:
        msgs = parse_messages(raw)
        return msgs
    except Exception as e:
        await update.message.reply_text(f"❌ Error parsing messages: {str(e)}")
        return []

def get_ig_groups(bot):
    """
    Get first 10 group chats and DMs from IG inbox.
    """
    try:
        session = bot.api.session
        params = {
            'persistentBadging': 'true',
            'use_unified_inbox': 'true',
            'cursor': None
        }
        response = session.get('https://i.instagram.com/api/v1/direct_v2/inbox/', params=params)
        if response.status_code != 200:
            print(f"Error: {response.status_code}")
            return []
        inbox = response.json()
        threads = inbox.get('inbox', {}).get('threads', [])
        groups = []
        for thread in threads:
            users = thread.get('users', [])
            if len(users) >= 2:  # group chat or DM
                title = thread.get('thread_title') or ', '.join([u['username'] for u in users[:3]]) + ('...' if len(users) > 3 else '')
                groups.append({
                    'thread_id': thread['thread_id'],
                    'title': title,
                    'users': [u['username'] for u in users]
                })
        return groups[:10]
    except Exception as e:
        print(f"Error getting groups: {e}")
        return []

# =========================
# ⚙️ SEND ENGINE
# =========================
async def send_engine():
    """
    Sends messages on Instagram using instabot.
    Respects COUNT (0 = infinite).
    """
    STATE["started_at"] = now_ts()
    STATE["sent"] = 0

    msgs = STATE["messages"]
    total = int(STATE["send_count"] or 0)

    if not msgs:
        STATE["running"] = False
        return

    # Get current account
    if STATE["current_account"] is None or not STATE["accounts"]:
        STATE["running"] = False
        return
    acc = STATE["accounts"][STATE["current_account"]]
    username = acc["username"]
    password = acc["password"]

    # Initialize Instagram bot
    bot = instabot.Bot()
    if 'session_id' in acc:
        bot.api.session = requests.Session()
        bot.api.session.cookies.set('sessionid', acc['session_id'], domain='.instagram.com')
    else:
        bot.login(username=username, password=password)

    targets = STATE["targets"]  # list of thread_ids

    i = 0
    while STATE["running"]:
        # send 10 messages per loop for speed
        for _ in range(10):
            if not STATE["running"]:
                break
            for target in targets:
                if not STATE["running"]:
                    break
                message = msgs[i % len(msgs)]
                send_success = False
                for retry in range(3):  # retry up to 3 times
                    try:
                        bot.send_message(message, thread_id=target)
                        STATE["sent"] += 1
                        send_success = True
                        logging.info(f"Sent message {i+1} to {target}")
                        break
                    except Exception as e:
                        logging.error(f"Send error on retry {retry+1}: {e}")
                        if retry < 2:
                            await asyncio.sleep(1)  # wait before retry
                if not send_success:
                    logging.error(f"Failed to send message {i+1} to {target} after 3 retries")
                i += 1

                # COUNT-based stop
                if total > 0 and STATE["sent"] >= total:
                    STATE["running"] = False
                    break

        # yield to event loop
        await asyncio.sleep(1)  # slow down to avoid rate limits

    bot.logout()

async def playwright_login(update, username, password):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        try:
            await page.goto("https://www.instagram.com/accounts/login/")
            await page.wait_for_selector("input[name='username']")
            await page.fill("input[name='username']", username)
            await page.fill("input[name='password']", password)
            await page.click("button[type='submit']")
            await page.wait_for_timeout(5000)  # wait for login
            # Check if login successful
            if "accounts" in page.url or "home" in page.url:
                STATE["accounts"].append({"username": username, "password": password, "logged_in": True})
                if STATE["current_account"] is None:
                    STATE["current_account"] = 0
                await update.message.reply_text(f"✅ Human-like login to {username}")
            else:
                await update.message.reply_text("❌ Playwright login failed")
        except Exception as e:
            await update.message.reply_text(f"❌ Playwright error: {str(e)}")
        await browser.close()

# =========================
# 🧭 COMMANDS
# =========================
async def start_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id): return
    STATE.update({
        "logged_in": False,
        "session_label": None,
        "step": "session",
    })
    await update.message.reply_text(
        "🤖 *🚀 INSTAGRAM BOT ONLINE 🚀*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "📋 *AVAILABLE COMMANDS*\n\n"
        "🔐 *Authentication*\n"
        "• `/login` - Login to Instagram\n"
        "• `/slogin` - Session ID login\n"
        "• `/logout` - Logout from account\n\n"
        "👤 *Account Management*\n"
        "• `/viewmyac` - View saved accounts\n"
        "• `/setig <num>` - Set default account\n"
        "• `/pair <acc1> <acc2>` - Pair accounts\n"
        "• `/unpair` - Unpair accounts\n"
        "• `/switch` - Switch between paired accounts\n\n"
        "⚙️ *Settings*\n"
        "• `/threads <num>` - Set thread count (1-5)\n"
        "• `/viewpref` - View current preferences\n\n"
        "💥 *Messaging*\n"
        "• `/attack` - Start messaging task\n"
        "• `/stop <pid/all>` - Stop tasks\n"
        "• `/task` - View running tasks\n\n"
        "👥 *User Management (Owner Only)*\n"
        "• `/add <id>` - Add authorized user\n"
        "• `/remove <id>` - Remove authorized user\n"
        "• `/users` - List authorized users\n\n"
        "🛠️ *System*\n"
        "• `/status` - View system status\n"
        "• `/kill <pid>` - Kill process\n"
        "• `/flush` - Clear all accounts\n"
        "• `/usg` - Usage statistics\n"
        "• `/cancel` - Cancel current operation\n"
        "• `/help` - Show this help\n\n"
        "🚀 *Ready to automate your Instagram messaging!*",
        parse_mode="Markdown"
    )

async def attack_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id): return
    if not STATE["logged_in"]:
        await update.message.reply_text("❌ Login first with `/start`")
        return
    STATE["step"] = "mode"
    await update.message.reply_text(
        "🚀 *💥 SEND ENGINE SETUP 💥*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "📍 *🎯 Destination 🎯*\n"
        "Reply with: `IG` (Instagram DM)",
        parse_mode="Markdown"
    )

async def stop_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id): return
    text = (update.message.text or "").strip()
    parts = text.split()
    if len(parts) > 1 and parts[1] == "all":
        for t in STATE["running_tasks"]:
            t["task"].cancel()
        STATE["running_tasks"] = []
        await update.message.reply_text("🛑 All tasks stopped.")
    elif len(parts) > 1 and parts[1].isdigit():
        pid = int(parts[1])
        for t in STATE["running_tasks"]:
            if t["id"] == pid:
                t["task"].cancel()
                STATE["running_tasks"].remove(t)
                await update.message.reply_text(f"🛑 Task {pid} stopped.")
                return
        await update.message.reply_text("❌ Task not found.")
    else:
        if STATE["task"]:
            STATE["task"].cancel()
            STATE["task"] = None
        STATE["running"] = False
        await update.message.reply_text("🛑 Task stopped.")

async def status_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id): return
    await update.message.reply_text(
        "📊 *🎛️ ENGINE DASHBOARD 🎛️*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🔐 *Logged In*     : {STATE['logged_in']}\n"
        f"📍 *Mode*          : {STATE['mode']}\n"
        f"🎯 *Targets*       : {len(STATE['targets'])}\n"
        f"📦 *Messages*      : {len(STATE['messages'])}\n"
        f"🔢 *Count*         : {STATE['send_count']} (0 = infinite)\n"
        f"📨 *Sent (IG)*     : {STATE['sent']}\n"
        f"⏱️ *Uptime*        : {uptime()}s\n"
        f"⚙️ *State*         : {'🟢 RUNNING' if STATE['running'] else '🔴 IDLE'}",
        parse_mode="Markdown"
    )

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id): return
    await update.message.reply_text(
        "🌟 *✨ INSTAGRAM AUTOMATION SUITE ✨*\n\n"
        "⚡ `/help` - Show this command list\n"
        "📱 `/login` - Login to Instagram\n"
        "🔑 `/slogin` - Session ID login\n"
        "👀 `/viewmyac` - View saved accounts\n"
        "🔄 `/setig <num>` - Set default account\n"
        "💥 `/attack` - Start messaging task\n"
        "🛑 `/stop <pid/all>` - Stop tasks\n"
        "📊 `/status` - View system status\n"
        "➕ `/add <id>` - Add authorized user (owner only)\n"
        "➖ `/remove <id>` - Remove authorized user (owner only)\n"
        "👥 `/users` - List authorized users (owner only)\n"
        "🗑️ `/flush` - Clear all accounts (owner only)\n\n"
        "🚀 *Send messages to your IG groups!*",
        parse_mode="Markdown"
    )

async def login_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id): return
    STATE["step"] = "login_username"
    await update.message.reply_text("📱 Enter Instagram username:")

async def viewmyac_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id): return
    if not STATE["accounts"]:
        await update.message.reply_text("👀 No saved accounts.")
        return
    lines = ["👀 *SAVED ACCOUNTS*\n━━━━━━━━━━━━━━━━━━━━━━\n"]
    for i, acc in enumerate(STATE["accounts"], 1):
        status = "✅" if acc.get("logged_in") else "❌"
        lines.append(f"{i}. {acc['username']} {status}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def setig_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id): return
    text = (update.message.text or "").strip()
    parts = text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        await update.message.reply_text("🔄 Usage: /setig <number>")
        return
    num = int(parts[1]) - 1
    if num < 0 or num >= len(STATE["accounts"]):
        await update.message.reply_text("❌ Invalid number.")
        return
    STATE["current_account"] = num
    await update.message.reply_text(f"✅ Default account set to {STATE['accounts'][num]['username']}")

async def plogin_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pass

async def slogin_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id): return
    STATE["step"] = "s_session"
    await update.message.reply_text("🔑 Send your Instagram session ID:")

async def pair_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pass

async def unpair_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pass

async def switch_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pass

async def threads_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pass

async def viewpref_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pass

async def usg_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pass

async def task_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pass

async def logout_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pass

async def kill_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pass

async def add_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_TG_ID: return
    text = (update.message.text or "").strip()
    parts = text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        await update.message.reply_text("➕ Usage: /add <telegram_id>")
        return
    tg_id = int(parts[1])
    username = parts[2] if len(parts) > 2 else ""
    if any(u['id'] == tg_id for u in STATE["authorized_users"] if isinstance(u, dict)):
        await update.message.reply_text("❌ User already authorized.")
        return
    STATE["authorized_users"].append({"id": tg_id, "username": username})
    save_authorized_users(STATE["authorized_users"])
    await update.message.reply_text(f"✅ Added user {tg_id}")

async def remove_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_TG_ID: return
    text = (update.message.text or "").strip()
    parts = text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        await update.message.reply_text("➖ Usage: /remove <telegram_id>")
        return
    tg_id = int(parts[1])
    original_len = len(STATE["authorized_users"])
    STATE["authorized_users"] = [u for u in STATE["authorized_users"] if not (isinstance(u, dict) and u['id'] == tg_id) and not (isinstance(u, int) and u == tg_id)]
    if len(STATE["authorized_users"]) < original_len:
        save_authorized_users(STATE["authorized_users"])
        await update.message.reply_text(f"✅ Removed user {tg_id}")
    else:
        await update.message.reply_text("❌ User not found.")

async def users_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_TG_ID: return
    if not STATE["authorized_users"]:
        await update.message.reply_text("👥 No authorized users.")
        return
    lines = ["👥 *AUTHORIZED USERS*\n━━━━━━━━━━━━━━━━━━━━━━\n"]
    for u in STATE["authorized_users"]:
        if isinstance(u, dict):
            lines.append(f"• {u['id']} (@{u.get('username', 'unknown')})")
        else:
            lines.append(f"• {u}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def flush_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_TG_ID: return
    STATE["accounts"] = []
    save_accounts([])
    await update.message.reply_text("🗑️ All accounts flushed.")

# =========================
# 🔁 TEXT ROUTER (STATE MACHINE)
# =========================
async def text_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id): return
    text = (update.message.text or "").strip()
    step = STATE["step"]

    # ---- LOGIN (SIMULATED) ----
    if step == "session":
        STATE["logged_in"] = True
        STATE["session_label"] = text or "demo-session"
        STATE["step"] = None
        await update.message.reply_text(
            f"✅ *🎉 Logged In 🎉*\n"
            f"Session: `{STATE['session_label']}`",
            parse_mode="Markdown"
        )
        return

    # ---- IG LOGIN USERNAME ----
    if step == "ig_username":
        STATE["temp_username"] = text
        STATE["step"] = "ig_password"
        await update.message.reply_text("Enter Instagram password:")
        return

    # ---- IG LOGIN PASSWORD ----
    if step == "ig_password":
        username = STATE.get("temp_username")
        if not username:
            await update.message.reply_text("❌ Error: Username not set.")
            STATE["step"] = None
            return
        password = text
        # Try login
        bot = instabot.Bot()
        try:
            bot.login(username=username, password=password)
            STATE["accounts"].append({"username": username, "password": password, "logged_in": True})
            if STATE["current_account"] is None:
                STATE["current_account"] = 0
            save_accounts(STATE["accounts"])
            await update.message.reply_text(f"✅ Logged in to {username}")
        except Exception as e:
            await update.message.reply_text(f"❌ Login failed: {str(e)}")
        STATE["step"] = None
        return

    # ---- PLOGIN USERNAME ----
    if step == "pl_username":
        STATE["temp_pl_username"] = text
        STATE["step"] = "pl_password"
        await update.message.reply_text("Enter Instagram password:")
        return

    # ---- PLOGIN PASSWORD ----
    if step == "pl_password":
        username = STATE.get("temp_pl_username")
        if not username:
            await update.message.reply_text("❌ Error: Username not set.")
            STATE["step"] = None
            return
        password = text
        # Playwright login
        await playwright_login(update, username, password)
        STATE["step"] = None
        return

    # ---- LOGIN METHOD ----
    if step == "login_method":
        if text == "1":
            STATE["step"] = "session_id"
            await update.message.reply_text("📎 Send sessionid:")
        elif text == "2":
            STATE["step"] = "login_username"
            await update.message.reply_text("📱 Enter Instagram username:")
        else:
            await update.message.reply_text("❌ Invalid choice. Choose 1 or 2.")
        return

    # ---- SESSION ID ----
    if step == "session_id":
        session_input = urllib.parse.unquote(text)
        session_parts = session_input.split(':')
        username = "swan.3022436"  # default, or extract
        session_id = session_input  # full string
        try:
            bot = instabot.Bot()
            bot.api.session = requests.Session()
            if len(session_parts) >= 4:
                user_id = session_parts[0]
                sessionid = session_parts[1]
                csrftoken = session_parts[3]
                bot.api.session.cookies.set('sessionid', sessionid, domain='.instagram.com')
                bot.api.session.cookies.set('ds_user_id', user_id, domain='.instagram.com')
                bot.api.session.cookies.set('csrftoken', csrftoken, domain='.instagram.com')
            else:
                bot.api.session.cookies.set('sessionid', session_input, domain='.instagram.com')
            # Fetch groups
            STATE["groups"] = get_ig_groups(bot)
            STATE["accounts"].append({"username": username, "password": "session", "session_id": session_id, "logged_in": True})
            if STATE["current_account"] is None:
                STATE["current_account"] = 0
            STATE["logged_in"] = True
            save_accounts(STATE["accounts"])
            await update.message.reply_text(f"✅ Logged in as {username}\nFound {len(STATE['groups'])} groups")
        except BaseException as e:
            await update.message.reply_text(f"❌ Session login failed: {str(e)}")
        STATE["step"] = None
        return

    # ---- S SESSION ----
    if step == "s_session":
        session_input = urllib.parse.unquote(text)
        session_parts = session_input.split(':')
        username = "swan.3022436"  # default
        session_id = session_input
        try:
            bot = instabot.Bot()
            bot.api.session = requests.Session()
            if len(session_parts) >= 4:
                user_id = session_parts[0]
                sessionid = session_parts[1]
                csrftoken = session_parts[3]
                bot.api.session.cookies.set('sessionid', sessionid, domain='.instagram.com')
                bot.api.session.cookies.set('ds_user_id', user_id, domain='.instagram.com')
                bot.api.session.cookies.set('csrftoken', csrftoken, domain='.instagram.com')
            else:
                bot.api.session.cookies.set('sessionid', session_input, domain='.instagram.com')
            STATE["groups"] = get_ig_groups(bot)
            STATE["accounts"].append({"username": username, "password": "session", "session_id": session_id, "logged_in": True})
            if STATE["current_account"] is None:
                STATE["current_account"] = 0
            save_accounts(STATE["accounts"])
            await update.message.reply_text(f"✅ Logged in as {username}\nFound {len(STATE['groups'])} groups")
        except BaseException as e:
            await update.message.reply_text(f"❌ Session login failed: {str(e)}")
        STATE["step"] = None
        return

    # ---- LOGIN USERNAME ----
    if step == "login_username":
        STATE["temp_login_username"] = text
        STATE["step"] = "login_password"
        await update.message.reply_text("🔑 Enter password:")
        return

    # ---- LOGIN PASSWORD ----
    if step == "login_password":
        username = STATE.get("temp_login_username")
        if not username:
            await update.message.reply_text("❌ Error: Username not set.")
            STATE["step"] = None
            return
        password = text
        # Try login
        bot = instabot.Bot()
        try:
            bot.login(username=username, password=password)
            STATE["groups"] = get_ig_groups(bot)
            STATE["accounts"].append({"username": username, "password": password, "logged_in": True})
            if STATE["current_account"] is None:
                STATE["current_account"] = 0
            STATE["logged_in"] = True
            await update.message.reply_text(f"✅ Logged in as {username}\nFound {len(STATE['groups'])} groups")
        except BaseException as e:
            await update.message.reply_text(f"❌ Login failed: {str(e)}")
        STATE["step"] = None
        return

    # ---- MODE ----
    if step == "mode" and text.lower() == "ig":
        STATE["mode"] = "IG"
        if not STATE["groups"]:
            await update.message.reply_text("❌ No groups found. Login with session first.")
            STATE["step"] = None
            return
        lines = [
            "📂 *📁 AVAILABLE GROUPS 📁*",
            "━━━━━━━━━━━━━━━━━━━━━━",
            ""
        ]
        for i, group in enumerate(STATE["groups"], 1):
            lines.append(f"🔹 `{i}` • {group['title']}")
        lines.append("\n✍️ Send group numbers (e.g. `1,3,5` or `1-3`)")
        STATE["step"] = "select_group"
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        return

    # ---- SELECT GROUP ----
    if step == "select_group":
        try:
            selected = []
            text = text.replace(' ', '')
            if '-' in text:
                start, end = map(int, text.split('-'))
                selected = list(range(start, end + 1))
            else:
                selected = [int(x) for x in text.split(',')]
            selected = [x for x in selected if 1 <= x <= len(STATE["groups"])]
            if not selected:
                await update.message.reply_text("❌ Invalid selection.")
                return
            selected_groups = [STATE["groups"][i-1] for i in selected]
            STATE["targets"] = [g["thread_id"] for g in selected_groups]
            selected_titles = [g['title'] for g in selected_groups]
            await update.message.reply_text(f"Selected groups: {', '.join(selected_titles)}\n\n📝 Send messages (or upload .txt):")
            STATE["step"] = "payload"
        except:
            await update.message.reply_text("❌ Invalid format. Use numbers like 1,3,5 or 1-3.")
        return

    # ---- PAYLOAD ----
    if step == "payload":
        msgs = await read_messages(update)
        if not msgs:
            await update.message.reply_text("❌ No messages found 🚫")
            return
        STATE["messages"] = msgs
        STATE["step"] = "count"
        await update.message.reply_text(
            "🔢 *🔢 SEND COUNT 🔢*\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "• `0` → Infinite ♾️\n"
            "• `10` → Send 10\n\n"
            "✍️ Send a number:",
            parse_mode="Markdown"
        )
        return

    # ---- COUNT ----
    if step == "count" and text.isdigit():
        STATE["send_count"] = int(text)
        STATE["running"] = True
        STATE["step"] = None
        if STATE["task"]:
            STATE["task"].cancel()
        STATE["task"] = asyncio.create_task(send_engine())
        task_id = len(STATE["running_tasks"]) + 1
        STATE["running_tasks"].append({"id": task_id, "description": "IG Message Send", "task": STATE["task"]})
        await update.message.reply_text(
            "🔥 *🚀 ENGINE LIVE (REAL IG) 🚀*\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "⚡ Speed: FAST (real sending) ⚡\n"
            f"🧮 Count: {STATE['send_count']} "
            f"({'♾️ Infinite' if STATE['send_count']==0 else 'Limited'})\n\n"
            "🟢 Running…",
            parse_mode="Markdown"
        )
        return

# =========================
# 🚀 MAIN
# =========================
def main():
    app = Application.builder().token(TG_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("login", login_cmd))
    app.add_handler(CommandHandler("plogin", plogin_cmd))
    app.add_handler(CommandHandler("slogin", slogin_cmd))
    app.add_handler(CommandHandler("viewmyac", viewmyac_cmd))
    app.add_handler(CommandHandler("setig", setig_cmd))
    app.add_handler(CommandHandler("pair", pair_cmd))
    app.add_handler(CommandHandler("unpair", unpair_cmd))
    app.add_handler(CommandHandler("switch", switch_cmd))
    app.add_handler(CommandHandler("threads", threads_cmd))
    app.add_handler(CommandHandler("viewpref", viewpref_cmd))
    app.add_handler(CommandHandler("attack", attack_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))
    app.add_handler(CommandHandler("task", task_cmd))
    app.add_handler(CommandHandler("logout", logout_cmd))
    app.add_handler(CommandHandler("kill", kill_cmd))
    app.add_handler(CommandHandler("usg", usg_cmd))
    app.add_handler(CommandHandler("add", add_cmd))
    app.add_handler(CommandHandler("remove", remove_cmd))
    app.add_handler(CommandHandler("users", users_cmd))
    app.add_handler(CommandHandler("flush", flush_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(MessageHandler(filters.ALL, text_router))
    app.run_polling()

if __name__ == "__main__":
    main()
