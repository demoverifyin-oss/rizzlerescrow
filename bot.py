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
users_coll = mongo_db["broadcast_users_rizzlerxescrow"] if mongo_db is not None else None

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


async def add_close_allowed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /add aur /close ke liye permission check:

    - Private chat: sirf hamari internal list wale bot-admin/owner (BOT_ADMINS / OWNER_IDS)
      use kar sakte hai.
    - Group / Supergroup: us GROUP ka Telegram-level admin ya owner (creator) use kar
      sakta hai — chahe wo hamari internal BOT_ADMINS list me ho ya na ho. Saath hi,
      BOT khud bhi us group me admin/owner hona chahiye, warna message delete/manage
      permission nahi milegi aur command kaam nahi karegi.

    Return: (allowed: bool, reason: str | None)
    reason sirf tab bheja jaata hai jab helpful diagnostic dena ho (warna silent skip).
    """
    chat = update.effective_chat
    user_id = update.effective_user.id

    if chat.type == "private":
        return is_admin(user_id), None

    if chat.type not in ("group", "supergroup"):
        return False, None

    # 1) Bot khud us group me admin/owner hai?
    try:
        bot_member = await context.bot.get_chat_member(chat.id, context.bot.id)
    except Exception:
        return False, "❌ Bot ka admin status is group me check nahi ho paaya."
    if bot_member.status not in ("administrator", "creator"):
        return False, (
            "❌ Ye command tabhi kaam karegi jab BOT is group me Admin ho "
            "(pehle bot ko group me admin banao)."
        )

    # 2) Command chalane wala us group ka admin/owner hai?
    try:
        user_member = await context.bot.get_chat_member(chat.id, user_id)
    except Exception:
        return False, "❌ Tumhara admin status is group me check nahi ho paaya."
    if user_member.status not in ("administrator", "creator"):
        return False, None  # normal member ke liye silent skip

    return True, None


# ===========================
# Sequential Trade ID: DL-RIZZLER-1, DL-RIZZLER-2, ...
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

    tid = f"DL-RIZZLER-{seq}"
    while tid in DEALS:  # safety, collision na ho
        seq += 1
        tid = f"DL-RIZZLER-{seq}"
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
    "⭐️": "5181422544162391976",
    "❤️": "5260535596941582167",
    "💬": "5258330865674494479",
    "🍑": "5323761960829862762",
    "⚡️": "5938539885907415367",
    "🌐": "6041705726206808304",
    "🔥": "5420315771991497307",
    "📈": "5774022692642492953",
    "🪙": "5884428842780594914",
    "💰": "6039802097916974085",
    "🤑": "5893473283696759404",
    "📱": "6152069549442208798",
    "💤": "5895266423952904371",
    "✅": "5197474765387864959",
    "🆔": "5936017305585586269",
    "🛡": "5920052658743283381",
    "📤": "6030822047150512346",
    "⭐": "5879785854284599288",
    "👤": "5258011929993026890",
    "📝": "5879841310902324730",
    "⏱️": "5936170807716745162",
    "📌": "5796440171364749940",
    "🛡️": "5920052658743283381",
    "🚀": "5780773956030043338",
    "🏆": "6194737030165959506",
    "👑": "5807868868886009920",
    "📖": "5258328383183396223",
    "ℹ️": "5994473545650934240",
}


def pe(emoji):
    """Return a Telegram custom emoji tag only for verified IDs."""
    emoji_id = PE.get(emoji)
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
        f"{pe('⭐️')} <b>Welcome {esc(first_name)}!</b>\n"
        "╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍\n"
        f"{pe('❤️')} Escrow Bot for {BRAND}\n"
        f"{pe('💬')} Provided by {PROVIDER}\n\n"
        f"{pe('🍑')} <b>This is Your Personal Dashboard:</b>\n"
        "╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍\n"
        f"Select the option below {pe('⚡️')}\n"
        "╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍"
    )


def global_stats_text():
    completed = [d for d in DEALS.values() if d.get("status") == "COMPLETED"]
    totals = {"TON": 0.0, "USDT": 0.0, "INR": 0.0}
    for d in completed:
        cur = d.get("currency", "INR")
        totals[cur] = totals.get(cur, 0.0) + d.get("amount", 0.0)

    lines = [
        f"{pe('🌐')} <b>Escrow Global Statistics</b>",
        "──────────────────",
        f"{pe('🔥')} Total Deals: {len(completed)}\n",
        f"{pe('📈')} <b>Total Volume:</b>",
        f"  {pe('🪙')} - {totals['TON']:g} TON",
        f"  {pe('💰')} - {totals['USDT']:g} USDT",
        f"  {pe('🤑')} - {totals['INR']:g} ₹",
        "──────────────────",
        f"{pe('📱')} Escrow Bot for {BRAND}",
        f"{pe('💤')} Provided by {PROVIDER}",
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
        f"{pe('📈')} <b>{esc(first_name)} Deal stats !</b>\n"
        "──────────────────\n"
        f"{pe('🚀')} Rank ➤ #{rank}\n\n"
        f"{pe('🔥')} Active deals ➤ {len(active)}\n\n"
        f"{pe('✅')} Total Escrow's ➤ {len(completed)}\n\n"
        f"{pe('⚡')} Total Volume :\n"
        f"  {pe('🪙')} ➤ {totals['TON']:g} TON\n"
        f"  {pe('💰')} ➤ {totals['USDT']:g} USDT\n"
        f"  {pe('🤑')} ➤ {totals['INR']:g} ₹\n"
        "──────────────────\n"
        f"{pe('📱')} Escrow Bot for {BRAND}\n"
        f"{pe('💤')} Provided by {PROVIDER} !"
    )


# ---- My Deals Info: paginated list + detail view ----

PAGE_SIZE = 6


def deal_status_display(status):
    return {
        "ACTIVE": "🟡 PENDING",
        "HOLD": "⏸️ HOLD",
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
        f"{pe('📱')} Escrow Bot for {BRAND}",
        f"{pe('💤')} Provided by {PROVIDER}",
    ]
    return "\n".join(lines)


def my_deals_header_text(update: Update):
    first_name = update.effective_user.first_name
    return (
        f"{pe('♡')} <b>{esc(first_name)} All deals info !</b>\n"
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
        return f"{pe('➤')} Koi pending deal nahi hai."

    lines = [f"{pe('➤')} <b>My Pending Deals</b>", "──────────────────"]
    for tid, d in pending:
        lines.append(
            f"<code>{esc(tid)}</code> — "
            f"{esc(d.get('buyer','-'))} ↔ {esc(d.get('seller','-'))} — "
            f"{fmt(d.get('amount',0), d.get('currency','INR'))}"
        )
    return "\n".join(lines)


# ===========================
# Broadcast subscribers
# ===========================

def remember_user(update: Update):
    if users_coll is None or not update.effective_user:
        return
    u = update.effective_user
    users_coll.update_one(
        {"_id": u.id},
        {"$set": {
            "username": u.username,
            "first_name": u.first_name,
            "last_seen": datetime.now(timezone.utc).isoformat(),
        }},
        upsert=True,
    )


async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private" or not is_admin(update.effective_user.id):
        return

    if not context.args:
        await update.message.reply_text("Usage: /broadcast <message>")
        return

    if users_coll is None:
        await update.message.reply_text("❌ MongoDB required for /broadcast.")
        return

    message = update.message.text.partition(" ")[2].strip()
    if not message:
        await update.message.reply_text("Usage: /broadcast <message>")
        return

    sent = failed = 0
    for doc in users_coll.find({}, {"_id": 1}):
        try:
            await context.bot.send_message(chat_id=doc["_id"], text=message)
            sent += 1
        except Exception:
            failed += 1

    await update.message.reply_text(
        f"📢 Broadcast finished.\nSent: {sent}\nFailed: {failed}"
    )


# ===========================
# /start
# ===========================

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    remember_user(update)
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
            parse_mode=ParseMode.HTML,
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
    remember_user(update)
    await update.message.reply_text(
        my_stats_text(update),
        parse_mode=ParseMode.HTML,
        reply_markup=status_kb(),
    )


# ===========================
# /add
# ===========================

async def add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    allowed, reason = await add_close_allowed(update, context)
    if not allowed:
        if reason and update.message:
            await update.message.reply_text(reason)
        return
    
    raw_text = (
        update.message.reply_to_message.text
        if update.message.reply_to_message
        else ""
    )
    text = normalize_bold(raw_text)

    # Deal template uses a bullet before every field:
    # "• SELLER : @username". Allow that bullet (and whitespace) explicitly.
    field_prefix = r"(?:^|\n)\s*(?:[•·▪▫●○‣➜➤-]\s*)?"
    seller = re.search(field_prefix + r"SELLER\s*:\s*(.*?)\s*(?:\n|$)", text, re.IGNORECASE)
    buyer = re.search(field_prefix + r"BUYER\s*:\s*(.*?)\s*(?:\n|$)", text, re.IGNORECASE)
    detail = re.search(field_prefix + r"DEAL\s+DETAIL\s*:\s*(.*?)\s*(?:\n|$)", text, re.IGNORECASE)
    amount = re.search(field_prefix + r"DEAL\s+AMOUNT\s*:\s*(.*?)\s*(?:\n|$)", text, re.IGNORECASE)
    exp_time = re.search(field_prefix + r"EXPECTED\s+TIME\s+TO\s+COMPLETE\s+DEAL\s*:\s*(.*?)\s*(?:\n|$)", text, re.IGNORECASE)
    tc = re.search(field_prefix + r"T\s*/\s*C\s*(?:\(\s*IF\s+ANY\s*\))?\s*:\s*(.*?)\s*(?:\n|$)", text, re.IGNORECASE)
    currency = re.search(field_prefix + r"CURRENCY\s*:\s*(.*?)\s*(?:\n|$)", text, re.IGNORECASE)

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
        f"{pe('💰')} <b>Deal Amount:</b> {fmt(amount_val, currency_val)}\n"
        f"{pe('📤')} <b>Fee:</b> {fee_percent:.2f}% — {fmt(fee_amount, currency_val)}\n"
        f"{pe('📤')} <b>Net Release:</b> {fmt(release_val, currency_val)}\n"
        f"{pe('🆔')} <b>Trade ID:</b> <code>{esc(tid)}</code>\n\n"
        f"{pe('👤')} <b>Buyer:</b> {esc(buyer_val)}\n"
        f"{pe('👤')} <b>Seller:</b> {esc(seller_val)}\n"
        f"{pe('📝')} <b>Detail:</b> {esc(detail_val)}\n"
        f"{pe('⏱️')} <b>Expected Time:</b> {esc(exp_time_val)}\n"
        f"{pe('📌')} <b>T/C:</b> {esc(tc_val)}\n\n"
        f"{pe('🛡')} <b>Escrowed By:</b> {esc(creator_username)}"
    )

    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
    try:
        await update.message.delete()
    except Exception:
        pass


# ===========================
# /hold — owner-only admin hold report
# ===========================

HOLD_ADMIN_EMOJI_ID = "5258011929993026890"


def _hold_admin_emoji():
    return pe('🛡️')


def _is_owner(user_id):
    return user_id in OWNER_IDS


async def hold_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Owner-only admin hold report.

    Shows every bot admin's currently open (ACTIVE) deal amount.
    /close removes the deal from this report automatically because its
    status changes to COMPLETED/CANCELLED.
    Non-owner users get no response.
    """
    if not update.effective_user or not is_admin(update.effective_user.id):
        return

    # Only the owner can use this command, regardless of chat type.
    open_deals = [
        (tid, deal) for tid, deal in DEALS.items()
        if deal.get("status") == "ACTIVE"
    ]

    # Group ACTIVE deals by the admin/escrower who created them.
    grouped = {}
    for tid, deal in open_deals:
        admin = deal.get("escrowed_by") or deal.get("created_by") or "-"
        grouped.setdefault(admin, []).append((tid, deal))

    lines = [
        f"{_hold_admin_emoji()} <b>ADMIN HOLD</b>",
        "",
    ]

    if not grouped:
        lines.append("No active deals are currently on hold.")
    else:
        grand_total = 0.0

        for admin in sorted(grouped, key=lambda x: x.lower()):
            deals = grouped[admin]
            admin_total = sum(float(d.get("amount", 0) or 0) for _, d in deals)
            grand_total += admin_total

            lines.append(
                f"{_hold_admin_emoji()} <b>{esc(admin)}</b> — "
                f"<b>Total Hold: {fmt(admin_total, 'INR')}</b>"
            )

            for tid, deal in deals:
                amount = float(deal.get("amount", 0) or 0)
                currency = deal.get("currency", "INR")
                buyer = esc(deal.get("buyer", "-"))
                seller = esc(deal.get("seller", "-"))
                detail = esc(deal.get("detail", "-"))
                fee = float(deal.get("fee_percent", 0) or 0)
                release = float(deal.get("release", 0) or 0)

                lines.extend([
                    f"  • <code>{esc(tid)}</code> — <b>{fmt(amount, currency)}</b>",
                    f"    Buyer: {buyer}",
                    f"    Seller: {seller}",
                    f"    Fee: {fee:.2f}% — Net: {fmt(release, currency)}",
                    f"    Detail: {detail}",
                ])
            lines.append("")

        lines.append("──────────────────")
        lines.append(
            f"{_hold_admin_emoji()} <b>ALL ADMINS TOTAL HOLD: "
            f"{fmt(grand_total, 'INR')}</b>"
        )

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
    )


# ===========================
# /close
# ===========================

async def close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    allowed, reason = await add_close_allowed(update, context)
    if not allowed:
        if reason and update.message:
            await update.message.reply_text(reason)
        return
    
    if not update.message.reply_to_message:
        await update.message.reply_text("❌ Us deal ke message pe reply karke /close bhejo.")
        return

    reply_text = update.message.reply_to_message.text or ""
    match = re.search(r"Trade ID:\s*(DL-RIZZLER-\d+)", reply_text, re.IGNORECASE)

    if not match:
        await update.message.reply_text("❌ Reply kiye gaye message me Trade ID nahi mila.")
        return

    tid = match.group(1)
    deal = DEALS.get(tid)

    if not deal:
        await update.message.reply_text("❌ Deal not found.")
        return

    if deal["status"] == "HOLD":
        await update.message.reply_text("⏸️ Yeh deal HOLD par hai. Pehle /unhold karo.")
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
            f"{pe('🆔')} Trade ID: <code>{esc(tid)}</code>\n"
            f"{pe('ℹ️')} 100% of the charge has been deducted.\n"
            f"{pe('🛡️')} Escrowed By: {esc(closer)}"
        )
    else:
        msg = (
            f"{pe('✅')} Deal Completed\n"
            f"{pe('🆔')} Trade ID: <code>{esc(tid)}</code>\n"
            f"{pe('📤')} Released: {fmt(released_val, currency_val)}\n"
            f"{pe('🛡️')} Escrowed By: {esc(closer)}\n\n"
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
        f"{pe('🏆')} <b>Leaderboard</b>\n"
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
    """/deal DL-RIZZLER-5 -> admin kisi bhi deal ki full detail (escrowed_by samet) dekh sakta hai."""
    if not admin_only_allowed(update):
        return

    if not context.args:
        await update.message.reply_text("Usage: <code>/deal DL-RIZZLER-5</code>", parse_mode=ParseMode.HTML)
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

    target_user = None

    if update.message.reply_to_message:
        target_user = update.message.reply_to_message.from_user
        target_id = target_user.id

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

    admin_data = {
        "added_by": update.effective_user.id,
    }

    # Reply se add karne par user's real Telegram details save hongi
    if target_user:
        admin_data.update({
            "username": target_user.username,
            "first_name": target_user.first_name,
            "last_name": target_user.last_name,
        })

    if admins_coll is not None:
        admins_coll.update_one(
            {"_id": target_id},
            {"$set": admin_data},
            upsert=True,
        )

    await update.message.reply_text(
        f"✅ <code>{target_id}</code> ab bot admin hai.",
        parse_mode=ParseMode.HTML,
    )


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

    lines = [f"{pe('👑')} <b>Owners</b>"]

    # ==========================
    # OWNERS
    # ==========================
    if not OWNER_IDS:
        lines.append("  (koi owner set nahi hai)")
    else:
        for uid in sorted(OWNER_IDS):
            lines.append(
                f'  • <a href="tg://user?id={uid}">Owner</a> '
                f'<code>({uid})</code>'
            )

    # Extra admins
    extra_admins = BOT_ADMINS - OWNER_IDS

    lines.append(f"\n{pe('🛡')} <b>Bot Admins</b>")

    if not extra_admins:
        lines.append("  (koi extra admin nahi hai)")
    else:
        for uid in sorted(extra_admins):

            # Default values
            username = None
            first_name = None
            last_name = None

            # ==========================
            # 1. MongoDB se saved details
            # ==========================
            if admins_coll is not None:
                admin_doc = admins_coll.find_one(
                    {"_id": uid}
                )

                if admin_doc:
                    username = admin_doc.get("username")
                    first_name = admin_doc.get("first_name")
                    last_name = admin_doc.get("last_name")

            # ==========================
            # 2. Alias fallback
            # ==========================
            if not username and uid in ADMIN_ALIASES:
                username = ADMIN_ALIASES[uid]

            # ==========================
            # 3. Display name banao
            # ==========================
            display_name = ""

            if first_name:
                display_name = first_name

                if last_name:
                    display_name += f" {last_name}"

            elif username:
                display_name = username.replace("_", " ").title()

            else:
                display_name = "Admin"

            # ==========================
            # Clickable display
            # ==========================

            if username:
                # Username hai -> clickable public Telegram link
                lines.append(
                    f'  • <a href="https://t.me/{esc(username)}">'
                    f'{esc(display_name)}</a> '
                    f'<code>({uid})</code>'
                )

            else:
                # Username nahi hai -> ID based clickable mention
                lines.append(
                    f'  • <a href="tg://user?id={uid}">'
                    f'{esc(display_name)}</a> '
                    f'<code>({uid})</code>'
                )

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )

# ===========================
# /help — admin/owner ko sab commands, normal user ko sirf user commands
# ===========================

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lines = [
        f"{pe('📖')} <b>Commands</b>",
        "──────────────────",
        "<b>👤 User Commands</b>",
        "/start — Dashboard kholo (private chat)",
        "/status — Apna deal stats dekho (private ya group, kahin bhi)",
        "/help — Ye list dikhata hai",
    ]

    if is_admin(uid):
        lines += [
            "",
            "<b>🛡 Admin Commands</b> (private chat me hi kaam karenge)",
            "/add — Deal create karo (deal message pe reply karke)",
            "/close — Deal complete karo (deal message pe reply karke)",
            "/alldeals — Saari deals ki poori list",
            "/leaderboard — Today + All-time top dealer/earner",
            "/deal &lt;DL-RIZZLER-N&gt; — Kisi bhi deal ki full detail dekho",
            "/admins — Bot admins ki list dekho",
            "/broadcast &lt;message&gt; — Private subscribers ko broadcast",
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
    app.add_handler(CommandHandler("hold", hold_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))
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
