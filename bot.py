import os
import re
import html
import asyncio
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from dotenv import load_dotenv
from pymongo import MongoClient, ReturnDocument
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

load_dotenv()

# ===========================
# Config (.env se aata hai)
# ===========================
# RIZZLER_BOT_TOKEN=xxxx
# MONGO_URI=xxxx
# ADMIN_IDS=123,456   -> ye "OWNERS" hai, sirf ye naye bot-admin add/remove kar sakte hai

BOT_TOKEN = os.getenv("RIZZLER_BOT_TOKEN")
BRAND = "@rizzlerxescrow"
PROVIDER = "@rizzlerxescrow"

MONGO_URI = os.getenv("MONGO_URI")
OWNER_IDS = set(
    int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()
)

# In "limited" secondary accounts se /add ya /close chale to "Escrowed By" me
# inka username nahi, mapped MAIN username dikhega.
ADMIN_ALIASES = {
    8258334055: "primaxog",
    8651783270: "gareeb_jimmy",
    8940820946: "A813ss",
}

mongo_client = MongoClient(MONGO_URI) if MONGO_URI else None
mongo_db = mongo_client["escrow_bots"] if mongo_client else None
coll = mongo_db["deals_rizzlerxescrow"] if mongo_db is not None else None
meta_coll = mongo_db["meta_rizzlerxescrow"] if mongo_db is not None else None
admins_coll = mongo_db["bot_admins_rizzlerxescrow"] if mongo_db is not None else None

DEALS = {}

if coll is not None:
    for doc in coll.find({}):
        tid = doc.pop("_id")
        DEALS[tid] = doc
    print(f"✅ [rizzlerxescrow] {len(DEALS)} deal(s) Mongo se load hui")

# ---- Bot-admin set (owners + dynamically added admins) ----
BOT_ADMINS = set(OWNER_IDS)
if admins_coll is not None:
    for doc in admins_coll.find({}):
        BOT_ADMINS.add(doc["_id"])
    print(f"✅ [rizzlerxescrow] {len(BOT_ADMINS)} bot admin(s) load hue")


def save_deal(tid):
    if coll is not None:
        coll.update_one({"_id": tid}, {"$set": dict(DEALS[tid])}, upsert=True)


def is_owner(uid):
    return uid in OWNER_IDS


def is_admin(uid):
    return uid in BOT_ADMINS or is_owner(uid)


def admin_only_allowed(update: Update):
    """Admin commands sirf private chat me, aur sirf admin/owner ke liye."""
    if update.effective_chat.type != "private":
        return False
    return is_admin(update.effective_user.id)


# ===========================
# Sequential Trade ID: DL-RIZZ-1, DL-RIZZ-2, ...
# ===========================

def next_trade_id():
    if meta_coll is not None:
        doc = meta_coll.find_one_and_update(
            {"_id": "trade_counter"},
            {"$inc": {"seq": 1}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        seq = doc["seq"]
    else:
        seq = len(DEALS) + 1

    tid = f"DL-RIZZ-{seq}"
    while tid in DEALS:  # safety, collision na ho
        seq += 1
        tid = f"DL-RIZZ-{seq}"
    return tid


# ===========================
# Helpers
# ===========================

def esc(text):
    if text is None:
        return ""
    return html.escape(str(text), quote=False)


def fmt(amount, currency="INR"):
    if currency in ("USDT", "TON"):
        return f"{amount:,.2f} {currency}"
    symbol = {"INR": "₹", "USD": "$"}.get(currency, "")
    return f"{symbol}{amount:,.2f}"


def extract_amount(text):
    match = re.search(r"[\d,]+(?:\.\d+)?", text or "")
    if not match:
        return 0.0
    value = match.group(0).replace(",", "")
    try:
        return float(value)
    except ValueError:
        return 0.0


def resolve_username(update: Update):
    user_id = update.effective_user.id
    if user_id in ADMIN_ALIASES:
        return "@" + ADMIN_ALIASES[user_id]
    return (
        "@" + update.effective_user.username
        if update.effective_user.username
        else update.effective_user.first_name
    )


# Bold unicode (Mathematical Sans-Bold) helpers
_UP = ord('𝗔') - ord('A')
_LOW = ord('𝗮') - ord('a')
_DIG = ord('𝟬') - ord('0')


def normalize_bold(text):
    out = []
    for ch in text:
        code = ord(ch)
        if ord('𝗔') <= code <= ord('𝗭'):
            out.append(chr(code - _UP))
        elif ord('𝗮') <= code <= ord('𝘇'):
            out.append(chr(code - _LOW))
        elif ord('𝟬') <= code <= ord('𝟵'):
            out.append(chr(code - _DIG))
        else:
            out.append(ch)
    return "".join(out)


# ===========================
# Premium Emoji IDs
# ===========================
# Ye IDs Telegram Premium custom-emoji document IDs hain. Har entry me "character"
# (jaise ⭐, ❤️) sirf fallback hai un clients ke liye jinke paas Premium nahi hai —
# actual visual wahi custom emoji hoga jiska ID diya gaya hai.
#
# Agar koi ID galat / expired ho jaaye to Telegram sirf fallback character dikha
# dega (crash nahi hoga). Apni khud ki custom emoji IDs nikalne ke liye:
#   1) Us emoji ko kisi message me bhejo jisme HTML/entities dikhne wala export ho
#      (ya koi "emoji id finder" utility bot use karo jo message forward karke
#      custom_emoji entities se ID nikaalta hai).
#   2) Wahan se mile document_id ko yaha neeche waali dict me daal do.
#
# Neeche di gayi IDs me se check / trade / escrow verify ho chuki hain (working).
PE = {
    "star1": "5181422544162391976",
    "star2": "5258179403652801593",
    "heart": "5260535596941582167",
    "chat": "5258330865674494479",
    "peach": "5323761960829862762",
    "bolt": "5938539885907415367",
    "globe": "6041705726206808304",
    "fire": "5420315771991497307",
    "chart": "5774022692642492953",
    "coin": "5884428842780594914",
    "money": "6039802097916974085",
    "cash": "5893473283696759404",
    "mobile": "6152069549442208798",
    "zzz": "5895266423952904371",
    "check": "5197474765387864959",    # ✅ verified working
    "trade": "5936017305585586269",    # 🆔 verified working
    "escrow": "5920052658743283381",   # 🛡 verified working
    "star3": "5879785854284599288",    # ⭐ extra verified id
}


def pe(emoji, key):
    emoji_id = PE.get(key)
    if emoji_id:
        return f'<tg-emoji emoji-id="{emoji_id}">{emoji}</tg-emoji>'
    return emoji


# ===========================
# CHARGES (amount ke hisaab se slabs)
# ===========================

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
    else:
        return amount * 0.03


# ===========================
# Dashboard views
# ===========================

def main_menu_kb():
    rows = [
        [InlineKeyboardButton("✦ My Stats", callback_data="menu:my_stats")],
        [InlineKeyboardButton("★ My Deals Info", callback_data="menu:my_deals")],
        [InlineKeyboardButton("➤ My Pending Deals", callback_data="menu:pending")],
        [InlineKeyboardButton("✓ Escrow Global Stats", callback_data="menu:global")],
    ]
    return InlineKeyboardMarkup(rows)


def status_kb():
    """Keyboard jo /status ke saath jaata hai — private aur group dono me kaam karta hai."""
    rows = [
        [InlineKeyboardButton("★ My Deals Info", callback_data="menu:my_deals")],
        [InlineKeyboardButton("➤ My Pending Deals", callback_data="menu:pending")],
        [InlineKeyboardButton("🔄 Refresh", callback_data="refresh:my_stats")],
    ]
    return InlineKeyboardMarkup(rows)


def back_refresh_kb(refresh_target):
    rows = [
        [InlineKeyboardButton("🔄 Refresh", callback_data=f"refresh:{refresh_target}")],
        [InlineKeyboardButton("➤ Back", callback_data="menu:back")],
    ]
    return InlineKeyboardMarkup(rows)


def welcome_text(first_name):
    return (
        f"{pe('⭐️', 'star1')} <b>Welcome {esc(first_name)}!</b>\n"
        "╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍\n"
        f"{pe('❤️', 'heart')} Escrow Bot for {BRAND}\n"
        f"{pe('💬', 'chat')} Provided by {PROVIDER}\n\n"
        f"{pe('🍑', 'peach')} <b>This is Your Personal Dashboard:</b>\n"
        "╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍\n"
        f"Select the option below {pe('⚡️', 'bolt')}\n"
        "╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍"
    )


def global_stats_text():
    completed = [d for d in DEALS.values() if d.get("status") == "COMPLETED"]
    totals = {"TON": 0.0, "USDT": 0.0, "INR": 0.0}
    for d in completed:
        cur = d.get("currency", "INR")
        totals[cur] = totals.get(cur, 0.0) + d.get("amount", 0.0)

    lines = [
        f"{pe('🌐', 'globe')} <b>Escrow Global Statistics</b>",
        "──────────────────",
        f"{pe('🔥', 'fire')} Total Deals: {len(completed)}\n",
        f"{pe('📈', 'chart')} <b>Total Volume:</b>",
        f"  {pe('🪙', 'coin')} - {totals['TON']:g} TON",
        f"  {pe('💰', 'money')} - {totals['USDT']:g} USDT",
        f"  {pe('🤑', 'cash')} - {totals['INR']:g} ₹",
        "──────────────────",
        f"{pe('📱', 'mobile')} Escrow Bot for {BRAND}",
        f"{pe('💤', 'zzz')} Provided by {PROVIDER}",
    ]
    return "\n".join(lines)


# ---- Leaderboard / rank ----

def _is_today(iso_ts):
    if not iso_ts:
        return False
    try:
        ts = datetime.fromisoformat(iso_ts)
    except ValueError:
        return False
    return ts.date() == datetime.now(timezone.utc).date()


def build_leaderboard(today_only=False):
    board = {}
    for d in DEALS.values():
        if d.get("status") != "COMPLETED":
            continue
        if today_only and not _is_today(d.get("completed_at")):
            continue
        user = d.get("escrowed_by", "-")
        entry = board.setdefault(user, {"deals": 0, "volume": 0.0})
        entry["deals"] += 1
        entry["volume"] += d.get("amount", 0.0)
    return board


def get_rank(username, board, by="deals"):
    ranked = sorted(board.items(), key=lambda kv: kv[1][by], reverse=True)
    for i, (user, _) in enumerate(ranked, start=1):
        if user == username:
            return i
    return len(ranked) + 1


def my_stats_text(update: Update):
    username = resolve_username(update)
    first_name = update.effective_user.first_name

    mine = [d for d in DEALS.values() if d.get("escrowed_by") == username]
    completed = [d for d in mine if d.get("status") == "COMPLETED"]
    active = [d for d in mine if d.get("status") == "ACTIVE"]

    totals = {"TON": 0.0, "USDT": 0.0, "INR": 0.0}
    for d in completed:
        cur = d.get("currency", "INR")
        totals[cur] = totals.get(cur, 0.0) + d.get("amount", 0.0)

    board = build_leaderboard(today_only=False)
    rank = get_rank(username, board, by="deals")

    return (
        f"{pe('📈', 'chart')} <b>{esc(first_name)} Deal stats !</b>\n"
        "──────────────────\n"
        f"{pe('🚀', 'chart')} Rank ➤ #{rank}\n\n"
        f"{pe('🔥', 'fire')} Active deals ➤ {len(active)}\n\n"
        f"{pe('✅', 'check')} Total Escrow's ➤ {len(completed)}\n\n"
        f"{pe('⚡', 'bolt')} Total Volume :\n"
        f"  {pe('🪙', 'coin')} ➤ {totals['TON']:g} TON\n"
        f"  {pe('💰', 'money')} ➤ {totals['USDT']:g} USDT\n"
        f"  {pe('🤑', 'cash')} ➤ {totals['INR']:g} ₹\n"
        "──────────────────\n"
        f"{pe('📱', 'mobile')} Escrow Bot for {BRAND}\n"
        f"{pe('💤', 'zzz')} Provided by {PROVIDER} !"
    )


# ---- My Deals Info: paginated list + detail view ----

PAGE_SIZE = 6


def deal_status_display(status):
    return {
        "ACTIVE": "🟡 PENDING",
        "COMPLETED": "✅ DONE",
        "CANCELLED": "❌ CANCELLED",
    }.get(status, status)


def deal_detail_text(tid, deal):
    lines = [
        f"Your Deal-{esc(tid)} Info !",
        "──────────────────",
        f"➥ Status: {deal_status_display(deal.get('status', '-'))}",
        f"➥ Buyer: {esc(deal.get('buyer', '-'))}",
        f"➥ Seller: {esc(deal.get('seller', '-'))}",
        f"➥ Amount: {fmt(deal.get('amount', 0), deal.get('currency', 'INR'))}",
        f"➥ Fees: {deal.get('fee_percent', 0):.1f}%",
        f"➥ Escrowed by: {esc(deal.get('escrowed_by', '-'))}",
    ]

    if deal.get("created_at"):
        dt = datetime.fromisoformat(deal["created_at"])
        lines.append(f"➥ Start Time: {dt.strftime('%H:%M:%S')}")
        lines.append(f"     [ {dt.strftime('%d %B %Y')} ]")

    if deal.get("completed_at"):
        dt2 = datetime.fromisoformat(deal["completed_at"])
        lines.append(f"➥ End Time: {dt2.strftime('%H:%M:%S')}")
        lines.append(f"     [ {dt2.strftime('%d %B %Y')} ]")

    lines += [
        "──────────────────",
        f"{pe('📱', 'mobile')} Escrow Bot for {BRAND}",
        f"{pe('💤', 'zzz')} Provided by {PROVIDER}",
    ]
    return "\n".join(lines)


def my_deals_header_text(update: Update):
    first_name = update.effective_user.first_name
    return (
        f"{pe('♡', 'heart')} <b>{esc(first_name)} All deals info !</b>\n"
        "──────────────────\n"
        "Select the deal below for info :\n"
        "──────────────────"
    )


def my_deals_ids(update: Update):
    username = resolve_username(update)
    ids = [tid for tid, d in DEALS.items() if d.get("escrowed_by") == username]
    return list(reversed(ids))  # naye deals upar


def my_deals_kb(update: Update, page=0):
    ids = my_deals_ids(update)
    start = page * PAGE_SIZE
    chunk = ids[start:start + PAGE_SIZE]

    rows = [[InlineKeyboardButton(tid, callback_data=f"dealview:{tid}:{page}")] for tid in chunk]

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ Prev", callback_data=f"dealspage:{page-1}"))
    if start + PAGE_SIZE < len(ids):
        nav.append(InlineKeyboardButton("Next ▶", callback_data=f"dealspage:{page+1}"))
    if nav:
        rows.append(nav)

    rows.append([InlineKeyboardButton("➤ Back", callback_data="menu:back")])
    return InlineKeyboardMarkup(rows), len(ids)


def deal_view_kb(page):
    rows = [
        [InlineKeyboardButton("◀ Back to My Deals", callback_data=f"dealspage:{page}")],
        [InlineKeyboardButton("➤ Main Menu", callback_data="menu:back")],
    ]
    return InlineKeyboardMarkup(rows)


def pending_deals_text(update: Update):
    username = resolve_username(update)
    pending = [
        (tid, d)
        for tid, d in DEALS.items()
        if d.get("escrowed_by") == username and d.get("status") == "ACTIVE"
    ]
    if not pending:
        return f"{pe('➤', 'chart')} Koi pending deal nahi hai."

    lines = [f"{pe('➤', 'chart')} <b>My Pending Deals</b>", "──────────────────"]
    for tid, d in pending:
        lines.append(
            f"<code>{esc(tid)}</code> — "
            f"{esc(d.get('buyer','-'))} ↔ {esc(d.get('seller','-'))} — "
            f"{fmt(d.get('amount',0), d.get('currency','INR'))}"
        )
    return "\n".join(lines)


# ===========================
# /start
# ===========================

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return  # group me /start kaam nahi karega

    await update.message.reply_text(
        welcome_text(update.effective_user.first_name),
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_kb(),
    )


async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    await query.answer()

    if data == "menu:back":
        # Private me full dashboard, group me wapas apne status pe.
        if update.effective_chat.type == "private":
            await query.edit_message_text(
                welcome_text(update.effective_user.first_name),
                parse_mode=ParseMode.HTML,
                reply_markup=main_menu_kb(),
            )
        else:
            await query.edit_message_text(
                my_stats_text(update),
                parse_mode=ParseMode.HTML,
                reply_markup=status_kb(),
            )
        return

    if data in ("menu:my_deals",) or data.startswith("dealspage:"):
        page = 0
        if data.startswith("dealspage:"):
            page = int(data.split(":", 1)[1])
        kb, total = my_deals_kb(update, page)
        if total == 0:
            text = my_deals_header_text(update) + "\n\n📭 Koi deal nahi mili."
        else:
            text = my_deals_header_text(update)
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        return

    if data.startswith("dealview:"):
        _, tid, page = data.split(":", 2)
        deal = DEALS.get(tid)
        if not deal:
            await query.edit_message_text("❌ Deal not found.", reply_markup=deal_view_kb(int(page)))
            return
        await query.edit_message_text(
            deal_detail_text(tid, deal),
            reply_markup=deal_view_kb(int(page)),
        )
        return

    target = None
    if data in ("menu:my_stats", "refresh:my_stats"):
        target = "my_stats"
        text = my_stats_text(update)
    elif data in ("menu:pending", "refresh:pending"):
        target = "pending"
        text = pending_deals_text(update)
    elif data in ("menu:global", "refresh:global"):
        target = "global"
        text = global_stats_text()
    else:
        return

    await query.edit_message_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=back_refresh_kb(target),
    )


# ===========================
# /status  — HAR USER, PRIVATE + GROUP dono me kaam karega
# ===========================

async def mystatus_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Har user apna khud ka status dekh sakta hai — group ho ya private, koi restriction nahi."""
    await update.message.reply_text(
        my_stats_text(update),
        parse_mode=ParseMode.HTML,
        reply_markup=status_kb(),
    )


# ===========================
# /add
# ===========================

async def add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only_allowed(update):
            return
    
    raw_text = (
        update.message.reply_to_message.text
        if update.message.reply_to_message
        else ""
    )
    text = normalize_bold(raw_text)

    seller = re.search(r"SELLER\s*:\s*(.*)", text, re.IGNORECASE)
    buyer = re.search(r"BUYER\s*:\s*(.*)", text, re.IGNORECASE)
    detail = re.search(r"DEAL DETAIL\s*:\s*(.*)", text, re.IGNORECASE)
    amount = re.search(r"DEAL AMOUNT\s*:\s*(.*)", text, re.IGNORECASE)
    exp_time = re.search(r"EXPECTED TIME TO COMPLETE DEAL\s*:\s*(.*)", text, re.IGNORECASE)
    tc = re.search(r"T\s*/\s*C\s*(?:\(IF ANY\))?\s*:\s*(.*)", text, re.IGNORECASE)
    currency = re.search(r"CURRENCY\s*:\s*(\w+)", text, re.IGNORECASE)

    seller_val = seller.group(1).strip() if seller else "-"
    buyer_val = buyer.group(1).strip() if buyer else "-"
    detail_val = detail.group(1).strip() if detail else "-"
    amount_val = extract_amount(amount.group(1)) if amount else 0.0
    exp_time_val = exp_time.group(1).strip() if exp_time else "-"
    tc_val = tc.group(1).strip() if tc else "-"
    currency_val = currency.group(1).strip().upper() if currency else "INR"

    is_exchange = bool(context.args) and context.args[0].lower() == "exchange"

    tid = next_trade_id()
    creator_username = resolve_username(update)

    fee_amount = calculate_fee(amount_val, is_exchange)
    release_val = amount_val - fee_amount
    fee_percent = (fee_amount / amount_val * 100) if amount_val else 0.0

    DEALS[tid] = {
        "seller": seller_val,
        "buyer": buyer_val,
        "detail": detail_val,
        "amount": amount_val,
        "release": release_val,
        "fee_percent": fee_percent,
        "exp_time": exp_time_val,
        "tc": tc_val,
        "currency": currency_val,
        "status": "ACTIVE",
        "escrowed_by": creator_username,
        "chat_id": update.effective_chat.id,
        "exchange": is_exchange,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    save_deal(tid)

    msg = (
        f"{pe('💰', 'money')} Deal Amount: {fmt(amount_val, currency_val)}\n"
        f"{pe('📤', 'chart')} Release/Refund Amount: {fmt(release_val, currency_val)}\n"
        f"{pe('🆔', 'trade')} Trade ID: <code>{esc(tid)}</code>\n\n"
        f"<b>Continue the Deal</b>\n"
        f"Buyer: {esc(buyer_val)}\n"
        f"Seller: {esc(seller_val)}\n"
        f"Detail: {esc(detail_val)}\n"
        f"Expected Time: {esc(exp_time_val)}\n"
        f"T/C: {esc(tc_val)}\n\n"
        f"{pe('🛡', 'check')} Escrowed By: {esc(creator_username)}"
    )

    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
    try:
        await update.message.delete()
    except Exception:
        pass


# ===========================
# /close
# ===========================

async def close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only_allowed(update):
            return
    
    if not update.message.reply_to_message:
        await update.message.reply_text("❌ Us deal ke message pe reply karke /close bhejo.")
        return

    reply_text = update.message.reply_to_message.text or ""
    match = re.search(r"Trade ID:\s*(DL-RIZZ-\d+)", reply_text, re.IGNORECASE)

    if not match:
        await update.message.reply_text("❌ Reply kiye gaye message me Trade ID nahi mila.")
        return

    tid = match.group(1)
    deal = DEALS.get(tid)

    if not deal:
        await update.message.reply_text("❌ Deal not found.")
        return

    if deal["status"] != "ACTIVE":
        await update.message.reply_text("❌ Yeh deal already closed hai.")
        return

    is_cancel = bool(context.args) and context.args[0].lower() == "cancel"
    currency_val = deal.get("currency", "INR")

    if is_cancel:
        released_val = 0.0
    else:
        released_val = (
            extract_amount(context.args[0])
            if context.args and not is_cancel
            else deal["release"]
        )

    deal["status"] = "CANCELLED" if is_cancel else "COMPLETED"
    deal["released"] = released_val
    deal["completed_at"] = datetime.now(timezone.utc).isoformat()
    save_deal(tid)

    closer = resolve_username(update)

    if is_cancel:
        msg = (
            f"❌ Deal Cancelled\n"
            f"{pe('🆔', 'trade')} Trade ID: <code>{esc(tid)}</code>\n"
            f"{pe('ℹ️', 'check')} 100% of the charge has been deducted.\n"
            f"{pe('🛡️', 'check')} Escrowed By: {esc(closer)}"
        )
    else:
        msg = (
            f"{pe('✅', 'check')} Deal Completed\n"
            f"{pe('🆔', 'trade')} Trade ID: <code>{esc(tid)}</code>\n"
            f"{pe('📤', 'chart')} Released: {fmt(released_val, currency_val)}\n"
            f"{pe('🛡️', 'check')} Escrowed By: {esc(closer)}\n\n"
            f"~ {esc(deal['buyer'])} and {esc(deal['seller'])} are requested to "
            f"drop the vouch before leaving👇🏻\n\n"
            f"<code>Vouch @rizzlerxescrow for {fmt(released_val, currency_val)} smooth escrow deal❤️</code>\n\n"
        )

    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
    try:
        await update.message.delete()
    except Exception:
        pass


# ===========================
# /alldeals, /leaderboard, /deal — admin only, private chat only, silent skip warna
# ===========================

async def alldeals_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Purana '/status' — ab admin ke liye saari deals ki poori list, private-only."""
    if not admin_only_allowed(update):
        return

    if not DEALS:
        await update.message.reply_text("📭 Koi deal record nahi hai.")
        return

    lines = [f"📊 <b>Total Deals:</b> {len(DEALS)}\n"]
    for tid, d in DEALS.items():
        lines.append(
            f"<code>{esc(tid)}</code> — {d['status']} — "
            f"{esc(d.get('buyer','-'))} ↔ {esc(d.get('seller','-'))} — "
            f"{fmt(d.get('amount',0), d.get('currency','INR'))}"
        )

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def leaderboard_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only_allowed(update):
        return

    today_board = build_leaderboard(today_only=True)
    all_board = build_leaderboard(today_only=False)

    def top_line(board, by):
        if not board:
            return "  koi data nahi"
        top_user, stats = max(board.items(), key=lambda kv: kv[1][by])
        return f"  {esc(top_user)} — {stats['deals']} deals, ₹{stats['volume']:,.2f}"

    msg = (
        f"{pe('🏆', 'check')} <b>Leaderboard</b>\n"
        "──────────────────\n"
        f"<b>📅 Today</b>\n"
        f"🔥 Top Dealer (most deals):\n{top_line(today_board, 'deals')}\n"
        f"💰 Top Earner (most volume):\n{top_line(today_board, 'volume')}\n\n"
        f"<b>♾ All-Time</b>\n"
        f"🔥 Top Dealer (most deals):\n{top_line(all_board, 'deals')}\n"
        f"💰 Top Earner (most volume):\n{top_line(all_board, 'volume')}"
    )

    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


async def deal_lookup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/deal DL-RIZZ-5 -> admin kisi bhi deal ki full detail (escrowed_by samet) dekh sakta hai."""
    if not admin_only_allowed(update):
        return

    if not context.args:
        await update.message.reply_text("Usage: <code>/deal DL-RIZZ-5</code>", parse_mode=ParseMode.HTML)
        return

    tid = context.args[0].upper()
    deal = DEALS.get(tid)
    if not deal:
        await update.message.reply_text("❌ Deal not found.")
        return

    await update.message.reply_text(deal_detail_text(tid, deal))


# ===========================
# Bot-admin management — sirf OWNER (.env ADMIN_IDS) add/remove kar sakta hai
# ===========================

async def addadmin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private" or not is_owner(update.effective_user.id):
        return

    if update.message.reply_to_message:
        target_id = update.message.reply_to_message.from_user.id
    elif context.args and context.args[0].isdigit():
        target_id = int(context.args[0])
    else:
        await update.message.reply_text(
            "Usage: kisi user ke message pe reply karke /addadmin bhejo, "
            "ya <code>/addadmin &lt;user_id&gt;</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    BOT_ADMINS.add(target_id)
    if admins_coll is not None:
        admins_coll.update_one(
            {"_id": target_id},
            {"$set": {"added_by": update.effective_user.id}},
            upsert=True,
        )
    await update.message.reply_text(f"✅ <code>{target_id}</code> ab bot admin hai.", parse_mode=ParseMode.HTML)


async def removeadmin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private" or not is_owner(update.effective_user.id):
        return

    if update.message.reply_to_message:
        target_id = update.message.reply_to_message.from_user.id
    elif context.args and context.args[0].isdigit():
        target_id = int(context.args[0])
    else:
        await update.message.reply_text(
            "Usage: <code>/removeadmin &lt;user_id&gt;</code>", parse_mode=ParseMode.HTML
        )
        return

    if target_id in OWNER_IDS:
        await update.message.reply_text("❌ Owner ko remove nahi kar sakte.")
        return

    BOT_ADMINS.discard(target_id)
    if admins_coll is not None:
        admins_coll.delete_one({"_id": target_id})
    await update.message.reply_text(f"✅ <code>{target_id}</code> ab admin nahi raha.", parse_mode=ParseMode.HTML)


async def admins_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only_allowed(update):
        return

    lines = [f"{pe('👑', 'check')} <b>Owners</b>"]
    lines += [f"  <code>{uid}</code>" for uid in OWNER_IDS] or ["  (koi owner set nahi hai)"]
    extra_admins = BOT_ADMINS - OWNER_IDS
    lines.append(f"\n{pe('🛡', 'check')} <b>Bot Admins</b>")
    lines += [f"  <code>{uid}</code>" for uid in extra_admins] or ["  (koi extra admin nahi hai)"]

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ===========================
# /help — admin/owner ko sab commands, normal user ko sirf user commands
# ===========================

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lines = [
        f"{pe('📖', 'check')} <b>Commands</b>",
        "──────────────────",
        "<b>👤 User Commands</b>",
        "/start — Dashboard kholo (private chat)",
        "/status — Apna deal stats dekho (private ya group, kahin bhi)",
        "/add — Deal create karo (deal message pe reply karke)",
        "/close — Deal complete karo (deal message pe reply karke)",
        "/help — Ye list dikhata hai",
    ]

    if is_admin(uid):
        lines += [
            "",
            "<b>🛡 Admin Commands</b> (private chat me hi kaam karenge)",
            "/alldeals — Saari deals ki poori list",
            "/leaderboard — Today + All-time top dealer/earner",
            "/deal &lt;DL-RIZZ-N&gt; — Kisi bhi deal ki full detail dekho",
            "/admins — Bot admins ki list dekho",
        ]

    if is_owner(uid):
        lines += [
            "",
            "<b>👑 Owner Commands</b>",
            "/addadmin — Reply karke (ya ID de ke) naya bot admin banao",
            "/removeadmin — Reply karke (ya ID de ke) admin hatao",
        ]

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ===========================
# Keep-alive server (Render port check ke liye)
# ===========================

def start_dummy_server():
    port = int(os.getenv("PORT", "10000"))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"RizzlerXEscrow bot is running")

        def do_HEAD(self):
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"✅ Dummy HTTP server listening on port {port}")


# ===========================
# Main
# ===========================

def main():
    start_dummy_server()

    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("status", mystatus_cmd))
    app.add_handler(CommandHandler("add", add))
    app.add_handler(CommandHandler("close", close))
    app.add_handler(CommandHandler("alldeals", alldeals_cmd))
    app.add_handler(CommandHandler("leaderboard", leaderboard_cmd))
    app.add_handler(CommandHandler("deal", deal_lookup_cmd))
    app.add_handler(CommandHandler("addadmin", addadmin_cmd))
    app.add_handler(CommandHandler("removeadmin", removeadmin_cmd))
    app.add_handler(CommandHandler("admins", admins_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CallbackQueryHandler(callback_router))

    print("✅ RizzlerXEscrow Bot Running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
