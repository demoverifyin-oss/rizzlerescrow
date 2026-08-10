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
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
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
BOT_USERNAME = "rizzlerxescrow"

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


def esc(text):
    if text is None:
        return ""
    return html.escape(str(text), quote=False)


def fmt(amount):
    return f"₹{amount:,.2f}"


def extract_amount(text):
    match = re.search(r"[\d,]+(?:\.\d+)?", text or "")
    if not match:
        return 0.0
    value = match.group(0).replace(",", "")
    try:
        return float(value)
    except ValueError:
        return 0.0


# ===========================
# CHARGES ACCORDING TO AMOUNT (slab based)
# Under 199        -> flat Rs 10
# 200 - 500        -> flat Rs 20
# 501 - 2000       -> 4%
# 2001 - 3000      -> 3.5%
# Above 3000       -> 3%
# Exchange deal     -> flat 2.5% (id exchange deals ke liye, /add ke saath "exchange" likho)
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
# Premium Emoji IDs
# ===========================

EMOJI_NEW_DEAL = {
    "💰": "5987880246865565644",
    "📥": "5877307202888273539",
    "📤": "5967548335542767952",
    "🆔": "5936017305585586269",
    "🛡": "5920052658743283381",
}
EMOJI_CLOSE_DEAL = {
    "✅": "5197474765387864959",
    "🆔": "5936017305585586269",
    "📤": "5879785854284599288",
    "ℹ️": "5879785854284599288",
    "🛡️": "5920052658743283381",
}


def pe(emoji, table):
    emoji_id = table.get(emoji)
    if emoji_id:
        return f'<tg-emoji emoji-id="{emoji_id}">{emoji}</tg-emoji>'
    return emoji


def generate_tid():
    while True:
        tid = "#TID" + "".join(
            random.choices(string.ascii_uppercase + string.digits, k=6)
        )
        if tid not in DEALS:
            return tid


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
# /add -> filled deal message pe reply karke chalao
# "/add exchange" -> id exchange deal (2.5% flat fee)
# ===========================

async def add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        update.message.reply_to_message.text
        if update.message.reply_to_message
        else ""
    )

    seller = re.search(r"SELLER\s*:[ \t]*(.*)", text, re.IGNORECASE)
    buyer = re.search(r"BUYER\s*:[ \t]*(.*)", text, re.IGNORECASE)
    amount = re.search(r"AMOUNT\s*:[ \t]*(.*)", text, re.IGNORECASE)
    received = re.search(r"RECEIVED AMOUNT\s*:[ \t]*(.*)", text, re.IGNORECASE)

    seller_val = seller.group(1).strip() if seller else "-"
    buyer_val = buyer.group(1).strip() if buyer else "-"
    amount_val = extract_amount(amount.group(1)) if amount else 0.0
    received_val = (
        extract_amount(received.group(1))
        if received and received.group(1).strip()
        else amount_val
    )

    is_exchange = bool(context.args) and context.args[0].lower() == "exchange"

    tid = generate_tid()
    creator_username = resolve_username(update)

    fee_amount = calculate_fee(amount_val, is_exchange)
    release_val = amount_val - fee_amount

    DEALS[tid] = {
        "seller": seller_val,
        "buyer": buyer_val,
        "amount": amount_val,
        "received": received_val,
        "release": release_val,
        "status": "ACTIVE",
        "escrowed_by": creator_username,
        "chat_id": update.effective_chat.id,
        "exchange": is_exchange,
    }
    save_deal(tid)

    msg = (
        f"{pe('💰', EMOJI_NEW_DEAL)} Deal Amount: {fmt(amount_val)}\n"
        f"{pe('📥', EMOJI_NEW_DEAL)} Received Amount: {fmt(received_val)}\n"
        f"{pe('📤', EMOJI_NEW_DEAL)} Release/Refund Amount: {fmt(release_val)}\n"
        f"{pe('🆔', EMOJI_NEW_DEAL)} Trade ID: <code>{esc(tid)}</code>\n\n"
        f"<b>Continue the Deal</b>\n"
        f"Buyer: {esc(buyer_val)}\n"
        f"Seller: {esc(seller_val)}\n\n"
        f"{pe('🛡', EMOJI_NEW_DEAL)} Escrowed By: {esc(creator_username)}"
    )

    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
    try:
        await update.message.delete()
    except Exception:
        pass


# ===========================
# /close -> DEAL ke message pe reply karke chalao
# Agar deal cancel hui hai (100% charge cut) -> "/close cancel"
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

    if is_cancel:
        # deal cancel -> pura amount hi charge kat jata hai, kuch release nahi
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
            f"{pe('🆔', EMOJI_CLOSE_DEAL)} Trade ID: <code>{esc(tid)}</code>\n"
            f"{pe('ℹ️', EMOJI_CLOSE_DEAL)} 100% of the charge has been deducted.\n"
            f"{pe('🛡️', EMOJI_CLOSE_DEAL)} Escrowed By: {esc(closer)}"
        )
    else:
        msg = (
            f"{pe('✅', EMOJI_CLOSE_DEAL)} Deal Completed\n"
            f"{pe('🆔', EMOJI_CLOSE_DEAL)} Trade ID: <code>{esc(tid)}</code>\n"
            f"{pe('📤', EMOJI_CLOSE_DEAL)} Released: {fmt(released_val)}\n"
            f"{pe('ℹ️', EMOJI_CLOSE_DEAL)} Total Released: {fmt(released_val)}\n"
            f"{pe('🛡️', EMOJI_CLOSE_DEAL)} Escrowed By: {esc(closer)}\n\n"
            f"~ {esc(deal['buyer'])} and {esc(deal['seller'])} are requested to "
            f"drop the vouch before leaving👇🏻\n\n"
            f"<code>Vouch @{BOT_USERNAME} for {fmt(released_val)} smooth escrow deal❤️</code>\n\n"
        )

    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
    try:
        await update.message.delete()
    except Exception:
        pass


# ===========================
# /status -> admin only
# ===========================

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
            f"{esc(d['buyer'])} ↔ {esc(d['seller'])} — {fmt(d['amount'])}"
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

    app.add_handler(CommandHandler("add", add))
    app.add_handler(CommandHandler("close", close))
    app.add_handler(CommandHandler("status", status))

    print("✅ RizzlerXEscrow Bot Running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
