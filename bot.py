import os
import re
import html
import random
import string
import asyncio
import threading
from datetime import datetime, timezone

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from dotenv import load_dotenv
from pymongo import MongoClient

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
)


# =========================================================
# CONFIG
# =========================================================

load_dotenv()

BOT_TOKEN = os.getenv("RIZZLER_BOT_TOKEN")
BOT_USERNAME = "rizzlerescrow"

MONGO_URI = os.getenv("MONGO_URI")

ADMIN_IDS = [
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
]

# Optional admin aliases
ADMIN_ALIASES = {
    8258334055: "primaxog",
    8651783270: "gareeb_jimmy",
    8940820946: "A813ss",
}


# =========================================================
# MONGODB
# =========================================================

mongo_client = MongoClient(MONGO_URI) if MONGO_URI else None
mongo_db = mongo_client["escrow_bots"] if mongo_client else None

deals_coll = (
    mongo_db["deals_rizzlerescrow"]
    if mongo_db is not None
    else None
)

users_coll = (
    mongo_db["users_rizzlerescrow"]
    if mongo_db is not None
    else None
)


DEALS = {}
USERS = {}


# =========================================================
# LOAD DATA
# =========================================================

if deals_coll is not None:
    for doc in deals_coll.find({}):
        tid = doc.pop("_id")
        DEALS[tid] = doc

    print(
        f"✅ [rizzlerescrow] "
        f"{len(DEALS)} deal(s) loaded from MongoDB"
    )


if users_coll is not None:
    for doc in users_coll.find({}):
        uid = str(doc.pop("_id"))
        USERS[uid] = doc

    print(
        f"✅ [rizzlerescrow] "
        f"{len(USERS)} user(s) loaded from MongoDB"
    )


# =========================================================
# DATABASE HELPERS
# =========================================================

def save_deal(tid):
    if deals_coll is not None:
        deals_coll.update_one(
            {"_id": tid},
            {"$set": dict(DEALS[tid])},
            upsert=True,
        )


def save_user(user_id):
    uid = str(user_id)

    if users_coll is not None:
        users_coll.update_one(
            {"_id": uid},
            {"$set": dict(USERS[uid])},
            upsert=True,
        )


def register_user(update: Update):
    user = update.effective_user

    if not user:
        return

    uid = str(user.id)

    if uid not in USERS:
        USERS[uid] = {
            "user_id": user.id,
            "username": user.username or "",
            "first_name": user.first_name or "",
            "joined_at": now_iso(),
        }
    else:
        USERS[uid]["username"] = user.username or USERS[uid].get(
            "username", ""
        )
        USERS[uid]["first_name"] = user.first_name or USERS[uid].get(
            "first_name", ""
        )

    save_user(user.id)


# =========================================================
# BASIC HELPERS
# =========================================================

def now_iso():
    return datetime.now(timezone.utc).isoformat()


def esc(text):
    if text is None:
        return ""

    return html.escape(str(text), quote=False)


def fmt(amount):
    return f"₹{float(amount):,.2f}"


def clean_username(value):
    value = (value or "").strip()

    if not value:
        return "-"

    return value


def extract_amount(text):
    match = re.search(
        r"[\d,]+(?:\.\d+)?",
        text or "",
    )

    if not match:
        return 0.0

    value = match.group(0).replace(",", "")

    try:
        return float(value)
    except ValueError:
        return 0.0


def normalize_status(status):
    return str(status or "").upper()


# =========================================================
# PREMIUM CUSTOM EMOJIS
# =========================================================

# User-provided premium emoji IDs
PREMIUM_EMOJIS = {
    "stats": "6264777724741556322",
    "rank": "5258179403652801593",
    "deal": "5260535596941582167",
    "volume": "5258330865674494479",
    "escrow": "5323761960829862762",
}


def pe(key, fallback="⭐"):
    emoji_id = PREMIUM_EMOJIS.get(key)

    if not emoji_id:
        return fallback

    return (
        f'<tg-emoji emoji-id="{emoji_id}">'
        f"{fallback}"
        f"</tg-emoji>"
    )


# =========================================================
# DEAL FEES
# =========================================================

def calculate_fee(amount, is_exchange=False):
    if is_exchange:
        return amount * 0.025

    if amount < 200:
        return 10.0

    elif amount <= 500:
        return 20.0

    elif amount <= 2000:
        return amount * 0.04

    elif amount <= 3000:
        return amount * 0.035

    return amount * 0.03


# =========================================================
# DEAL ID
# =========================================================

def generate_tid():
    while True:
        tid = "#TID" + "".join(
            random.choices(
                string.ascii_uppercase + string.digits,
                k=6,
            )
        )

        if tid not in DEALS:
            return tid


# =========================================================
# USERNAME
# =========================================================

def resolve_username(update: Update):
    user = update.effective_user

    if not user:
        return "Unknown"

    if user.id in ADMIN_ALIASES:
        return "@" + ADMIN_ALIASES[user.id]

    if user.username:
        return "@" + user.username

    return user.first_name or str(user.id)


def display_user(value):
    value = clean_username(value)

    if value.startswith("@"):
        return value

    return value


# =========================================================
# DEAL FORM PARSER
# =========================================================

def parse_field(text, field_names):
    """
    Supports:
    SELLER :
    BUYER :
    DEAL DETAIL :
    DEAL AMOUNT :
    EXPECTED TIME TO COMPLETE DEAL :
    T/C :
    """

    for field in field_names:
        pattern = (
            rf"{re.escape(field)}"
            r"\s*:[ \t]*(.*)"
        )

        match = re.search(
            pattern,
            text or "",
            re.IGNORECASE,
        )

        if match:
            return match.group(1).strip()

    return ""


def parse_deal_form(text):
    seller = parse_field(
        text,
        ["SELLER"],
    )

    buyer = parse_field(
        text,
        ["BUYER"],
    )

    detail = parse_field(
        text,
        [
            "DEAL DETAIL",
            "DEAL DETAILS",
        ],
    )

    amount_text = parse_field(
        text,
        [
            "DEAL AMOUNT",
            "AMOUNT",
        ],
    )

    expected_time = parse_field(
        text,
        [
            "EXPECTED TIME TO COMPLETE DEAL",
            "EXPECTED TIME",
        ],
    )

    terms = parse_field(
        text,
        [
            "T/C (IF ANY)",
            "T/C",
            "TERMS",
            "TERMS & CONDITIONS",
        ],
    )

    amount = extract_amount(amount_text)

    return {
        "seller": clean_username(seller),
        "buyer": clean_username(buyer),
        "detail": detail or "Not provided",
        "amount": amount,
        "expected_time": expected_time or "Not specified",
        "terms": terms or "Nothing",
    }


# =========================================================
# DEAL STATISTICS
# =========================================================

def completed_deals():
    return [
        d
        for d in DEALS.values()
        if normalize_status(d.get("status"))
        == "COMPLETED"
    ]


def active_deals():
    return [
        d
        for d in DEALS.values()
        if normalize_status(d.get("status"))
        == "ACTIVE"
    ]


def total_deals():
    return len(DEALS)


def total_volume_inr():
    return sum(
        float(d.get("amount", 0))
        for d in DEALS.values()
    )


def total_released_inr():
    return sum(
        float(d.get("released", 0))
        for d in DEALS.values()
        if normalize_status(d.get("status"))
        == "COMPLETED"
    )


def total_escrows_by_user(username):
    username = username.lower()

    count = 0

    for deal in DEALS.values():
        escrowed_by = str(
            deal.get("escrowed_by", "")
        ).lower()

        if escrowed_by == username:
            count += 1

    return count


def get_user_deals(update: Update):
    user = update.effective_user

    if not user:
        return []

    username = resolve_username(update).lower()

    results = []

    for tid, deal in DEALS.items():

        seller = str(
            deal.get("seller", "")
        ).lower()

        buyer = str(
            deal.get("buyer", "")
        ).lower()

        escrowed_by = str(
            deal.get("escrowed_by", "")
        ).lower()

        if (
            username == seller
            or username == buyer
            or username == escrowed_by
            or str(user.id) in {
                str(deal.get("seller_id", "")),
                str(deal.get("buyer_id", "")),
                str(deal.get("escrower_id", "")),
            }
        ):
            results.append(
                (tid, deal)
            )

    return results


# =========================================================
# GLOBAL STATS
# =========================================================

def global_stats_text():
    total = total_deals()

    volume = total_volume_inr()

    active = len(active_deals())

    completed = len(completed_deals())

    return (
        f"{pe('stats', '📈')} "
        f"<b>RizzlerEscrow Global Statistics</b>\n"
        f"──────────────────\n"
        f"🔥 <b>Total Deals:</b> {total}\n"
        f"⚡ <b>Active Deals:</b> {active}\n"
        f"✅ <b>Completed Deals:</b> {completed}\n"
        f"🤑 <b>Total Volume:</b> {fmt(volume)}\n"
        f"──────────────────\n"
        f"📱 Escrow Bot: @{BOT_USERNAME}\n"
        f"💤 Securely powered by RizzlerEscrow"
    )


# =========================================================
# DEAL MESSAGE
# =========================================================

def build_deal_message(tid, deal):
    status = normalize_status(
        deal.get("status")
    )

    if status == "ACTIVE":
        status_text = "🟢 ACTIVE"

    elif status == "COMPLETED":
        status_text = "✅ COMPLETED"

    elif status == "CANCELLED":
        status_text = "❌ CANCELLED"

    else:
        status_text = status

    return (
        f"{pe('deal', '💼')} "
        f"<b>RIZZLERESCROW DEAL</b>\n"
        f"──────────────────\n"
        f"{pe('rank', '🆔')} "
        f"<b>Trade ID:</b> "
        f"<code>{esc(tid)}</code>\n\n"
        f"👤 <b>Seller:</b> "
        f"{esc(deal.get('seller', '-'))}\n"
        f"👤 <b>Buyer:</b> "
        f"{esc(deal.get('buyer', '-'))}\n\n"
        f"📝 <b>Deal Detail:</b>\n"
        f"{esc(deal.get('detail', '-'))}\n\n"
        f"💰 <b>Deal Amount:</b> "
        f"{fmt(deal.get('amount', 0))}\n"
        f"⏱ <b>Expected Completion:</b> "
        f"{esc(deal.get('expected_time', '-'))}\n"
        f"📜 <b>T/C:</b> "
        f"{esc(deal.get('terms', 'Nothing'))}\n\n"
        f"{pe('escrow', '🛡')} "
        f"<b>Escrowed By:</b> "
        f"{esc(deal.get('escrowed_by', '-'))}\n"
        f"📊 <b>Status:</b> {status_text}\n"
        f"──────────────────\n"
        f"📱 @{BOT_USERNAME}"
    )


# =========================================================
# /START
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update)

    await update.message.reply_text(
        f"{pe('escrow', '🛡')} "
        f"<b>RizzlerEscrow</b>\n\n"
        f"Secure escrow management bot.\n\n"
        f"📌 <b>User Commands</b>\n"
        f"/status - Your pending deals\n"
        f"/mydeals - Your complete deal history\n"
        f"/allstatus - Admin only\n"
        f"/deal TID - View deal details\n"
        f"/stats - Global escrow statistics\n\n"
        f"📱 @{BOT_USERNAME}",
        parse_mode=ParseMode.HTML,
    )


# =========================================================
# /ADD
# =========================================================

async def add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update)

    if not update.message.reply_to_message:
        await update.message.reply_text(
            "❌ Deal form ke message par reply karke /add bhejo."
        )
        return

    text = (
        update.message.reply_to_message.text
        or update.message.reply_to_message.caption
        or ""
    )

    parsed = parse_deal_form(text)

    if parsed["amount"] <= 0:
        await update.message.reply_text(
            "❌ Deal amount detect nahi hua.\n\n"
            "Form mein `DEAL AMOUNT : 500` jaisa field hona chahiye."
        )
        return

    is_exchange = (
        bool(context.args)
        and context.args[0].lower() == "exchange"
    )

    tid = generate_tid()

    creator_username = resolve_username(update)

    fee_amount = calculate_fee(
        parsed["amount"],
        is_exchange,
    )

    release_amount = (
        parsed["amount"] - fee_amount
    )

    deal = {
        "seller": parsed["seller"],
        "buyer": parsed["buyer"],
        "detail": parsed["detail"],
        "amount": parsed["amount"],
        "expected_time": parsed["expected_time"],
        "terms": parsed["terms"],
        "received": parsed["amount"],
        "fee": fee_amount,
        "release": release_amount,
        "released": 0,
        "status": "ACTIVE",
        "escrowed_by": creator_username,
        "escrower_id": update.effective_user.id,
        "chat_id": update.effective_chat.id,
        "created_at": now_iso(),
        "exchange": is_exchange,
    }

    DEALS[tid] = deal

    save_deal(tid)

    msg = build_deal_message(
        tid,
        deal,
    )

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔎 Open Deal",
                    callback_data=f"view:{tid}",
                )
            ]
        ]
    )

    await update.message.reply_text(
        msg,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )

    try:
        await update.message.delete()
    except Exception:
        pass


# =========================================================
# /CLOSE
# =========================================================

async def close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update)

    if not update.message.reply_to_message:
        await update.message.reply_text(
            "❌ Deal message par reply karke /close bhejo."
        )
        return

    reply_text = (
        update.message.reply_to_message.text
        or ""
    )

    match = re.search(
        r"Trade ID:\s*(#TID\w+)",
        reply_text,
        re.IGNORECASE,
    )

    if not match:
        await update.message.reply_text(
            "❌ Reply kiye gaye message mein Trade ID nahi mila."
        )
        return

    tid = match.group(1).upper()

    deal = DEALS.get(tid)

    if not deal:
        await update.message.reply_text(
            "❌ Deal not found."
        )
        return

    if normalize_status(
        deal.get("status")
    ) != "ACTIVE":
        await update.message.reply_text(
            "❌ Yeh deal already closed hai."
        )
        return

    is_cancel = (
        bool(context.args)
        and context.args[0].lower() == "cancel"
    )

    if is_cancel:
        released_value = 0.0
        deal["status"] = "CANCELLED"

    else:
        if context.args:
            released_value = extract_amount(
                context.args[0]
            )

            if released_value <= 0:
                released_value = float(
                    deal.get("release", 0)
                )
        else:
            released_value = float(
                deal.get("release", 0)
            )

        deal["status"] = "COMPLETED"

    deal["released"] = released_value
    deal["closed_at"] = now_iso()
    deal["closed_by"] = resolve_username(update)

    save_deal(tid)

    if is_cancel:
        msg = (
            f"❌ <b>Deal Cancelled</b>\n"
            f"──────────────────\n"
            f"{pe('rank', '🆔')} "
            f"<b>Trade ID:</b> "
            f"<code>{esc(tid)}</code>\n\n"
            f"💰 <b>Deal Amount:</b> "
            f"{fmt(deal.get('amount', 0))}\n"
            f"⚠️ <b>Released:</b> ₹0.00\n"
            f"📌 <b>Reason:</b> Deal cancelled\n\n"
            f"{pe('escrow', '🛡')} "
            f"<b>Closed By:</b> "
            f"{esc(resolve_username(update))}\n"
            f"──────────────────\n"
            f"📱 @{BOT_USERNAME}"
        )

    else:
        msg = (
            f"{pe('deal', '✅')} "
            f"<b>Deal Completed</b>\n"
            f"──────────────────\n"
            f"{pe('rank', '🆔')} "
            f"<b>Trade ID:</b> "
            f"<code>{esc(tid)}</code>\n\n"
            f"👤 <b>Buyer:</b> "
            f"{esc(deal.get('buyer', '-'))}\n"
            f"👤 <b>Seller:</b> "
            f"{esc(deal.get('seller', '-'))}\n\n"
            f"💰 <b>Deal Amount:</b> "
            f"{fmt(deal.get('amount', 0))}\n"
            f"📤 <b>Total Released:</b> "
            f"{fmt(released_value)}\n\n"
            f"{pe('escrow', '🛡')} "
            f"<b>Closed By:</b> "
            f"{esc(resolve_username(update))}\n"
            f"──────────────────\n"
            f"❤️ Please drop your vouch for "
            f"@{BOT_USERNAME}"
        )

    await update.message.reply_text(
        msg,
        parse_mode=ParseMode.HTML,
    )

    try:
        await update.message.delete()
    except Exception:
        pass


# =========================================================
# /STATUS
# USER = OWN ACTIVE/PENDING
# ADMIN = ALL ACTIVE/PENDING
# =========================================================

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update)

    if update.effective_user.id in ADMIN_IDS:
        deals = [
            (tid, d)
            for tid, d in DEALS.items()
            if normalize_status(d.get("status"))
            == "ACTIVE"
        ]

        title = (
            f"{pe('stats', '📈')} "
            f"<b>Admin Pending Deals</b>"
        )

    else:
        deals = [
            (tid, d)
            for tid, d in get_user_deals(update)
            if normalize_status(d.get("status"))
            == "ACTIVE"
        ]

        title = (
            f"{pe('deal', '📋')} "
            f"<b>My Pending Deals</b>"
        )

    if not deals:
        await update.message.reply_text(
            title
            + "\n\n"
            + "📭 No pending/active deals.",
            parse_mode=ParseMode.HTML,
        )
        return

    lines = [
        title,
        "──────────────────",
    ]

    for tid, deal in deals:
        lines.append(
            f"{pe('rank', '🆔')} "
            f"<code>{esc(tid)}</code>\n"
            f"👤 {esc(deal.get('buyer', '-'))} "
            f"↔ {esc(deal.get('seller', '-'))}\n"
            f"💰 {fmt(deal.get('amount', 0))}\n"
            f"📊 ACTIVE\n"
            f""
        )

    lines.append(
        f"──────────────────\n"
        f"📱 @{BOT_USERNAME}"
    )

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
    )


# =========================================================
# /MYDEALS
# =========================================================

async def mydeals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update)

    deals = get_user_deals(update)

    if not deals:
        await update.message.reply_text(
            "📭 Tumhare account se koi deal linked nahi hai."
        )
        return

    lines = [
        f"{pe('deal', '💼')} "
        f"<b>My Deal History</b>",
        "──────────────────",
    ]

    for tid, deal in deals:
        status = normalize_status(
            deal.get("status")
        )

        icon = {
            "ACTIVE": "🟢",
            "COMPLETED": "✅",
            "CANCELLED": "❌",
        }.get(status, "⚪")

        lines.append(
            f"{icon} <code>{esc(tid)}</code>\n"
            f"👤 {esc(deal.get('buyer', '-'))} "
            f"↔ {esc(deal.get('seller', '-'))}\n"
            f"💰 {fmt(deal.get('amount', 0))}\n"
            f"📊 {status}\n"
        )

    lines.append(
        f"──────────────────\n"
        f"📱 @{BOT_USERNAME}"
    )

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
    )


# =========================================================
# /ALLSTATUS
# ADMIN ONLY
# =========================================================

async def allstatus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update)

    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text(
            "❌ Ye command sirf admin ke liye hai."
        )
        return

    if not DEALS:
        await update.message.reply_text(
            "📭 Koi deal record nahi hai."
        )
        return

    lines = [
        f"{pe('stats', '📈')} "
        f"<b>RizzlerEscrow — All Deals</b>",
        "──────────────────",
        f"🔥 <b>Total Deals:</b> {len(DEALS)}",
        f"🟢 <b>Pending:</b> {len(active_deals())}",
        f"✅ <b>Completed:</b> {len(completed_deals())}",
        f"🤑 <b>Total Volume:</b> "
        f"{fmt(total_volume_inr())}",
        "──────────────────",
    ]

    for tid, deal in DEALS.items():

        status_value = normalize_status(
            deal.get("status")
        )

        lines.append(
            f"<code>{esc(tid)}</code> — "
            f"<b>{status_value}</b>\n"
            f"👤 {esc(deal.get('buyer', '-'))} "
            f"↔ {esc(deal.get('seller', '-'))}\n"
            f"💰 {fmt(deal.get('amount', 0))}\n"
            f"🛡 {esc(deal.get('escrowed_by', '-'))}\n"
        )

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
    )


# =========================================================
# /DEAL
# =========================================================

async def deal_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update)

    if not context.args:
        await update.message.reply_text(
            "Usage:\n"
            "/deal TIDXXXXXX"
        )
        return

    tid = context.args[0].upper()

    if not tid.startswith("#TID"):
        tid = "#" + tid

    deal = DEALS.get(tid)

    if not deal:
        await update.message.reply_text(
            "❌ Deal not found."
        )
        return

    msg = build_deal_message(
        tid,
        deal,
    )

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔄 Refresh",
                    callback_data=f"view:{tid}",
                )
            ]
        ]
    )

    await update.message.reply_text(
        msg,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


# =========================================================
# CALLBACK — OPEN DEAL
# =========================================================

async def callback_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    await query.answer()

    data = query.data or ""

    if not data.startswith("view:"):
        return

    tid = data.split(":", 1)[1].upper()

    deal = DEALS.get(tid)

    if not deal:
        await query.edit_message_text(
            "❌ Deal not found."
        )
        return

    msg = build_deal_message(
        tid,
        deal,
    )

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔄 Refresh",
                    callback_data=f"view:{tid}",
                )
            ]
        ]
    )

    try:
        await query.edit_message_text(
            msg,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
    except Exception:
        pass


# =========================================================
# /STATS
# PUBLIC GLOBAL STATS
# =========================================================

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update)

    await update.message.reply_text(
        global_stats_text(),
        parse_mode=ParseMode.HTML,
    )


# =========================================================
# /ADMIN
# =========================================================

async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update)

    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text(
            "❌ Admin access required."
        )
        return

    active = len(active_deals())
    completed = len(completed_deals())

    msg = (
        f"{pe('stats', '📈')} "
        f"<b>RizzlerEscrow Admin Panel</b>\n"
        f"──────────────────\n"
        f"👥 <b>Registered Users:</b> "
        f"{len(USERS)}\n"
        f"🔥 <b>Total Deals:</b> "
        f"{len(DEALS)}\n"
        f"🟢 <b>Pending:</b> "
        f"{active}\n"
        f"✅ <b>Completed:</b> "
        f"{completed}\n"
        f"🤑 <b>Total Volume:</b> "
        f"{fmt(total_volume_inr())}\n"
        f"──────────────────\n\n"
        f"<b>Admin Commands</b>\n"
        f"/status — Pending deals\n"
        f"/allstatus — All deals\n"
        f"/broadcast message — Broadcast\n"
        f"/stats — Global stats\n"
    )

    await update.message.reply_text(
        msg,
        parse_mode=ParseMode.HTML,
    )


# =========================================================
# /BROADCAST
# ADMIN ONLY
# =========================================================

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update)

    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text(
            "❌ Ye command sirf admin ke liye hai."
        )
        return

    if not context.args:
        await update.message.reply_text(
            "Usage:\n"
            "/broadcast Your message here"
        )
        return

    message = " ".join(context.args)

    sent = 0
    failed = 0

    for uid, user_data in USERS.items():

        user_id = user_data.get("user_id")

        if not user_id:
            continue

        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    f"📢 <b>RizzlerEscrow Announcement</b>\n"
                    f"──────────────────\n\n"
                    f"{esc(message)}\n\n"
                    f"──────────────────\n"
                    f"📱 @{BOT_USERNAME}"
                ),
                parse_mode=ParseMode.HTML,
            )

            sent += 1

        except Exception:
            failed += 1

    await update.message.reply_text(
        f"📢 <b>Broadcast Finished</b>\n\n"
        f"✅ Sent: {sent}\n"
        f"❌ Failed: {failed}",
        parse_mode=ParseMode.HTML,
    )


# =========================================================
# KEEP ALIVE SERVER
# =========================================================

def start_dummy_server():

    port = int(
        os.getenv(
            "PORT",
            "10000",
        )
    )

    class Handler(BaseHTTPRequestHandler):

        def do_GET(self):
            self.send_response(200)
            self.end_headers()

            self.wfile.write(
                b"RizzlerEscrow bot is running"
            )

        def do_HEAD(self):
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(
        ("0.0.0.0", port),
        Handler,
    )

    threading.Thread(
        target=server.serve_forever,
        daemon=True,
    ).start()

    print(
        f"✅ HTTP server listening on port {port}"
    )


# =========================================================
# MAIN
# =========================================================

def main():

    if not BOT_TOKEN:
        raise RuntimeError(
            "❌ RIZZLER_BOT_TOKEN missing in .env"
        )

    start_dummy_server()

    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(
            asyncio.new_event_loop()
        )

    app = (
        Application
        .builder()
        .token(BOT_TOKEN)
        .build()
    )

    # Public
    app.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    app.add_handler(
        CommandHandler(
            "stats",
            stats,
        )
    )

    app.add_handler(
        CommandHandler(
            "status",
            status,
        )
    )

    app.add_handler(
        CommandHandler(
            "mydeals",
            mydeals,
        )
    )

    app.add_handler(
        CommandHandler(
            "deal",
            deal_command,
        )
    )

    # Deal management
    app.add_handler(
        CommandHandler(
            "add",
            add,
        )
    )

    app.add_handler(
        CommandHandler(
            "close",
            close,
        )
    )

    # Admin
    app.add_handler(
        CommandHandler(
            "admin",
            admin,
        )
    )

    app.add_handler(
        CommandHandler(
            "allstatus",
            allstatus,
        )
    )

    app.add_handler(
        CommandHandler(
            "broadcast",
            broadcast,
        )
    )

    # Inline buttons
    app.add_handler(
        CallbackQueryHandler(
            callback_handler
        )
    )

    print(
        "✅ RizzlerEscrow Bot Running..."
    )

    app.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
