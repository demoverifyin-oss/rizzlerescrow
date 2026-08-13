import os
import re
import html
import random
import string
import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from dotenv import load_dotenv
from pymongo import MongoClient
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
# ADMIN_IDS=123,456

BOT_TOKEN = os.getenv("RIZZLER_BOT_TOKEN")
BRAND = "@rizzlerxescrow"          # "Escrow Bot for @..." line
PROVIDER = "@rizzlerxescrow"       # "Provided by @..." line

MONGO_URI = os.getenv("MONGO_URI")
ADMIN_IDS = [
    int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()
]

ADMIN_ALIASES = {
    8258334055: "primaxog",
    8651783270: "gareeb_jimmy",
    8940820946: "A813ss",
}

mongo_client = MongoClient(MONGO_URI) if MONGO_URI else None
mongo_db = mongo_client["escrow_bots"] if mongo_client else None
coll = mongo_db["deals_rizzlerxescrow"] if mongo_db is not None else None

DEALS = {}

if coll is not None:
    for doc in coll.find({}):
        tid = doc.pop("_id")
        DEALS[tid] = doc
    print(f"✅ [rizzlerxescrow] {len(DEALS)} deal(s) Mongo se load hui")


def save_deal(tid):
    if coll is not None:
        coll.update_one({"_id": tid}, {"$set": dict(DEALS[tid])}, upsert=True)


# ===========================
# Bold unicode helper (Mathematical Sans-Bold block)
# ===========================

_UP = ord('𝗔') - ord('A')
_LOW = ord('𝗮') - ord('a')
_DIG = ord('𝟬') - ord('0')


def to_bold(text):
    out = []
    for ch in text:
        if 'A' <= ch <= 'Z':
            out.append(chr(ord(ch) + _UP))
        elif 'a' <= ch <= 'z':
            out.append(chr(ord(ch) + _LOW))
        elif '0' <= ch <= '9':
            out.append(chr(ord(ch) + _DIG))
        else:
            out.append(ch)
    return "".join(out)


def normalize_bold(text):
    # bold-sans unicode wapas normal ASCII me convert karta hai (parsing ke liye)
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


# ===========================
# Premium Emoji IDs (jo tumne diye)
# ===========================

PE = {
    "star1": "6113744392323867038",
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
    "check": "5774138454896022007",
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


def generate_tid():
    while True:
        tid = "#TID" + "".join(
            random.choices(string.ascii_uppercase + string.digits, k=6)
        )
        if tid not in DEALS:
            return tid


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
    totals = {}
    for d in completed:
        cur = d.get("currency", "INR")
        totals[cur] = totals.get(cur, 0.0) + d.get("amount", 0.0)

    lines = [
        f"{pe('🌐', 'globe')} <b>Escrow Global Statistics</b>",
        "──────────────────",
        f"{pe('🔥', 'fire')} Total Deals: {len(completed)}\n",
        f"{pe('📈', 'chart')} <b>Total Volume:</b>",
    ]
    if not totals:
        lines.append("  (abhi koi completed deal nahi hai)")
    else:
        icon_map = {"TON": ("🪙", "coin"), "USDT": ("💰", "money"), "INR": ("🤑", "cash")}
        for cur, amt in totals.items():
            icon, key = icon_map.get(cur, ("💠", "coin"))
            lines.append(f"  {pe(icon, key)} - {amt} {cur}")

    lines += [
        "──────────────────",
        f"{pe('📱', 'mobile')} Escrow Bot for {BRAND}",
        f"{pe('💤', 'zzz')} Provided by {PROVIDER}",
    ]
    return "\n".join(lines)


def my_stats_text(update: Update):
    username = resolve_username(update)
    mine = [d for d in DEALS.values() if d.get("escrowed_by") == username]
    completed = [d for d in mine if d.get("status") == "COMPLETED"]
    active = [d for d in mine if d.get("status") == "ACTIVE"]
    total_vol = sum(d.get("amount", 0.0) for d in completed)

    return (
        f"{pe('✦', 'star2')} <b>My Stats</b> — {esc(username)}\n"
        "──────────────────\n"
        f"{pe('🔥', 'fire')} Completed Deals: {len(completed)}\n"
        f"{pe('➤', 'chart')} Active Deals: {len(active)}\n"
        f"{pe('📈', 'chart')} Total Volume: ₹{total_vol:,.2f}"
    )


def my_deals_text(update: Update):
    username = resolve_username(update)
    mine = [
        (tid, d) for tid, d in DEALS.items() if d.get("escrowed_by") == username
    ]
    if not mine:
        return "📭 Tumhari koi deal record nahi hai."

    lines = [f"{pe('★', 'star2')} <b>My Deals Info</b>", "──────────────────"]
    for tid, d in mine[-15:]:
        lines.append(
            f"<code>{esc(tid)}</code> — {d['status']} — "
            f"{esc(d.get('buyer','-'))} ↔ {esc(d.get('seller','-'))} — "
            f"{fmt(d.get('amount',0), d.get('currency','INR'))}"
        )
    return "\n".join(lines)


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
# Handlers
# ===========================

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        await query.edit_message_text(
            welcome_text(update.effective_user.first_name),
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu_kb(),
        )
        return

    target = None
    if data in ("menu:my_stats", "refresh:my_stats"):
        target = "my_stats"
        text = my_stats_text(update)
    elif data in ("menu:my_deals", "refresh:my_deals"):
        target = "my_deals"
        text = my_deals_text(update)
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
# /add -> ESCROW DEAL format (bold unicode) pe reply karke, ya normal text pe bhi
# "/add exchange" -> id exchange deal (2.5% flat fee)
# Optional: "CURRENCY : USDT" ya "CURRENCY : TON" likha ho to us currency me track hota hai
# ===========================

async def add(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    exp_time = re.search(
        r"EXPECTED TIME TO COMPLETE DEAL\s*:\s*(.*)", text, re.IGNORECASE
    )
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

    tid = generate_tid()
    creator_username = resolve_username(update)

    fee_amount = calculate_fee(amount_val, is_exchange)
    release_val = amount_val - fee_amount

    DEALS[tid] = {
        "seller": seller_val,
        "buyer": buyer_val,
        "detail": detail_val,
        "amount": amount_val,
        "release": release_val,
        "exp_time": exp_time_val,
        "tc": tc_val,
        "currency": currency_val,
        "status": "ACTIVE",
        "escrowed_by": creator_username,
        "chat_id": update.effective_chat.id,
        "exchange": is_exchange,
    }
    save_deal(tid)

    msg = (
        f"{pe('💰', 'money')} Deal Amount: {fmt(amount_val, currency_val)}\n"
        f"{pe('📤', 'chart')} Release/Refund Amount: {fmt(release_val, currency_val)}\n"
        f"{pe('🆔', 'check')} Trade ID: <code>{esc(tid)}</code>\n\n"
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
# /close -> DEAL ke message pe reply karke chalao
# "/close cancel" -> deal cancel, 100% charge cut
# ===========================

async def close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        await update.message.reply_text(
            "❌ Us deal ke message pe reply karke /close bhejo."
        )
        return

    reply_text = update.message.reply_to_message.text or ""
    match = re.search(r"Trade ID:\s*(#TID\w+)", reply_text, re.IGNORECASE)

    if not match:
        await update.message.reply_text(
            "❌ Reply kiye gaye message me Trade ID nahi mila."
        )
        return

    tid = match.group(1).upper()
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
    save_deal(tid)

    closer = resolve_username(update)

    if is_cancel:
        msg = (
            f"❌ Deal Cancelled\n"
            f"{pe('🆔', 'check')} Trade ID: <code>{esc(tid)}</code>\n"
            f"{pe('ℹ️', 'check')} 100% of the charge has been deducted.\n"
            f"{pe('🛡️', 'check')} Escrowed By: {esc(closer)}"
        )
    else:
        msg = (
            f"{pe('✅', 'check')} Deal Completed\n"
            f"{pe('🆔', 'check')} Trade ID: <code>{esc(tid)}</code>\n"
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
# /status -> admin only
# ===========================

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Ye command sirf admin ke liye hai.")
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
    app.add_handler(CommandHandler("add", add))
    app.add_handler(CommandHandler("close", close))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CallbackQueryHandler(callback_router))

    print("✅ RizzlerXEscrow Bot Running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
