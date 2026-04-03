import os
import threading
import asyncio
import logging
import datetime as dt
from datetime import datetime, timedelta, timezone
import pytz
from flask import Flask
from pymongo import MongoClient
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, MessageHandler, CommandHandler, CallbackQueryHandler, filters, ContextTypes
from telegram.constants import ParseMode

# --- CONFIGURAZIONE ---
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
GROUP_MONITOR = int(os.getenv("GROUP_MONITOR", 0))
ADMIN_ENV = os.getenv("GROUP_ADMIN", "0")
GROUP_ADMINS = [int(i.strip()) for i in ADMIN_ENV.split(",") if i.strip()]
ITALY_TZ = pytz.timezone('Europe/Rome')

try:
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    db = client.monitor_bot
    users_col = db.users
    messages_col = db.messages
    messages_col.create_index("timestamp", expireAfterSeconds=7776000)
    logger.info("✅ MongoDB Connesso")
except Exception as e:
    logger.error(f"❌ Errore MongoDB: {e}")

# --- SERVER WEB ---
webapp = Flask(__name__)
@webapp.route('/')
def health(): return "OK", 200

def run_flask():
    port = int(os.environ.get('PORT', 10000))
    webapp.run(host='0.0.0.0', port=port, threaded=True)

threading.Thread(target=run_flask, daemon=True).start()

# --- TRACKING ---
async def track_activity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if update.effective_chat.id == GROUP_MONITOR:
            user = update.effective_user
            if not user or user.is_bot: return
            now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
            username = f"@{user.username}" if user.username else user.full_name
            users_col.update_one(
                {"user_id": user.id},
                {"$set": {"username": username, "last_seen": now_utc, "last_text": (update.message.text or "")[:100]}}, 
                upsert=True
            )
            messages_col.insert_one({"username": username, "timestamp": now_utc})
    except Exception as e: logger.error(f"Tracking error: {e}")

# --- REPORT ---
def create_status_report():
    try:
        last = messages_col.find_one(sort=[("timestamp", -1)])
        if last:
            diff_min = int((datetime.now(timezone.utc).replace(tzinfo=None) - last['timestamp']).total_seconds() // 60)
            status = "🟢 Online" if diff_min < 120 else "⚠️ Offline"
            ora_it = last['timestamp'].replace(tzinfo=pytz.UTC).astimezone(ITALY_TZ).strftime('%H:%M:%S')
            return (f"<b>📊 Report Stato Bot</b>\n{status}\n\n"
                    f"👤 Ultimo: {last['username']}\n"
                    f"⏰ Ora ITA: {ora_it}\n"
                    f"⏳ Ritardo: {max(0, diff_min)} min fa")
        return "<b>📊 Report Stato Bot</b>\n❌ Nessun dato."
    except: return "❌ Errore report."

async def total_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in GROUP_ADMINS: return
    status_msg = await update.message.reply_text("⏳ Elaborazione classifica...")
    try:
        def get_data():
            pipeline = [{"$group": {"_id": "$username", "total": {"$sum": 1}}}, {"$sort": {"total": -1}}, {"$limit": 50}]
            return list(messages_col.aggregate(pipeline))

        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(None, get_data)
        
        if not results:
            await status_msg.edit_text("📊 Database vuoto.")
            return

        lines = [f"- {i['_id']}: <b>{i['total']}</b>" for i in results]
        res = "<b>📊 Classifica Messaggi (Top 50):</b>\n" + "\n".join(lines)
        await status_msg.edit_text(res[:4000], parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Total error: {e}")
        await status_msg.edit_text("❌ Errore nel calcolo.")

async def count_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in GROUP_ADMINS: return
    try:
        days, target = int(context.args[0]), context.args[1]
        limit = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
        count = messages_col.count_documents({"username": target, "timestamp": {"$gte": limit}})
        await update.message.reply_text(f"📊 {target}: <b>{count}</b> msg in {days}gg.", parse_mode=ParseMode.HTML)
    except: await update.message.reply_text("❌ Uso: <code>/count 7 @username</code>", parse_mode=ParseMode.HTML)

async def refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in GROUP_ADMINS: return
    try:
        msg = await update.message.reply_text("🔄 Sincronizzazione...")
        all_u = list(users_col.find())
        gone_ids, gone_names = [], []
        for i, u in enumerate(all_u):
            try:
                m = await context.bot.get_chat_member(GROUP_MONITOR, u['user_id'])
                if m.status in ['left', 'kicked']: gone_ids.append(u['user_id']); gone_names.append(u['username'])
            except: gone_ids.append(u['user_id']); gone_names.append(u['username'])
            if (i+1) % 15 == 0: await msg.edit_text(f"⏳ Verificati: {i+1}/{len(all_u)}")
            await asyncio.sleep(0.1)
        
        if gone_ids:
            context.user_data['pending_del'] = gone_ids
            elenco = "\n".join([f"- {n}" for n in gone_names[:15]])
            kb = [[InlineKeyboardButton("🗑️ ELIMINA", callback_data="do_del")], [InlineKeyboardButton("❌ ANNULLA", callback_data="can_del")]]
            await msg.edit_text(f"⚠️ <b>Trovati {len(gone_ids)} usciti:</b>\n{elenco}", reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)
        else: await msg.edit_text("✅ Tutti presenti.")
    except Exception as e: logger.error(f"Refresh error: {e}")

async def list_inactive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in GROUP_ADMINS: return
    try:
        days = int(context.args[0])
        limit = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
        inactive = list(users_col.find({"last_seen": {"$lt": limit}}).limit(30))
        lines = [f"- {u['username']} ({u['last_seen'].replace(tzinfo=pytz.UTC).astimezone(ITALY_TZ).strftime('%d/%m')})" for u in inactive]
        await update.message.reply_text(f"⚠️ <b>Inattivi da {days}gg:</b>\n" + "\n".join(lines) if lines else "✅ Tutti attivi!", parse_mode=ParseMode.HTML)
    except: await update.message.reply_text("❌ Uso: <code>/list 5</code>", parse_mode=ParseMode.HTML)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "do_del":
        ids = context.user_data.get('pending_del', [])
        if ids: users_col.delete_many({"user_id": {"$in": ids}})
        await q.edit_message_text(f"✅ Rimossi {len(ids)} record.")
    else: await q.edit_message_text("❌ Annullato.")

def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(MessageHandler(filters.Chat(GROUP_MONITOR) & ~filters.COMMAND, track_activity))
    app.add_handler(CommandHandler("total", total_messages))
    app.add_handler(CommandHandler("count", count_messages))
    app.add_handler(CommandHandler("refresh", refresh))
    app.add_handler(CommandHandler("list", list_inactive))
    app.add_handler(CommandHandler("test", lambda u, c: u.message.reply_text(create_status_report(), parse_mode=ParseMode.HTML)))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.run_polling()

if __name__ == '__main__':
    main()
