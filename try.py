# =========================================================
# 🤖 INSTAGRAM AUTOMATION SUITE (SPBOT5 STYLE)
# =========================================================

import asyncio
import sqlite3
import logging
import urllib.parse
import time
from typing import Dict, List
from concurrent.futures import ThreadPoolExecutor
import atexit

import requests
import instabot

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ================= CONFIG =================

TG_BOT_TOKEN = "7096827924:AAHRPqyxCNsDuA4NFZvzbJGKQ7BYtU_tNgE"
OWNER_TG_ID = 7510461579
DB_FILE = "bot_data.db"

EXECUTOR = ThreadPoolExecutor(max_workers=8)
atexit.register(EXECUTOR.shutdown)

# ================= LOG =================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

# ================= DB =================

def init_db():
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS accounts(
            id INTEGER PRIMARY KEY,
            username TEXT,
            password TEXT,
            session_id TEXT
        )
    """)
    con.commit()
    con.close()

def load_accounts():
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("SELECT username,password,session_id FROM accounts")
    rows = cur.fetchall()
    con.close()
    return [{"username":u,"password":p,"session_id":s} for u,p,s in rows]

def save_accounts(accs):
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("DELETE FROM accounts")
    for a in accs:
        cur.execute(
            "INSERT INTO accounts(username,password,session_id) VALUES(?,?,?)",
            (a["username"], a["password"], a["session_id"])
        )
    con.commit()
    con.close()

init_db()

# ================= STATE =================

STATE: Dict[str, object] = {
    "logged_in": False,
    "step": None,

    "accounts": load_accounts(),
    "current_account": 0,

    "groups": [],
    "targets": [],
    "messages": [],

    "send_count": 0,
    "sent": 0,
    "running": False,
    "task": None,
    "start_time": None
}

# ================= AUTH =================

def is_auth(uid:int)->bool:
    return uid == OWNER_TG_ID

# ================= IG HELPERS =================

def get_groups(bot) -> List[Dict]:
    try:
        s = bot.api.session
        s.headers.update({
            "User-Agent": "Instagram 289.0.0.77.109 Android",
            "X-IG-App-ID": "936619743392459"
        })

        r = s.get("https://i.instagram.com/api/v1/direct_v2/inbox/")
        data = r.json()

        threads = data.get("inbox", {}).get("threads", [])
        groups = []

        for t in threads:
            users = t.get("users", [])
            if not users:
                continue

            title = t.get("thread_title") or ", ".join(u["username"] for u in users[:3])

            groups.append({
                "thread_id": t["thread_id"],
                "title": title,
                "last": t.get("last_activity_at", 0)
            })

        # last active first
        groups.sort(key=lambda x: x["last"], reverse=True)
        return groups[:10]

    except Exception as e:
        logging.error(e)
        return []

# ================= SEND ENGINE =================

async def send_engine():
    acc = STATE["accounts"][STATE["current_account"]]
    bot = instabot.Bot()

    if acc["session_id"]:
        bot.api.session = requests.Session()
        bot.api.session.cookies.set(
            "sessionid", acc["session_id"], domain=".instagram.com"
        )
    else:
        bot.login(username=acc["username"], password=acc["password"])

    loop = asyncio.get_running_loop()
    STATE["sent"] = 0
    STATE["start_time"] = time.time()
    i = 0

    while STATE["running"]:
        jobs = []

        for tid in STATE["targets"]:
            msg = STATE["messages"][i % len(STATE["messages"])]
            i += 1

            jobs.append(
                loop.run_in_executor(EXECUTOR, bot.send_message, msg, tid)
            )

            if STATE["send_count"] > 0 and STATE["sent"] + len(jobs) >= STATE["send_count"]:
                break

        if not jobs:
            await asyncio.sleep(0.2)
            continue

        res = await asyncio.gather(*jobs, return_exceptions=True)
        for r in res:
            if not isinstance(r, Exception):
                STATE["sent"] += 1

        if STATE["send_count"] > 0 and STATE["sent"] >= STATE["send_count"]:
            break

        await asyncio.sleep(0.3)

    bot.logout()
    STATE["running"] = False

# ================= COMMANDS =================

async def start_cmd(u:Update,c:ContextTypes.DEFAULT_TYPE):
    if not is_auth(u.effective_user.id): return
    await u.message.reply_text(
        "🌟 ✨ *INSTAGRAM AUTOMATION SUITE* ✨\n\n"
        "⚡ /help - Show this command list\n"
        "📱 /login - Login to Instagram\n"
        "🔑 /slogin - Session ID login\n"
        "👀 /viewmyac - View saved accounts\n"
        "🔄 /setig <num> - Set default account\n"
        "💥 /attack - Start messaging task\n"
        "🛑 /stop - Stop task\n"
        "📊 /status - View system status\n\n"
        "🚀 *Send messages to your IG groups!*",
        parse_mode="Markdown"
    )

async def help_cmd(u:Update,c):
    await start_cmd(u,c)

async def slogin_cmd(u:Update,c):
    STATE["step"] = "session"
    await u.message.reply_text("🔑 Send Instagram sessionid:")

async def login_cmd(u:Update,c):
    STATE["step"] = "username"
    await u.message.reply_text("📱 Instagram username:")

async def viewmyac_cmd(u:Update,c):
    if not STATE["accounts"]:
        await u.message.reply_text("No accounts saved")
        return
    txt=["👀 *SAVED ACCOUNTS*"]
    for i,a in enumerate(STATE["accounts"],1):
        txt.append(f"{i}. {a['username']}")
    await u.message.reply_text("\n".join(txt),parse_mode="Markdown")

async def setig_cmd(u:Update,c):
    parts=u.message.text.split()
    if len(parts)<2 or not parts[1].isdigit():
        await u.message.reply_text("Usage: /setig <num>")
        return
    i=int(parts[1])-1
    if i<0 or i>=len(STATE["accounts"]):
        await u.message.reply_text("Invalid number")
        return
    STATE["current_account"]=i
    await u.message.reply_text("Default account set")

async def attack_cmd(u:Update,c):
    if not STATE["logged_in"]:
        await u.message.reply_text("Login first")
        return

    if not STATE["groups"]:
        await u.message.reply_text("No active groups found")
        return

    lines = [
        "📂 *LAST ACTIVE GROUPS*",
        "━━━━━━━━━━━━━━━━━━━━━━",
        ""
    ]
    for i,g in enumerate(STATE["groups"],1):
        lines.append(f"`{i}.` {g['title']}")

    lines.append("\n✍️ Send group numbers (example: `1,3`)")

    STATE["step"] = "groups"
    await u.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def stop_cmd(u:Update,c):
    STATE["running"] = False
    if STATE["task"]:
        STATE["task"].cancel()
        STATE["task"] = None
    await u.message.reply_text("Stopped")

async def status_cmd(u:Update,c):
    up = int(time.time()-STATE["start_time"]) if STATE["start_time"] else 0
    await u.message.reply_text(
        f"📊 STATUS\n\n"
        f"Running: {STATE['running']}\n"
        f"Sent: {STATE['sent']}\n"
        f"Uptime: {up}s"
    )

# ================= ROUTER =================

async def router(u:Update,c):
    if not is_auth(u.effective_user.id): return
    t = u.message.text.strip()

    if STATE["step"]=="session":
        sid = urllib.parse.unquote(t)
        bot = instabot.Bot()
        bot.api.session = requests.Session()
        bot.api.session.cookies.set("sessionid",sid,domain=".instagram.com")

        STATE["groups"] = get_groups(bot)
        STATE["accounts"].append({"username":"session","password":"session","session_id":sid})
        STATE["current_account"] = len(STATE["accounts"])-1
        save_accounts(STATE["accounts"])

        STATE["logged_in"] = True
        STATE["step"] = None
        await u.message.reply_text(f"Logged in. Groups found: {len(STATE['groups'])}")
        return

    if STATE["step"]=="username":
        STATE["tmp"] = t
        STATE["step"] = "password"
        await u.message.reply_text("🔒 Password:")
        return

    if STATE["step"]=="password":
        bot = instabot.Bot()
        bot.login(username=STATE["tmp"],password=t)

        STATE["groups"] = get_groups(bot)
        STATE["accounts"].append({"username":STATE["tmp"],"password":t,"session_id":None})
        STATE["current_account"] = len(STATE["accounts"])-1
        save_accounts(STATE["accounts"])

        STATE["logged_in"] = True
        STATE["step"] = None
        await u.message.reply_text("Logged in")
        return

    if STATE["step"]=="groups":
        idx=[]
        for x in t.split(","):
            if x.isdigit():
                i=int(x)-1
                if 0<=i<len(STATE["groups"]):
                    idx.append(i)

        if not idx:
            await u.message.reply_text("Invalid selection")
            return

        STATE["targets"]=[STATE["groups"][i]["thread_id"] for i in idx]
        chosen=", ".join(STATE["groups"][i]["title"] for i in idx)
        STATE["step"]="msg"
        await u.message.reply_text(f"✅ Selected:\n{chosen}\n\n📝 Send message:")
        return

    if STATE["step"]=="msg":
        STATE["messages"]=[t]
        STATE["step"]="count"
        await u.message.reply_text("Send count (0 = infinite):")
        return

    if STATE["step"]=="count" and t.isdigit():
        STATE["send_count"]=int(t)
        STATE["running"]=True
        STATE["step"]=None
        STATE["task"]=asyncio.create_task(send_engine())
        await u.message.reply_text("🚀 Sending started")
        return

# ================= MAIN =================

def main():
    app = Application.builder().token(TG_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",start_cmd))
    app.add_handler(CommandHandler("help",help_cmd))
    app.add_handler(CommandHandler("login",login_cmd))
    app.add_handler(CommandHandler("slogin",slogin_cmd))
    app.add_handler(CommandHandler("viewmyac",viewmyac_cmd))
    app.add_handler(CommandHandler("setig",setig_cmd))
    app.add_handler(CommandHandler("attack",attack_cmd))
    app.add_handler(CommandHandler("stop",stop_cmd))
    app.add_handler(CommandHandler("status",status_cmd))

    app.add_handler(MessageHandler(filters.TEXT,router))
    app.run_polling()

if __name__=="__main__":
    main()
